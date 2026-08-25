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


def debug_headers(
    request_id: str,
    prefix: Optional[str] = None,
    portal_url: Optional[str] = None,
) -> Dict[str, str]:
    """INJECT-002. The debug response headers, which ship on every status.

    ``x-log-url`` is omitted with no portal origin; ``x-debug`` carries no
    URL, so it always ships.
    """
    headers = {"x-debug": "npx api debug {}".format(format_request_id(request_id, prefix))}
    if portal_url:
        headers["x-log-url"] = "{}/logs/{}".format(portal_url, request_id)
    return headers


def build_debug_injection(
    status: int,
    request_id: str,
    prefix: Optional[str] = None,
    recovery: Optional[str] = None,
    method: Optional[str] = None,
    path: Optional[str] = None,
    portal_url: Optional[str] = None,
) -> Tuple[Dict[str, str], Optional[Any]]:
    """INJECT-001..006. Returns (headers, body_mutator).

    ``body_mutator`` is None when nothing should be injected.

    ``portal_url`` is the project's public portal origin, published by the
    server. It is NOT the ingest base URL, which serves ``/v1/*`` and would
    404 both paths, and there is deliberately no fallback to it: with no
    portal origin we emit ``x-debug`` alone. A caller cannot tell a broken
    URL from a missing one, and one fetched 404 teaches an agent to stop
    following the link (INJECT-006).
    """
    headers = debug_headers(request_id, prefix, portal_url)

    # INJECT-001. The body object is 4xx/5xx only: a successful body is the
    # caller's data, not ours to reshape. With no portal origin there is no
    # URL to put in one either (INJECT-006).
    if status < 400 or not portal_url:
        return headers, None

    log_url = headers["x-log-url"]
    debug_cmd = headers["x-debug"]

    slug = recovery_slug(method, path)
    dig_in = "For the accepted parameters and next steps, fetch {}/p/{}/{}.md".format(
        portal_url, request_id, slug
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

    return headers, mutate


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
