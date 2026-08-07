"""ASGI adapter. Works with FastAPI, Starlette, Quart, or bare ASGI.

    app.add_middleware(...)  # or:
    app = client.asgi(app)

Unlike the WSGI adapter this one can await an async ``enrich`` callback,
which is the common case in an async framework.
"""

import time
from typing import Any, Callable, Dict, List, Optional

from .._capture import CaptureEngine, now_iso
from .._injection import resolve_block
from .._request_id import new_request_id, request_id_response_headers
from .._uploader import _debug

_MAX_CAPTURE_BYTES = 1024 * 1024
_STREAMING_TYPES = ("text/event-stream",)


def _decode_headers(raw: List) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in raw or []:
        name = key.decode("latin-1").lower()
        val = value.decode("latin-1")
        # HAR-004. Duplicates are joined, matching the reference.
        out[name] = "{}, {}".format(out[name], val) if name in out else val
    return out


def _full_url(scope: Dict[str, Any], headers: Dict[str, str]) -> str:
    scheme = scope.get("scheme", "http")
    host = headers.get("host") or "localhost"
    path = scope.get("path", "")
    query = (scope.get("query_string") or b"").decode("latin-1")
    return "{}://{}{}{}".format(scheme, host, path, "?" + query if query else "")


def _route_pattern(scope: Dict[str, Any]) -> Optional[str]:
    route = scope.get("route")
    path_format = getattr(route, "path_format", None) or getattr(route, "path", None)
    if path_format:
        return str(path_format)
    # Starlette records this on the scope for matched routes.
    endpoint = scope.get("path_params")
    if endpoint is not None and scope.get("raw_path") is not None:
        return None
    return None


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def wrap_asgi(app: Callable, engine: CaptureEngine) -> Callable:
    async def middleware(scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        try:
            await _capture(app, engine, scope, receive, send)
        except Exception as err:  # noqa: BLE001 - SAFETY-001
            _debug("asgi middleware failed, passing through:", err)
            await app(scope, receive, send)

    return middleware


async def _capture(app, engine, scope, receive, send) -> None:
    req_headers = _decode_headers(scope.get("headers") or [])
    method = scope.get("method", "GET")
    url = _full_url(scope, req_headers)

    setup = engine.resolve(scope)
    # An async enrich returns a coroutine that the sync engine could not
    # await; resolve it here and merge, so async lookups work naturally.
    project = setup.get("project")
    if isinstance(project, dict):
        for key, value in list(project.items()):
            if hasattr(value, "__await__"):
                project[key] = await _maybe_await(value)

    blocked = resolve_block(setup.get("block"))
    if blocked:
        body = ('{"error":"%s"}' % blocked["message"]).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": blocked["status"],
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})
        return

    req_chunks: List[bytes] = []
    req_size = 0

    async def capturing_receive():
        nonlocal req_size
        message = await receive()
        if message.get("type") == "http.request":
            chunk = message.get("body", b"")
            if req_size + len(chunk) <= _MAX_CAPTURE_BYTES:
                req_chunks.append(chunk)
                req_size += len(chunk)
        return message

    raw_id = new_request_id()
    started_at = now_iso()
    start_time = time.time()

    state: Dict[str, Any] = {"status": 200, "headers": {}, "chunks": [], "size": 0,
                             "streaming": False, "start_sent": False}

    async def capturing_send(message: Dict[str, Any]) -> None:
        mtype = message.get("type")
        if mtype == "http.response.start":
            state["status"] = message.get("status", 200)
            state["raw_headers"] = list(message.get("headers") or [])
            state["headers"] = _decode_headers(state["raw_headers"])
            content_type = state["headers"].get("content-type", "")
            state["streaming"] = any(t in content_type for t in _STREAMING_TYPES)
            # Hold the start until the body is known: on a 4xx/5xx we may
            # rewrite it and content-length has to move with it (INJECT-008).
            if state["status"] < 400 or state["streaming"]:
                state["start_sent"] = True
                id_headers = request_id_response_headers(
                    raw_id, req_headers, engine.request_id_prefix, engine.has_api_key)
                message = dict(message, headers=state["raw_headers"] + [
                    (k.encode(), v.encode()) for k, v in id_headers.items()])
                await send(message)
            return

        if mtype == "http.response.body":
            chunk = message.get("body", b"")
            more = message.get("more_body", False)
            if not state["streaming"] and state["size"] + len(chunk) <= _MAX_CAPTURE_BYTES:
                state["chunks"].append(chunk)
                state["size"] += len(chunk)
            if state["start_sent"]:
                await send(message)
                if not more:
                    await _finish(engine, scope, state, raw_id, req_headers, req_chunks,
                                  method, url, started_at, start_time, setup, sent=True)
                return
            if more:
                return  # keep buffering until the final chunk
            await _finish(engine, scope, state, raw_id, req_headers, req_chunks,
                          method, url, started_at, start_time, setup, sent=False, send=send)
            return

        await send(message)

    await app(scope, capturing_receive, capturing_send)


async def _finish(engine, scope, state, raw_id, req_headers, req_chunks,
                  method, url, started_at, start_time, setup, sent, send=None) -> None:
    raw_body: Optional[str] = None
    if not state["streaming"]:
        raw_body = b"".join(state["chunks"]).decode("utf-8", "replace")

    route = _route_pattern(scope)
    duration = int((time.time() - start_time) * 1000)
    status = state["status"]

    inject_headers, new_body, fingerprint_wire = engine.build_injection(
        status=status,
        request_id=raw_id,
        method=method,
        route=route,
        raw_body=raw_body,
        content_type=state["headers"].get("content-type", ""),
        response_headers=state["headers"],
    )

    out_headers = dict(state["headers"])
    if not sent and send is not None:
        id_headers = request_id_response_headers(
            raw_id, req_headers, engine.request_id_prefix, engine.has_api_key)
        header_list = [(k.encode(), v.encode()) for k, v in state["headers"].items()
                       if k not in inject_headers]
        for k, v in list(inject_headers.items()) + list(id_headers.items()):
            header_list.append((k.encode(), v.encode()))
            out_headers[k] = v

        body_bytes = b"".join(state["chunks"])
        if new_body is not None and new_body != raw_body:
            body_bytes = new_body.encode("utf-8")
            header_list = [(k, v) for k, v in header_list if k != b"content-length"]
            header_list.append((b"content-length", str(len(body_bytes)).encode()))

        await send({"type": "http.response.start", "status": status, "headers": header_list})
        await send({"type": "http.response.body", "body": body_bytes})

    try:
        engine.record({
            "requestId": raw_id,
            "startedAt": started_at,
            "duration": duration,
            "routePattern": route,
            "request": {
                "method": method,
                "url": url,
                "headers": req_headers,
                "body": b"".join(req_chunks).decode("utf-8", "replace") or None,
            },
            "response": {
                "status": status,
                "headers": out_headers,
                "body": new_body if new_body is not None else raw_body,
            },
            "user": {
                "apiKey": setup.get("api_key") or setup.get("apiKey"),
                "project": setup.get("project"),
            },
            "errorFingerprint": fingerprint_wire,
        })
    except Exception as err:  # noqa: BLE001 - SAFETY-001
        _debug("record failed:", err)
