"""HAR 1.2 envelope construction.

Implements CONTRACT.md section 7.
"""

import json
from typing import Any, Dict, List, Optional

from ._redact import _percent_decode
from ._text import escape_lone_surrogates
from ._text import utf8_len as _utf8_len


def utf8_len(s: str) -> int:
    """HAR-010. Sizes are UTF-8 BYTES, not characters and not code units."""
    return _utf8_len(s)


def safe_serialize_body(body: Any, content_type: Optional[str] = None) -> Optional[str]:
    """SAFETY-005, SAFETY-006. Serialize an already-parsed body, defensively.

    Anything that cannot be serialized is dropped to "no body captured"
    rather than raised. Observability must never break the request path.
    """
    if isinstance(body, str):
        return body
    if body is None or body == b"":
        return None
    if isinstance(body, (bytes, bytearray)):
        try:
            return bytes(body).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if "multipart/form-data" in str(content_type or "").lower():
        return None
    try:
        return escape_lone_surrogates(
            json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        )
    except (TypeError, ValueError, RecursionError):
        return None


def _headers_to_list(headers: Dict[str, str]) -> List[Dict[str, str]]:
    """HAR-004. Order preserved."""
    return [{"name": k, "value": v} for k, v in headers.items()]


def parse_query_string(url: str) -> List[Dict[str, str]]:
    """HAR-005. Parse the query into ordered name/value pairs.

    Mirrors WHATWG URLSearchParams parsing: split on '&', then on the first
    '=', percent-decode both halves with '+' meaning space. Duplicate names
    are preserved, unlike a dict-based parse.
    """
    q = url.find("?")
    if q == -1:
        return []
    rest = url[q + 1 :]
    frag = rest.find("#")
    if frag != -1:
        rest = rest[:frag]
    if not rest:
        return []

    out: List[Dict[str, str]] = []
    for pair in rest.split("&"):
        if not pair:
            continue
        eq = pair.find("=")
        if eq == -1:
            out.append({"name": _percent_decode(pair), "value": ""})
        else:
            out.append(
                {
                    "name": _percent_decode(pair[:eq]),
                    "value": _percent_decode(pair[eq + 1 :]),
                }
            )
    return out


def to_har_entry(captured: Dict[str, Any]) -> Dict[str, Any]:
    """Build the HAR entry for one captured request/response pair."""
    req = captured["request"]
    res = captured["response"]

    req_headers: Dict[str, str] = req.get("headers") or {}
    res_headers: Dict[str, str] = res.get("headers") or {}

    req_content_type = req_headers.get("content-type", "")
    # HAR-007
    res_content_type = res_headers.get("content-type") or "application/octet-stream"

    req_body = req.get("body")
    res_body = res.get("body")

    request: Dict[str, Any] = {
        "method": req["method"],
        "url": req["url"],
        "httpVersion": "HTTP/1.1",  # HAR-003
        "headers": _headers_to_list(req_headers),
        "queryString": parse_query_string(req["url"]),
    }
    # HAR-006. postData only when a body was captured.
    if req_body:
        request["postData"] = {"mimeType": req_content_type, "text": req_body}
    request["headersSize"] = -1  # HAR-012
    # HAR-011. -1 means "no body captured". An empty body is 0, not -1.
    request["bodySize"] = -1 if req_body is None else utf8_len(req_body)

    response: Dict[str, Any] = {
        "status": res["status"],
        "statusText": "",  # HAR-008
        "httpVersion": "HTTP/1.1",
        "headers": _headers_to_list(res_headers),
        "content": {
            "size": 0 if res_body is None else utf8_len(res_body),
            "mimeType": res_content_type,
            "text": res_body if res_body is not None else "",
        },
        "headersSize": -1,
        "bodySize": -1 if res_body is None else utf8_len(res_body),
    }

    duration = captured["duration"]
    return {
        "startedDateTime": captured["startedAt"],  # HAR-001
        "time": duration,
        "request": request,
        "response": response,
        "timings": {"send": 0, "wait": duration, "receive": 0},  # HAR-002
    }
