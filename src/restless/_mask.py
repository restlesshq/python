"""End-user API key masking.

Implements CONTRACT.md section 3. This is the lookup key the ingest and the
dashboard index on, so it must be byte-identical to every other SDK and to
the server's own ``mask()``.
"""

import base64
import hashlib
import re
from typing import Optional

from ._text import to_utf8

# MASK-011. Setup-time placeholders that must not become real-looking masks.
# Matched exactly and case-sensitively.
_PLACEHOLDER_KEYS = frozenset(
    {"API_KEY_HERE", "YOUR_API_KEY", "YOUR_KEY", "REPLACE_ME"}
)

# PRIM-005: `fullmatch` with no anchors. Python's `$` also matches just
# before a trailing newline, so "^...$" would accept a value JS rejects.
_REDACTED_RE = re.compile(r"<REDACTED:\d+(?::[^>]*)?>", re.DOTALL)


def mask(api_key: Optional[str]) -> Optional[str]:
    """Mask an end-user API key.

    Returns ``sha512-<standard base64 of sha512(utf8(key))>?<last 4 code
    points>``, or ``None`` when there is no usable key.

    Pass the raw header value straight through. Never substitute a
    placeholder such as ``"anonymous"``: its last 4 characters would become
    the tail and cluster unrelated callers together (MASK-010).
    """
    if not api_key:
        return None
    if api_key in _PLACEHOLDER_KEYS:
        return None

    # MASK-012 / MASK-013. Idempotent for our own output, and a value the
    # SDK already redacted passes through rather than being hashed.
    if api_key.startswith("sha512-"):
        return api_key
    if _REDACTED_RE.fullmatch(api_key):
        return api_key

    # MASK-004. Hash the UTF-8 encoding. `to_utf8` (PRIM-013) maps unpaired
    # surrogates to U+FFFD the way Node's Buffer does; `errors="replace"`
    # would emit a 1-byte "?" instead and silently produce a different
    # digest, and `errors="strict"` would raise on the request path.
    digest = hashlib.sha512(to_utf8(api_key)).digest()
    encoded = base64.b64encode(digest).decode("ascii")

    # MASK-006. Last 4 CODE POINTS. Python's slicing is code-point based, so
    # this is naturally correct here; it is JS that needs the care.
    last4 = api_key[-4:]
    return "sha512-{}?{}".format(encoded, last4)
