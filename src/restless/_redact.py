"""Redaction of sensitive values in captured traffic.

Implements CONTRACT.md section 4. Runs at the single choke point before
anything enters the upload queue; no adapter bypasses it.
"""

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Set

from ._fingerprint import _WS_CHARS
from ._text import escape_lone_surrogates, to_utf8

# REDACT-001. Values shorter than this get no tail preview.
_TAIL_MIN_LENGTH = 8
_TAIL_CHARS = 4

# REDACT-011
DEFAULT_HEADER_DENYLIST: Sequence[str] = (
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-api-key",
    "x-auth-token",
)

# REDACT-012
DEFAULT_BODY_KEY_DENYLIST: Sequence[str] = (
    "password",
    "pass",
    "pwd",
    "token",
    "secret",
    "apikey",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "sessionid",
    "ssn",
    "creditcard",
    "ccnumber",
    "cvv",
    "cvc",
)

# REDACT-013
DEFAULT_QUERY_PARAM_DENYLIST: Sequence[str] = DEFAULT_BODY_KEY_DENYLIST

# REDACT-016. Headers carrying an HTTP auth-scheme prefix. For these the
# scheme word survives so a debugger can see Bearer vs Basic vs custom at a
# glance; only the credential is replaced.
_SCHEME_PREFIX_HEADERS = frozenset({"authorization", "proxyauthorization"})


def _split_auth_scheme(value: str):
    """REDACT-016. Split ``Bearer <credential>`` into its three parts.

    Returns ``None`` when there is no scheme prefix to preserve, in which
    case the whole value is redacted.

    An explicit scan rather than ``^(\\S+)(\\s+)(\\S.*)$``, because that
    regex is not portable: JS's ``.`` excludes CR, LS and PS while Python's
    ``re.DOTALL`` includes them, and the two engines' ``\\s`` sets differ
    again. A credential containing a stray CR took the scheme-preserving
    branch in one SDK and the redact-whole branch in the other, for the
    same header value.
    """
    i = 0
    n = len(value)
    while i < n and value[i] not in _WS_CHARS:
        i += 1
    # No whitespace at all, or the value starts with it: nothing to preserve.
    if i == 0 or i >= n:
        return None
    j = i
    while j < n and value[j] in _WS_CHARS:
        j += 1
    if j >= n:  # whitespace but no credential after it
        return None
    return value[:i], value[i:j], value[j:]



def redact_value(value: str) -> str:
    """REDACT-001, REDACT-002. Replace a value with the length/tail sentinel.

    Length and tail are in Unicode CODE POINTS. Python's ``len`` and slicing
    are already code-point based, so this is the easy direction; JS needs
    an explicit ``[...s]`` to avoid counting UTF-16 units.
    """
    n = len(value)
    if n < _TAIL_MIN_LENGTH:
        return "<REDACTED:{}>".format(n)
    return "<REDACTED:{}:{}>".format(n, value[-_TAIL_CHARS:])


def _normalize(name: str) -> str:
    """REDACT-010. Full Unicode lowercase, then drop every '-' and '_'.

    Full Unicode (PRIM-020), not ASCII-only, and this is a SECURITY
    requirement. Normalization decides whether a value is redacted, so the
    safe direction is to fold MORE aggressively: a body key ``toKen`` whose
    K is U+212A KELVIN SIGN full-lowercases to ``token`` and must be
    redacted. An ASCII-only fold leaves it unmatched and ships the
    secret in plaintext.

    ``str.lower()`` is already the full, locale-independent mapping the
    reference uses, so there is nothing to hand-roll here.
    """
    return name.lower().replace("-", "").replace("_", "")


def _deny_set(defaults: Sequence[str], extra: Optional[Sequence[str]]) -> Set[str]:
    """REDACT-014. Defaults are always applied; extras only ever add."""
    names = list(defaults) + list(extra or ())
    return {_normalize(n) for n in names}


def redact_headers(
    headers: Dict[str, str], extra: Optional[Sequence[str]] = None
) -> Dict[str, str]:
    """REDACT-016..019. Returns a new dict; never mutates the input."""
    deny = _deny_set(DEFAULT_HEADER_DENYLIST, extra)
    out: Dict[str, str] = {}
    for key, value in headers.items():
        norm = _normalize(key)
        if norm not in deny:
            out[key] = value
            continue
        if norm in _SCHEME_PREFIX_HEADERS:
            split = _split_auth_scheme(value)
            if split:
                scheme, gap, credential = split
                out[key] = "{}{}{}".format(scheme, gap, redact_value(credential))
                continue
        out[key] = redact_value(value)
    return out


# --------------------------------------------------------------------------
# Query strings
# --------------------------------------------------------------------------

# REDACT-028. Characters that survive percent-encoding unescaped. This is the
# RFC 3986 unreserved set, chosen because it is the one set every language
# agrees on: JS `encodeURIComponent` additionally leaves `!'()*` alone and
# Python's `quote` defaults differ again, so the contract names the set
# explicitly rather than deferring to a builtin.
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


