"""WSGI adapter. Works with Flask, Django, Pyramid, Bottle, or bare WSGI.

    app.wsgi_app = client.wsgi(app.wsgi_app)

Every path in here is wrapped so that an SDK failure degrades to "no log
captured" and never to a failed request (SAFETY-001).
"""

import io
import re
import time
import traceback
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .._capture import CaptureEngine, now_iso
from .._injection import resolve_block
from .._request import RequestInfo
from .._request_id import new_request_id, request_id_response_headers
from .._uploader import _debug

#: SAFETY-007. Never buffer an unbounded response.
_MAX_CAPTURE_BYTES = 1024 * 1024
_STREAMING_TYPES = ("text/event-stream",)


def _request_headers(environ: Dict[str, Any]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            name = key[5:].replace("_", "-").lower()
            headers[name] = value
        elif key in ("CONTENT_TYPE", "CONTENT_LENGTH") and value:
            headers[key.replace("_", "-").lower()] = value
    return headers


def _full_url(environ: Dict[str, Any]) -> str:
    scheme = environ.get("wsgi.url_scheme", "http")
    host = environ.get("HTTP_HOST") or environ.get("SERVER_NAME", "localhost")
    path = environ.get("PATH_INFO", "")
    query = environ.get("QUERY_STRING", "")
    return "{}://{}{}{}".format(scheme, host, path, "?" + query if query else "")


#: Flask/Werkzeug rules look like ``/pets/<int:pet_id>`` or ``/pets/<name>``.
_FLASK_PARAM = re.compile(r"<(?:[^:<>]+:)?([^<>]+)>")


def _route_pattern(environ: Dict[str, Any]) -> Optional[str]:
    """Templated route in `{param}` form.

    Framework-native syntax is normalized so the route a Flask app reports
    matches what an ASGI or Node app reports for the same endpoint. Without
    this, Flask would ship `/pets/<int:pet_id>` where everything else ships
    `/pets/{pet_id}`, which splits the dashboard's grouping and produces a
    junk recovery slug (`get-pets-int-pet-id`).

    An absent route is expected for frameworks that expose no router, and is
    handled everywhere downstream.
    """
    rule = environ.get("restless.route")
    if not rule:
        request = environ.get("werkzeug.request")
        url_rule = getattr(request, "url_rule", None) if request is not None else None
        rule = getattr(url_rule, "rule", None) if url_rule is not None else None
    if not rule:
        return None
    return _FLASK_PARAM.sub(r"{\1}", str(rule))


def wrap_wsgi(app: Callable, engine: CaptureEngine) -> Callable:
    def middleware(environ: Dict[str, Any], start_response: Callable) -> Iterable[bytes]:
        try:
            return _capture(app, engine, environ, start_response)
        except Exception as err:  # noqa: BLE001 - SAFETY-001
            _debug("wsgi middleware failed, passing through:", err)
            return app(environ, start_response)

    return middleware


def _capture(
    app: Callable,
    engine: CaptureEngine,
    environ: Dict[str, Any],
    start_response: Callable,
) -> Iterable[bytes]:
    req_headers = _request_headers(environ)
    method = environ.get("REQUEST_METHOD", "GET")
    url = _full_url(environ)

    # The callback gets a normalized view, matching the Ruby and Go SDKs.
    # `environ` stays reachable as `request.environ`, and anything an upstream
    # layer stashed in `environ["restless.request"]` as
    # `request.framework_request`.
    setup = engine.resolve(RequestInfo(
        headers=req_headers,
        method=method,
        url=url,
        route=_route_pattern(environ),
        environ=environ,
    ))

    # SETUP-004. Reject before the handler runs.
    blocked = resolve_block(setup.get("block"))
    if blocked:
        body = ('{"error":"%s"}' % blocked["message"]).encode("utf-8")
        start_response(
            "{} Forbidden".format(blocked["status"]),
            [("content-type", "application/json"), ("content-length", str(len(body)))],
        )
        return [body]

    # Buffer the request body so the app still sees it.
    req_body = None
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    if 0 < length <= _MAX_CAPTURE_BYTES:
        stream = environ.get("wsgi.input")
        if stream is not None:
            raw = stream.read(length)
            req_body = raw.decode("utf-8", "replace")
            environ["wsgi.input"] = io.BytesIO(raw)

    raw_id = new_request_id()
    started_at = now_iso()
    start_time = time.time()

    state: Dict[str, Any] = {"status": 200, "headers": [], "chunks": [], "size": 0}

    def capturing_start_response(status: str, headers: List[Tuple[str, str]], exc_info=None):
        state["status"] = int(str(status).split(" ", 1)[0] or 500)
        state["headers"] = list(headers)

        id_headers = request_id_response_headers(
            raw_id, req_headers, engine.request_id_prefix, engine.has_api_key
        )
        merged = list(headers) + [(k, v) for k, v in id_headers.items()]
        state["deferred"] = (status, merged, exc_info)
        # Defer the real call: on a 4xx/5xx we may rewrite the body, and
        # Content-Length has to move with it (INJECT-008).
        return lambda data: None

    try:
        result = app(environ, capturing_start_response)
    except BaseException:
        # An unhandled exception is the single most valuable thing to log,
        # and without this it is the ONE case that produced no log at all:
        # the exception propagates to the server, which writes its own 500,
        # and the middleware never reaches its record() call below.
        #
        # The traceback goes through as `stackTrace`, which is what makes
        # the `stack` fingerprint strategy (FP-040) reachable at all in a
        # Python app: crashes then group by the throwing function instead
        # of collapsing into one bucket per route.
        #
        # Re-raised unchanged, so the server's 500 handling and any outer
        # middleware behave exactly as they did before (SAFETY-001).
        _record_crash(
            engine, environ, raw_id, started_at, start_time,
            req_headers, req_body, method, url, traceback.format_exc(),
        )
        raise

    res_headers = {k.lower(): v for k, v in state["headers"]}
    content_type = res_headers.get("content-type", "")
    streaming = any(t in content_type for t in _STREAMING_TYPES)

    body_parts: List[bytes] = []
    for chunk in result:
        if not streaming and state["size"] <= _MAX_CAPTURE_BYTES:
            body_parts.append(chunk)
            state["size"] += len(chunk)
        else:
            body_parts.append(chunk)

    raw_body: Optional[str] = None
    if not streaming and state["size"] <= _MAX_CAPTURE_BYTES:
        raw_body = b"".join(body_parts).decode("utf-8", "replace")

    status_code = state["status"]
    duration = int((time.time() - start_time) * 1000)

    # INJECT-009. Fingerprint the customer's RAW response, before injection.
    inject_headers, new_body, fingerprint_wire = engine.build_injection(
        status=status_code,
        request_id=raw_id,
        method=method,
        route=_route_pattern(environ),
        raw_body=raw_body,
        content_type=content_type,
        response_headers=res_headers,
    )

    status_line, out_headers, exc_info = state["deferred"]
    out_headers = [
        (k, v) for k, v in out_headers if k.lower() not in inject_headers
    ] + [(k, v) for k, v in inject_headers.items()]

    payload: List[bytes] = body_parts
    if new_body is not None and new_body != raw_body:
        encoded = new_body.encode("utf-8")
        payload = [encoded]
        # INJECT-008. Otherwise the client truncates mid-JSON.
        out_headers = [(k, v) for k, v in out_headers if k.lower() != "content-length"]
        out_headers.append(("content-length", str(len(encoded))))

    start_response(status_line, out_headers, exc_info)

    try:
        engine.record(
            {
                "requestId": raw_id,
                "startedAt": started_at,
                "duration": duration,
                "routePattern": _route_pattern(environ),
                "request": {
                    "method": method,
                    "url": url,
                    "headers": req_headers,
                    "body": req_body,
                },
                "response": {
                    "status": status_code,
                    "headers": {k.lower(): v for k, v in out_headers},
                    "body": new_body if new_body is not None else raw_body,
                },
                "user": {
                    "apiKey": setup.get("api_key") or setup.get("apiKey"),
                    "project": setup.get("project"),
                },
                "errorFingerprint": fingerprint_wire,
            }
        )
    except Exception as err:  # noqa: BLE001 - SAFETY-001
        _debug("record failed:", err)

    return payload


def _record_crash(engine, environ, raw_id, started_at, start_time,
                  req_headers, req_body, method, url, tb):
    """Log an unhandled exception as a 500. Never raises: a failure here
    would replace the app's real exception with ours, which is the worst
    possible outcome (SAFETY-001)."""
    try:
        engine.record({
            "requestId": raw_id,
            "startedAt": started_at,
            "duration": int((time.time() - start_time) * 1000),
            "routePattern": _route_pattern(environ),
            "request": {
                "method": method,
                "url": url,
                "headers": req_headers,
                "body": req_body,
            },
            "response": {
                "status": 500,
                "headers": {"content-type": "text/plain"},
                "body": None,
            },
            "stackTrace": tb,
        })
    except Exception as err:  # noqa: BLE001
        _debug("crash record failed:", err)
