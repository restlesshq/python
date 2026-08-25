"""The capture engine: the single choke point every adapter goes through.

Implements CONTRACT.md sections 10, 11 and 13.
"""

import datetime
import inspect
from typing import Any, Callable, Dict, List, Optional

from ._caches import Blocklist, EnrichCache, RecoveryCache
from ._fingerprint import Fingerprint, fingerprint
from ._injection import apply_internal_body_mods, build_debug_injection, debug_headers
from ._redact import redact_body, redact_headers, redact_url, truncate_body
from ._uploader import Uploader, _debug

#: REDACT-030. 256 KiB, in UTF-8 bytes.
MAX_BODY_BYTES = 256 * 1024


def now_iso() -> str:
    """PRIM-040. Exactly three fractional digits and a literal Z.

    NOT ``datetime.isoformat()``, which emits microseconds and ``+00:00``.
    The ingest parses this permissively and silently falls back to server
    time when it cannot, so a wrong format loses real request timing with no
    error anywhere in the system.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    return "{}.{:03d}Z".format(now.strftime("%Y-%m-%dT%H:%M:%S"), now.microsecond // 1000)


class CaptureEngine:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        request_id_prefix: Optional[str] = None,
        redact: Optional[Dict[str, List[str]]] = None,
        transport: Optional[Callable] = None,
    ):
        self.blocklist = Blocklist()
        self.enrich_cache = EnrichCache()
        self.recovery_cache = RecoveryCache()
        self._redact = redact or {}
        self._callback: Optional[Callable] = None
        self._portal_url: Optional[str] = None
        self.uploader = Uploader(
            api_key=api_key,
            base_url=base_url,
            request_id_prefix=request_id_prefix,
            on_response=self._handle_server_response,
            transport=transport,
        )

    # ---------------------------------------------------------------- setup

    def set_callback(self, callback: Callable) -> None:
        self._callback = callback

    @property
    def portal_url(self) -> Optional[str]:
        """INJECT-006. The server-published portal origin every injected URL
        is built on. None before the first upload round-trip, and then
        nothing is emitted rather than a guess."""
        return self._portal_url

    @property
    def request_id_prefix(self) -> Optional[str]:
        return self.uploader.request_id_prefix

    @property
    def base_url(self) -> str:
        return self.uploader.base_url

    @property
    def has_api_key(self) -> bool:
        return self.uploader.has_api_key

    # ------------------------------------------------------- server signals

    def _handle_server_response(self, body: Any, batch_fingerprints: List[str]) -> None:
        """WIRE-021..023, CACHE-006, CACHE-012, CACHE-013."""
        if not isinstance(body, dict):
            return

        needs = body.get("needsEnrichment")
        if isinstance(needs, list):
            for key in needs:
                if isinstance(key, str):
                    self.enrich_cache.invalidate(key)

        # The wire key stays `docsUrl`: every already-deployed SDK reads it,
        # so renaming would strand them all with no portal origin (WIRE-023).
        docs = body.get("docsUrl")
        if isinstance(docs, str) and docs:
            self._portal_url = docs.rstrip("/")

        messages = body.get("recoveryMessages")
        if not isinstance(messages, dict):
            messages = {}
        for key in batch_fingerprints:
            value = messages.get(key, ...)
            if isinstance(value, str):
                self.recovery_cache.set(key, value)
            elif value is None and key in messages:
                self.recovery_cache.set(key, None)
            elif self.recovery_cache.get(key) is RecoveryCache._MISS:
                # CACHE-012 with CACHE-013: negative-cache what the server
                # did not answer, but never clobber a positive entry.
                self.recovery_cache.set(key, None)

    def lookup_recovery(self, fingerprint_key: str) -> Optional[str]:
        """CACHE-010. Synchronous, in-process, never touches the network."""
        return self.recovery_cache.lookup(fingerprint_key)

    # ----------------------------------------------------------- per-request

    def compute_fingerprint(self, captured: Dict[str, Any]) -> Optional[Fingerprint]:
        """FP-002. Errors only."""
        response = captured["response"]
        if response["status"] < 400:
            return None
        parsed: Any = response.get("body")
        if isinstance(parsed, str):
            try:
                import json

                parsed = json.loads(parsed)
            except ValueError:
                pass  # fingerprint() handles a raw string too
        return fingerprint(
            status=response["status"],
            method=captured["request"]["method"],
            route=captured.get("routePattern"),
            response_headers=response.get("headers"),
            response_body=parsed,
            stack_trace=captured.get("stackTrace"),
        )

    def resolve(self, request: Any) -> Dict[str, Any]:
        """SETUP-001..005, CACHE-001..007, SAFETY-002."""
        if self._callback is None:
            return {}
        try:
            result = self._callback(request) or {}
        except Exception:  # noqa: BLE001 - SAFETY-002
            return {}
        if not isinstance(result, dict):
            return {}

        rest = {k: v for k, v in result.items() if k not in ("owner", "project")}
        raw = result.get("owner") or result.get("project")
        if not raw:
            return rest

        # SETUP-003. `id` is the only field carried off the raw owner; all
        # other metadata flows through enrich.
        owner_id = raw.get("id")
        enrich = raw.get("enrich")
        base = {"id": owner_id} if owner_id is not None else {}
        cache_key = owner_id or rest.get("apiKey")

        if callable(enrich) and owner_id and cache_key:
            cached = self.enrich_cache.get(cache_key)
            if cached is not None:
                merged = dict(base)
                merged.update(cached)
                return dict(rest, project=merged)
            try:
                enriched = enrich(owner_id)
                if inspect.isawaitable(enriched):  # pragma: no cover
                    enriched = None  # async enrich is resolved by the ASGI adapter
                if isinstance(enriched, dict):
                    self.enrich_cache.set(cache_key, enriched)
                    merged = dict(base)
                    merged.update(enriched)
                    return dict(rest, project=merged)
            except Exception:  # noqa: BLE001 - CACHE-005, SAFETY-003
                pass  # failures are NOT cached; the next request retries

        # CACHE-007. Ship the bare id so the dashboard can still group by it.
        return dict(rest, project=base)

    def record(self, captured: Dict[str, Any]) -> None:
        """The single choke point. Redact, truncate, fingerprint, enqueue."""
        request = captured["request"]
        response = captured["response"]
        redact_opts = self._redact

        sanitized = dict(captured)
        sanitized["request"] = dict(
            request,
            url=redact_url(request["url"], redact_opts.get("queryParams")),
            headers=redact_headers(request.get("headers") or {}, redact_opts.get("headers")),
            # REDACT-033: truncation runs AFTER redaction, so a secret cannot
            # survive by sitting past the byte limit.
            body=truncate_body(
                redact_body(
                    request.get("body"),
                    (request.get("headers") or {}).get("content-type"),
                    redact_opts.get("bodyKeys"),
                ),
                MAX_BODY_BYTES,
            ),
        )
        sanitized["response"] = dict(
            response,
            headers=redact_headers(response.get("headers") or {}, redact_opts.get("headers")),
            body=truncate_body(
                redact_body(
                    response.get("body"),
                    (response.get("headers") or {}).get("content-type"),
                    redact_opts.get("bodyKeys"),
                ),
                MAX_BODY_BYTES,
            ),
        )

        # INJECT-010. Reuse the fingerprint the adapter already computed for
        # its recovery lookup rather than repeating the work.
        if not sanitized.get("errorFingerprint") and response["status"] >= 400:
            fp = self.compute_fingerprint(sanitized)
            if fp is not None:
                sanitized["errorFingerprint"] = fp.to_wire()
        sanitized.pop("stackTrace", None)

        try:
            self.uploader.push(sanitized)
        except Exception as err:  # noqa: BLE001 - SAFETY-001
            _debug("record failed:", err)

    def flush(self) -> None:
        self.uploader.flush()

    # ------------------------------------------------------------ injection

    def build_injection(
        self,
        status: int,
        request_id: str,
        method: Optional[str],
        route: Optional[str],
        raw_body: Optional[str],
        content_type: Optional[str],
        response_headers: Dict[str, str],
    ):
        """INJECT-001..009. Returns (headers, new_body, fingerprint_wire).

        The fingerprint is computed against the customer's RAW response,
        before any injected header or body field is layered on (INJECT-009).
        """
        # INJECT-001. The headers ship on every status; only the body work
        # below is 4xx/5xx, and fingerprinting a success would be wasted.
        if status < 400:
            return (
                debug_headers(request_id, self.request_id_prefix, self.portal_url),
                raw_body,
                None,
            )

        fp = self.compute_fingerprint(
            {
                "request": {"method": method or "GET", "url": "", "headers": {}},
                "response": {
                    "status": status,
                    "headers": response_headers,
                    "body": raw_body,
                },
                "routePattern": route,
            }
        )
        recovery = self.lookup_recovery(fp.key) if fp else None
        headers, mutate = build_debug_injection(
            status=status,
            request_id=request_id,
            prefix=self.request_id_prefix,
            recovery=recovery,
            method=method,
            path=route,
            portal_url=self.portal_url,
        )
        new_body = apply_internal_body_mods(raw_body, content_type, mutate)
        return headers, new_body, (fp.to_wire() if fp else None)