def _percent_encode(value: str) -> str:
    out = []
    for byte in to_utf8(value):
        ch = chr(byte)
        if ch in _UNRESERVED:
            out.append(ch)
        else:
            out.append("%{:02X}".format(byte))
    return "".join(out)


def _percent_decode(value: str) -> str:
    """Decode a query component: '+' is a space, %XX is a byte."""
    raw = bytearray()
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch == "+":
            raw.extend(b" ")
            i += 1
        elif ch == "%":
            hexpart = value[i + 1 : i + 3]
            # Validate the two digits EXPLICITLY. `int(x, 16)` is far more
            # permissive than the contract: it strips Unicode whitespace,
            # accepts underscores and a leading sign, so "%D<thin space>"
            # would decode as one byte here and stay literal in JS.
            # REDACT-029 says two hex digits, so check for exactly that.
            if len(hexpart) == 2 and all(c in _HEX_DIGITS for c in hexpart):
                raw.append(int(hexpart, 16))
                i += 3
                continue
            raw.extend(to_utf8(ch))
            i += 1
        else:
            raw.extend(to_utf8(ch))
            i += 1
    return raw.decode("utf-8", "replace")


def redact_url(url: str, extra: Optional[Sequence[str]] = None) -> str:
    """REDACT-025..027. Replace denylisted query values in place.

    Only the matched values are rewritten. The scheme, host, port, path,
    parameter order, separators and fragment come through byte for byte,
    because re-serializing a URL is both lossy and the single least portable
    thing an SDK can do (WHATWG normalization is not reproducible from
    Python's or Go's URL libraries).
    """
    deny = _deny_set(DEFAULT_QUERY_PARAM_DENYLIST, extra)

    q = url.find("?")
    if q == -1:
        return url

    head = url[: q + 1]
    rest = url[q + 1 :]

    frag = rest.find("#")
    if frag == -1:
        query, tail = rest, ""
    else:
        query, tail = rest[:frag], rest[frag:]

    if not query:
        return url

    parts: List[str] = []
    for pair in query.split("&"):
        eq = pair.find("=")
        if eq == -1:
            parts.append(pair)
            continue
        raw_key = pair[:eq]
        raw_val = pair[eq + 1 :]
        if _normalize(_percent_decode(raw_key)) in deny:
            sentinel = redact_value(_percent_decode(raw_val))
            parts.append("{}={}".format(raw_key, _percent_encode(sentinel)))
        else:
            parts.append(pair)

    return head + "&".join(parts) + tail


# --------------------------------------------------------------------------
# Bodies
# --------------------------------------------------------------------------


def _contains_denied_key(value: Any, deny: Set[str]) -> bool:
    """REDACT-021. Walk the PARSED value, never the raw text.

    Every SDK then reaches the same verdict using its own JSON parser, with
    no regex-dialect or escape-decoding differences to get wrong.
    """
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and _normalize(k) in deny:
                return True
            if _contains_denied_key(v, deny):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_denied_key(v, deny) for v in value)
    return False


def _redact_json_value(value: Any, deny: Set[str]) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and _normalize(k) in deny:
                if v is None:
                    out[k] = v
                elif isinstance(v, str):
                    out[k] = redact_value(v)
                else:
                    out[k] = "<REDACTED>"
            else:
                out[k] = _redact_json_value(v, deny)
        return out
    if isinstance(value, list):
        return [_redact_json_value(v, deny) for v in value]
    return value


def _is_json_content_type(content_type: Optional[str]) -> bool:
    return "application/json" in (content_type or "").lower()


def redact_body(
    body: Optional[str],
    content_type: Optional[str],
    extra: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """REDACT-020..024.

    When the body carries nothing to redact the caller's ORIGINAL string is
    returned byte for byte. Re-serializing is lossy in every language and in
    different ways (Python preserves int64 and ``1.0``; JS mangles both), and
    it is where SDKs diverge from each other. Skipping it for the
    overwhelming majority of bodies removes the divergence instead of trying
    to specify it away.
    """
    if not body:
        return body
    if not _is_json_content_type(content_type):
        return body
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return body

    deny = _deny_set(DEFAULT_BODY_KEY_DENYLIST, extra)
    if not _contains_denied_key(parsed, deny):
        return body

    # PRIM-030 / PRIM-031 / PRIM-032: compact separators, insertion order
    # (Python dicts preserve it), literal UTF-8 rather than \u escapes.
    return escape_lone_surrogates(
        json.dumps(
            _redact_json_value(parsed, deny),
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )


def truncate_body(body: Optional[str], max_bytes: int) -> Optional[str]:
    """REDACT-030..032. Cut at a UTF-8 byte limit, on a character boundary."""
    if not body:
        return body
    buf = to_utf8(body)
    if len(buf) <= max_bytes:
        return body
    # Walk back off any UTF-8 continuation byte (0b10xxxxxx).
    end = max_bytes
    while end > 0 and (buf[end] & 0xC0) == 0x80:
        end -= 1
    sliced = buf[:end].decode("utf-8", "replace")
    return "{}\n[...TRUNCATED: original {} bytes]".format(sliced, len(buf))
