"""Request identifiers.

Implements CONTRACT.md section 6.
"""

import re
import uuid
from typing import Dict, Optional

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE | re.ASCII | re.DOTALL,
)
_PREFIXED_RE = re.compile(r"[A-Za-z0-9]{1,7}-(.+)", re.DOTALL | re.ASCII)


def new_request_id() -> str:
    """REQID-001, REQID-002.

    RFC 4122 v4 from a CSPRNG. Deliberately NOT time-ordered: request ids
    appear in user-visible URLs and logs, and must not leak ordering or
    timing. ``uuid.uuid4()`` reads from ``os.urandom``.
    """
    return str(uuid.uuid4())


def format_request_id(raw_id: str, prefix: Optional[str] = None) -> str:
    """REQID-004. Display form. The raw UUID is what goes on the wire."""
    return "{}-{}".format(prefix, raw_id) if prefix else raw_id


def strip_request_id_prefix(request_id: str) -> str:
    """REQID-005. Safe when no prefix is present."""
    m = _PREFIXED_RE.fullmatch(request_id)
    if m and _UUID_RE.fullmatch(m.group(1)):
        return m.group(1)
    return request_id


def is_valid_request_id(raw: str) -> bool:
    return _UUID_RE.fullmatch(strip_request_id_prefix(raw)) is not None


def request_id_response_headers(
    our_id: str,
    incoming_headers: Dict[str, str],
    prefix: Optional[str] = None,
    has_api_key: bool = True,
) -> Dict[str, str]:
    """REQID-010, REQID-011. Exactly one id header per response.

    If the incoming request already carried ``x-request-id`` we emit our own
    ``x-restless-id`` instead, so an existing request-id chain set by a
    client or proxy is never clobbered. We never REUSE the incoming value:
    our id is always freshly minted so one UUID identifies one log.
    """
    value = format_request_id(our_id, prefix) if has_api_key else "missing-key"
    # Incoming header names are case-insensitive.
    has_incoming = any(
        isinstance(k, str) and k.lower() == "x-request-id" and v
        for k, v in incoming_headers.items()
    )
    name = "x-restless-id" if has_incoming else "x-request-id"
    return {name: value}
