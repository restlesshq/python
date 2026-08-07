"""Stable identifiers for HTTP error responses.

Implements CONTRACT.md section 5. The SDK computes a fingerprint at capture
time and ships it; the ingest stores it, the dashboard groups by it, and a
customer attaches a recovery message to a group. Nothing downstream ever
re-derives it, so every SDK must agree exactly.

Regex note: every pattern here is compiled with ``re.ASCII``. Python's
``re`` makes ``\\w``, ``\\d`` and ``\\b`` Unicode-aware by default, while
JS, Go (RE2) and Ruby keep them ASCII. Without the flag a message
containing any accented character produces a different key here than in
every other SDK. See CONTRACT.md PRIM-001 and PRIM-003.
"""

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

# PRIM-002. The whitespace class, enumerated as escapes rather than literal
# characters: most of these are invisible and several are indistinguishable
# from a plain space in an editor.
#
# This is the JS `\s` set. Python's `\s` is narrower under re.ASCII and
# wider under Unicode, so neither shorthand is correct here.
_WS_CHARS = (
    "\t\n\x0b\x0c\r\x20\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
# Safe to interpolate straight into a character class: the set contains no
# '-', '^', ']' or backslash, so nothing can be read as a range or escape.
_WS = _WS_CHARS

# PRIM-001
_WORD = "A-Za-z0-9_"

_CODE_FIELDS: Sequence[str] = ("code", "error_code", "errorCode", "type")
_NESTED_PATHS: Sequence[Sequence[str]] = (
    ("error", "code"),
    ("error", "type"),
    ("error", "error_code"),
)

# FP-015. A code must look like an identifier, not a sentence or a UUID.
# PRIM-005: unanchored + fullmatch. Python's `$` also matches before a
# trailing newline, so "^...$" accepts values JS rejects.
_CODE_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_.\-]*", re.ASCII | re.DOTALL)

# FP-030. Whole-segment tests. Deliberately anchored rather than scanning
# with a lookahead: RE2 has no lookahead, so the scanning form could not be
# ported to Go at all.
_SEG_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE | re.ASCII | re.DOTALL,
)
_SEG_NUMERIC = re.compile(r"[0-9]+", re.ASCII)
_SEG_LONG_HEX = re.compile(r"[0-9a-f]{16,}", re.IGNORECASE | re.ASCII)

# FP-020 steps 2 through 7.
_RE_URL = re.compile(r"https?://[^" + _WS + r"]+", re.ASCII)
_RE_EMAIL = re.compile(
    r"[^" + _WS + r"]+@[^" + _WS + r"]+\.[^" + _WS + r"]+", re.ASCII
)
_RE_QUOTED = re.compile(r"['\"`][^'\"`]*['\"`]", re.ASCII)
_RE_DIGIT_WORD = re.compile(
    r"\b[" + _WORD + r"-]*[0-9][" + _WORD + r"-]*\b", re.ASCII
)
_RE_PUNCTUATION = re.compile(r"[^" + _WORD + _WS + r"-]", re.ASCII)
_RE_WS_RUN = re.compile(r"[" + _WS + r"]+", re.ASCII)

# FP-042
_PROJECT_DIR_RE = re.compile(
    r"/(?:src|lib|app|api|routes|controllers|handlers)/.+$"
)

# FP-044. Python traceback frames, e.g.
#   File "/proj/src/db/users.py", line 12, in find_by_id
_PY_FRAME_RE = re.compile(r'File "(?P<file>[^"]+)", line \d+, in (?P<fn>\S+)')

# Frames that are not user code. The Node reference skips node_modules,
# node:internal and its own package; these are the Python equivalents.
_SKIP_FRAME_MARKERS = (
    "site-packages",
    "dist-packages",
    "<frozen",
    "/lib/python",
    "restless/_capture.py",
    "restless/adapters/",
)


class Fingerprint:
    __slots__ = ("strategy", "key", "reason")

    def __init__(self, strategy: str, key: str, reason: str):
        self.strategy = strategy
        self.key = key
        self.reason = reason

    def to_wire(self) -> Dict[str, str]:
        return {"strategy": self.strategy, "key": self.key, "reason": self.reason}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Fingerprint({!r}, {!r})".format(self.strategy, self.key)


def _looks_like_code(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 64
        and _CODE_RE.fullmatch(value) is not None
    )


def _read_header_code(headers: Optional[Dict[str, str]]) -> Optional[str]:
    """FP-017. Header names are case-insensitive."""
    if not headers:
        return None
    for name, value in headers.items():
        if isinstance(name, str) and name.lower() == "x-restless-error-code":
            return value if _looks_like_code(value) else None
    return None


def _read_body_code(body: Any) -> Optional[str]:
    """FP-016. Field names matched EXACTLY, unlike redaction keys."""
    if not isinstance(body, dict):
        return None
    for field in _CODE_FIELDS:
        if _looks_like_code(body.get(field)):
            return body[field]
    for path in _NESTED_PATHS:
        cursor: Any = body
        for part in path:
            cursor = cursor.get(part) if isinstance(cursor, dict) else None
        if _looks_like_code(cursor):
            return cursor
    return None


def project_relative(file: str) -> str:
    """FP-042. Make a source path machine-independent."""
    m = _PROJECT_DIR_RE.search(file)
    if m:
        return m.group(0)[1:]
    return "/".join(file.split("/")[-2:])


