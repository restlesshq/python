# Conformance

| | |
|---|---|
| **Spec version** | 1.3.0 |
| **Level** | L2 (core + batching, caches, injection, safety) |
| **Reference** | `restlesshq/node` (`@restlessai/sdk`) |
| **Driver** | `python -m restless._conformance` |

Declared in `src/restless/_version.py` (META-001).

## Verifying

The harness and vectors live in the reference SDK, so the commands below
assume it is checked out as a sibling (`../node`), which is how
`setup.sh` in the install repo arranges things. The vectors in `spec/` here
are a pinned copy, so `python -m unittest` alone works without it.


```sh
# in-process, no Node needed
PYTHONPATH=src python -m unittest discover -s tests

# the shared cross-language harness
PYTHONPATH=src node ../node/spec/harness/run-vectors.mjs \
  -- python -m restless._conformance

# differential fuzz against the reference implementation
PYTHONPATH=src node ../node/spec/harness/fuzz.mjs \
  --ref  "node ../node/spec/driver/.build/node.js" \
  --test "python -m restless._conformance" \
  --iterations 20000
```

Current status: **252 vectors, 245 passed, 0 failed, 7 skipped.** Zero
divergence across ~98,000 fuzz comparisons on five seeds (24301, 991, 70117,
8675309, 42), sampling ops included.

The 7 skips are the `fp/stack-*` cases, which feed a v8-shaped stack into
`fingerprint`. FP-044 makes frame parsing per-language and FP-046 requires
the driver to report those as an unsupported dialect rather than guess. They
are covered natively in `tests/test_stack_frames.py`.

## Python-specific decisions

Each of these is a place where the obvious Python code silently disagrees
with the reference. They are the reason this SDK is byte-compatible.

| Contract | What Python needs |
|---|---|
| PRIM-001, PRIM-003 | Every contract regex compiles with `re.ASCII`. Python's `\w`, `\d` and `\b` are Unicode-aware by default, which would change fingerprints for any non-ASCII message. |
| PRIM-002 | `WS` is enumerated in `_fingerprint._WS_CHARS`. Neither `\s` nor `re.ASCII`-`\s` is the right set. |
| PRIM-005 | `re.fullmatch` everywhere, never `^...$`. Python's `$` also matches before a trailing newline, so `"5\n"` would normalize to `:id` here and not in JS. |
| PRIM-006 | Hex digits validated explicitly. `int(s, 16)` strips Unicode whitespace and accepts underscores, so `%D` + U+2009 would decode as a byte. |
| PRIM-013 | `_text.to_utf8` maps unpaired surrogates to U+FFFD before encoding. `errors="replace"` emits a 1-byte `?` on encode where JS emits 3-byte U+FFFD; `errors="strict"` raises on the request path. |
| PRIM-030, PRIM-032 | `json.dumps(separators=(",", ":"), ensure_ascii=False)`. |
| PRIM-034 | `_text.escape_lone_surrogates` after serializing. `ensure_ascii=False` writes a raw surrogate, producing a string that cannot be UTF-8 encoded at all. |
| PRIM-040 | Hand-built timestamp. `datetime.isoformat()` emits microseconds and `+00:00`, which the ingest silently rejects and replaces with server time. |
| REDACT-010 | `str.lower()` for header, body-key and query-param names, the same full Unicode mapping as message normalization. This is a security requirement, not a style choice: `toKen` spelled with U+212A KELVIN SIGN lowercases to the denylisted `token` and must be redacted, and an ASCII-only fold leaves it unmatched and ships the secret. An ASCII-only fold is the easy mistake here, and it silently ships the secret. |
| BATCH-008 | Test-runner detection keys on `PYTEST_CURRENT_TEST` and friends. |
| BATCH-009, BATCH-010 | Each upload is a daemon thread, started only while fewer than 4 are running, under one `threading.Condition` that `flush()` waits on. Daemon, so a retrying upload never holds up interpreter exit; call `flush()` to wait for it. |
| WIRE-026 | The `transport` hook returns the body on a 2xx and raises on anything else. `urllib.error.HTTPError` is how it reports a status and `Retry-After`; any other exception is a connection failure, retried only when it is `ConnectionRefusedError` or `socket.gaierror` (bare or as a `URLError` reason). Jitter draws from its own `random.Random`, so a retry never consumes a draw from the module-level generator sampling decides with (SAMPLE-032). |
| WIRE-028 | `urlopen(timeout=10)`. urllib applies it to each blocking socket operation (connect, each read), not to the whole request. That bounds a stalled ingest at 10 s; an ingest that keeps trickling bytes could hold one upload longer. Enforcing a true wall-clock deadline would mean closing the socket from another thread, which the standard library does not expose for `urlopen`. |
| SAMPLE-001 | `isinstance(x, bool)` is checked before the number check. `bool` is an `int` subclass, so JSON `true` would otherwise pass as the rate 1. |
| SAMPLE-002 | `math.floor(r * 10000 + 0.5)`, never `round()`, which rounds half to even. |
| SAMPLE-010, SAMPLE-021 | Methods are uppercased with an ASCII-only table. `str.upper()` turns `"ß"` into `"SS"`. Key and path regexes use `re.fullmatch`, for the PRIM-005 reason. |
| SAMPLE-032 | One `random.random()` draw per request, in `CaptureEngine.sample()`. WSGI decides after the response is buffered, before calling the server's `start_response`; ASGI decides on `http.response.start`, so a streamed response is decided before its first header leaves. |
| FP-044 | Stack frames are `File "...", line N, in fn`; skips `site-packages`, `dist-packages`, `<frozen`, `/lib/python`. |
