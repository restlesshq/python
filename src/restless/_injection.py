"""What the SDK adds to the customer's own error responses.

Implements CONTRACT.md section 10.
"""

import json
import re
from typing import Any, Dict, Optional, Tuple

from ._request_id import format_request_id
from ._text import escape_lone_surrogates

_SLUG_SEPARATORS = re.compile(r"[/{}:]+", re.ASCII)
_SLUG_ILLEGAL = re.compile(r"[^a-zA-Z0-9-]", re.ASCII)
_SLUG_DASHES = re.compile(r"-+", re.ASCII)


def recovery_slug(method: Optional[str] = None, path: Optional[str] = None) -> str:
    """INJECT-005, INJECT-007.

    Legible slug for the dig-in URL, derived from method plus route
    pattern: ``GET /car/{id}`` becomes ``get-car-id``. The server resolves
    it back to an OpenAPI operation by applying the same scheme, so this
    MUST stay in sync with ``recoverySlug`` in the app's recovery route.
    """
    m = (method or "").lower()
    p = (path or "").strip()
    if not m or not p:
        return "unknown"
    flat = _SLUG_SEPARATORS.sub("-", p)
    flat = _SLUG_ILLEGAL.sub("", flat)
    flat = _SLUG_DASHES.sub("-", flat)
    flat = flat.strip("-")
    return "{}-{}".format(m, flat) if flat else m


def build_debug_injection(
    status: int,
    request_id: str,
    base_url: str,
    prefix: Optional[str] = None,
    recovery: Optional[str] = None,
    method: Optional[str] = None,
    path: Optional[str] = None,
    docs_url: Optional[str] = None,
) -> Tuple[Dict[str, str], Optional[Any]]:
    """INJECT-001..006. Returns (headers, body_mutator).

    ``body_mutator`` is None when nothing should be injected.
    """
    if status < 400:
        return {}, None

    display = format_request_id(request_id, prefix)
    # INJECT-006. Server-learned docs origin when we have one, else the
    # configured base URL. One-batch staleness window after a domain change.
    log_host = docs_url or base_url
    log_url = "{}/logs/{}".format(log_host, request_id)
    debug_cmd = "npx api debug {}".format(display)

    slug = recovery_slug(method, path)
    dig_in = "For the accepted parameters and next steps, fetch {}/p/{}/{}.md".format(
        log_host, request_id, slug
    )
    # INJECT-004. The dig-in line always ships; a cached recovery message
    # precedes it, separated by a blank line.
    recovery_text = "{}\n\n{}".format(recovery, dig_in) if recovery else dig_in

    def mutate(body: Any) -> Any:
        # INJECT-003. Objects only. An array or scalar body is left alone;
        # there is nowhere sensible to attach `debug`.
        if isinstance(body, dict):
            out = dict(body)
            out["debug"] = {"log": log_url, "cli": debug_cmd, "recovery": recovery_text}
            return out
        return body

    return {"x-log-url": log_url, "x-debug": debug_cmd}, mutate


def apply_internal_body_mods(
    body: Optional[str], content_type: Optional[str], mutate: Optional[Any]
) -> Optional[str]:
    """INJECT-003. Only rewrites JSON-typed bodies; never raises."""
    if not body or mutate is None:
        return body
    if "application/json" not in (content_type or "").lower():
        return body
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return body
    try:
        return escape_lone_surrogates(
            json.dumps(
                mutate(parsed), separators=(",", ":"), ensure_ascii=False, allow_nan=False
            )
        )
    except (TypeError, ValueError, RecursionError):
        return body


def resolve_block(block: Any) -> Optional[Dict[str, Any]]:
    """SETUP-004. Normalize the `block` field into a concrete response."""
    if not block:
        return None
    if block is True:
        return {"status": 403, "message": "Forbidden"}
    if isinstance(block, dict):
        return {
            "status": block.get("status", 403),
            "message": block.get("message", "Forbidden"),
        }
    return {"status": 403, "message": "Forbidden"}