def top_user_frame(
    stack: Union[str, Sequence[str], None]
) -> Optional[Tuple[str, str]]:
    """FP-043, FP-044. Innermost frame that is not vendor or runtime code.

    Parses PYTHON tracebacks. The frame format is language-specific by
    design; only the output shape is contract surface, so a v8 stack string
    is simply unparseable here and the ladder falls through.

    Iterated in REVERSE, which is the whole subtlety of porting this. FP-043
    wants the frame nearest the throw site, because that is what tells two
    different crashes apart. The two languages order their frames
    oppositely:

        v8 stack            throw site FIRST, callers below
        Python traceback    callers first, throw site LAST

    So the reference walks forwards and this walks backwards, and both land
    on the throw site. Walking forwards here would return the WSGI entry
    point for every crash in the process, collapsing every 500 in the app
    into a single fingerprint group and making error grouping useless.
    """
    if not stack:
        return None
    lines: List[str] = (
        stack.split("\n") if isinstance(stack, str) else [str(s) for s in stack]
    )
    for line in reversed(lines):
        if any(marker in line for marker in _SKIP_FRAME_MARKERS):
            continue
        m = _PY_FRAME_RE.search(line)
        if not m:
            continue
        return project_relative(m.group("file")), m.group("fn") or "anonymous"
    return None


def normalize_route(route: Optional[str]) -> str:
    """FP-030..032."""
    if not route:
        return "/"
    segments = route.split("/")
    # FP-031: index 0 is the text BEFORE the first slash and is never
    # normalized. Preserved deliberately so stored fingerprints stay stable.
    for i in range(1, len(segments)):
        seg = segments[i]
        if (
            _SEG_UUID.fullmatch(seg)
            or _SEG_NUMERIC.fullmatch(seg)
            or _SEG_LONG_HEX.fullmatch(seg)
        ):
            segments[i] = ":id"
    return "/".join(segments)


def normalize_message(msg: Optional[str]) -> str:
    """FP-020. Steps applied in exactly the contract's order."""
    if not msg:
        return ""
    s = msg.lower()
    s = _RE_URL.sub(" ", s)
    s = _RE_EMAIL.sub(" ", s)
    s = _RE_QUOTED.sub(" ", s)
    s = _RE_DIGIT_WORD.sub(" ", s)
    s = _RE_PUNCTUATION.sub(" ", s)
    s = _RE_WS_RUN.sub(" ", s)
    s = s.strip(_strip_chars())
    tokens = [t for t in s.split(" ") if len(t) > 1]
    return "-".join(tokens[:6])


def _strip_chars() -> str:
    """The literal characters in WS, for str.strip.

    ``str.strip()`` with no argument uses Python's own whitespace notion,
    which is not the contract's WS set. Passing the set explicitly keeps
    step 8 aligned with steps 6 and 7.
    """
    return _WS_CHARS

def _extract_message(body: Any) -> str:
    """FP-018."""
    if not body:
        return ""
    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return ""
    if isinstance(body.get("message"), str):
        return body["message"]
    nested = body.get("error")
    if isinstance(nested, str):
        return nested
    if isinstance(nested, dict) and isinstance(nested.get("message"), str):
        return nested["message"]
    return ""


def fingerprint(
    status: int,
    method: Optional[str] = None,
    route: Optional[str] = None,
    response_headers: Optional[Dict[str, str]] = None,
    response_body: Any = None,
    stack_trace: Union[str, Sequence[str], None] = None,
) -> Fingerprint:
    """FP-010. Strategies in priority order; first to yield a key wins."""
    method = method or "GET"  # FP-011

    # FP-012, FP-013. 404 is intercepted before the code strategies. A
    # generic not_found code is identical on every route, so grouping 404s
    # by code is useless for recovery. The two buckets need opposite advice.
    if status == 404:
        norm = normalize_route(route) if route else ""
        if ":" in norm or "{" in norm:
            return Fingerprint(
                "resource",
                "404:resource",
                "404 on a parameterized route ({} {}); the addressed resource was not found".format(
                    method, norm
                ),
            )
        return Fingerprint(
            "endpoint",
            "404:endpoint",
            "404 on {} {}; no resource at this path".format(method, norm)
            if norm
            else "404 on a path that matched no route; the endpoint does not exist",
        )

    header_code = _read_header_code(response_headers)
    if header_code:
        return Fingerprint(
            "header",
            "{}:{}".format(status, header_code),
            'x-restless-error-code header: "{}"'.format(header_code),
        )

    body_code = _read_body_code(response_body)
    if body_code:
        return Fingerprint(
            "body-code",
            "{}:{}".format(status, body_code),
            'code field in body: "{}"'.format(body_code),
        )

    if status >= 500 and stack_trace:
        frame = top_user_frame(stack_trace)
        if frame:
            file, fn = frame
            return Fingerprint(
                "stack",
                "{}:{}:{}".format(status, file, fn),
                "top user frame: {} in {}".format(fn, file),
            )

    norm_route = normalize_route(route)
    msg = normalize_message(_extract_message(response_body))
    if msg:
        return Fingerprint(
            "message",
            "{}:{}:{}:{}".format(status, method, norm_route, msg),
            'message normalized to "{}"'.format(msg),
        )

    return Fingerprint(
        "route-only",
        "{}:{}:{}".format(status, method, norm_route),
        "no usable code or message; falling back to status + route",
    )
