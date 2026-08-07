"""UTF-8 encoding that matches the reference implementation exactly.

CONTRACT.md PRIM-013. Every place this SDK counts or slices UTF-8 bytes
goes through :func:`to_utf8`; none of them call ``str.encode`` directly.

Why this file exists
--------------------

Python and JavaScript disagree about what to do with an unpaired surrogate,
and they disagree *silently*, in the byte count:

    "\\ud83d".encode("utf-8", "replace")   -> b"?"          1 byte
    Buffer.from("\\ud83d", "utf8")          -> <ef bf bd>    3 bytes

Python's ``errors="replace"`` substitutes an ASCII question mark when
ENCODING (it only produces U+FFFD when decoding), while JS substitutes
U+FFFD in both directions. Left alone that makes ``bodySize``,
``content.size`` and the truncation point differ between the two SDKs for
any string containing a stray surrogate, and ``errors="strict"`` would
raise ``UnicodeEncodeError`` on the request path, which SAFETY-001 forbids
outright.

A surrogate code point in a Python ``str`` is by construction unpaired:
astral characters are stored as single code points, never as pairs. So
replacing every U+D800..U+DFFF with U+FFFD and then encoding strictly is
exactly what JS does, with no special-casing needed.

These do turn up in practice, not just under fuzzing: JSON payloads can
carry lone ``\\uD83D`` escapes, and ``surrogateescape`` decoding of
malformed bytes (which WSGI servers use for header values) puts surrogates
straight into a ``str``.
"""

import re

_SURROGATES = re.compile("[\ud800-\udfff]")
_REPLACEMENT = "�"


def to_utf8(s: str) -> bytes:
    """Encode to UTF-8, mapping unpaired surrogates to U+FFFD."""
    if _SURROGATES.search(s):
        s = _SURROGATES.sub(_REPLACEMENT, s)
    return s.encode("utf-8")


def utf8_len(s: str) -> int:
    """Length in UTF-8 bytes (PRIM-011)."""
    return len(to_utf8(s))


def escape_lone_surrogates(s: str) -> str:
    """PRIM-034. Emit unpaired surrogates as \\uXXXX escapes.

    ``json.dumps(..., ensure_ascii=False)`` writes a lone surrogate as a raw
    character, producing a ``str`` that cannot even be UTF-8 encoded.
    JavaScript's well-formed ``JSON.stringify`` (ES2019) escapes it instead.
    Applied after serialization so PRIM-032 still holds for every ordinary
    non-ASCII character.
    """
    if not _SURROGATES.search(s):
        return s
    return _SURROGATES.sub(lambda m: "\\u{:04x}".format(ord(m.group())), s)
