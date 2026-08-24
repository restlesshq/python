# Conformance

| | |
|---|---|
| **Spec version** | 1.0.0 |
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

Current status: **206 vectors, 199 passed, 0 failed, 7 skipped.** Zero
divergence across ~38,900 fuzz comparisons on five seeds (24301, 991, 70117,
8675309, 42).

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
| FP-044 | Stack frames are `File "...", line N, in fn`; skips `site-packages`, `dist-packages`, `<frozen`, `/lib/python`. |
