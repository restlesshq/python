"""Python conformance driver. See node-sdk/spec/driver/PROTOCOL.md.

Dev-only: not part of the public API, not imported by customer code.

    python -m restless._conformance

Reads JSON Lines on stdin, writes JSON Lines on stdout. The body is a thin
shell over the real modules in ``restless``; anything it reimplemented would
be testing itself instead of the SDK.
"""

import json
import re
import sys
from typing import Any, Dict

from .._fingerprint import (
    _fallback_key,
    fingerprint,
    normalize_message,
    normalize_route,
    project_relative,
)
from .._har import to_har_entry
from .._injection import recovery_slug
from .._mask import mask
from .._redact import (
    redact_body,
    redact_headers,
    redact_url,
    redact_value,
    truncate_body,
)
from .._request_id import (
    format_request_id,
    request_id_response_headers,
    strip_request_id_prefix,
)

# FP-044. The stack strategy's frame format is language-specific: only the
# OUTPUT shape is contract surface. The shared vectors carry v8-shaped
# stacks, which a Python traceback parser cannot read, so we report those
# cases as an unsupported dialect. The harness records them as SKIPPED
# rather than failed, and the SDK covers Python tracebacks in its own
# tests (tests/test_stack_frames.py).
_V8_FRAME_RE = re.compile(r"^\s*at\s+\S", re.MULTILINE)


class UnsupportedDialect(Exception):
    pass


def _op_fingerprint(i: Dict[str, Any]) -> Dict[str, str]:
    stack = i.get("stackTrace")
    if stack:
        text = stack if isinstance(stack, str) else "\n".join(str(s) for s in stack)
        if _V8_FRAME_RE.search(text):
            raise UnsupportedDialect(
                "unsupported stack dialect: v8 (this SDK parses Python tracebacks; FP-044)"
            )
    fp = fingerprint(
        status=i["status"],
        method=i.get("method"),
        route=i.get("route"),
        response_headers=i.get("responseHeaders"),
        response_body=i.get("responseBody"),
        stack_trace=stack,
    )
    # FP-003: `reason` is prose, not contract surface. FP-047's previousKey
    # is, and is emitted only when the stack strategy displaced something.
    out = {"strategy": fp.strategy, "key": fp.key}
    if fp.previous_key:
        out["previousKey"] = fp.previous_key
    return out


OPS = {
    "mask": lambda i: mask(i.get("apiKey")),
    "redactValue": lambda i: redact_value(i["value"]),
    "redactHeaders": lambda i: redact_headers(i["headers"], i.get("extra")),
    "redactUrl": lambda i: redact_url(i["url"], i.get("extra")),
    "redactBody": lambda i: redact_body(
        i.get("body"), i.get("contentType"), i.get("extra")
    ),
    "truncateBody": lambda i: truncate_body(i.get("body"), i["maxBytes"]),
    "fingerprint": _op_fingerprint,
    "normalizeRoute": lambda i: normalize_route(i.get("route")),
    "normalizeMessage": lambda i: normalize_message(i.get("message")),
    "projectRelative": lambda i: project_relative(i["file"]),
    # FP-047's derivation, dialect-free: every case reaching it through
    # `fingerprint` carries a v8 stack this SDK must skip (FP-046).
    "fallbackKey": lambda i: _fallback_key(
        i["status"], i.get("method") or "GET", i.get("route"), i.get("responseBody")
    ),
    "formatRequestId": lambda i: format_request_id(i["rawId"], i.get("prefix")),
    "stripRequestIdPrefix": lambda i: strip_request_id_prefix(i["requestId"]),
    "requestIdHeaders": lambda i: request_id_response_headers(
        i["ourId"], i.get("incomingHeaders") or {}, i.get("prefix"), i["hasApiKey"]
    ),
    "recoverySlug": lambda i: recovery_slug(i.get("method"), i.get("path")),
    "harEntry": lambda i: to_har_entry(i["captured"]),
}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            op = req.get("op")
            fn = OPS.get(op)
            if fn is None:
                raise KeyError("unknown op: {}".format(op))
            out = {"id": req_id, "result": fn(req.get("input") or {})}
        except Exception as err:  # noqa: BLE001 - the protocol reports errors
            out = {"id": req_id, "error": "{}".format(err)}
        # ensure_ascii=True here, deliberately. This is the DRIVER's own
        # transport, not a captured body: PRIM-032's "emit UTF-8 literally"
        # governs payloads, while stdout has to survive values the fuzzer
        # generates - an unpaired surrogate cannot be UTF-8 encoded at all
        # and would kill the process mid-run. JSON \uXXXX escapes carry it
        # losslessly and the harness decodes it back.
        sys.stdout.write(
            json.dumps(out, separators=(",", ":"), ensure_ascii=True) + "\n"
        )
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
