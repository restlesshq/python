"""Batched uploader.

Implements CONTRACT.md sections 8 (wire format) and 9 (batching).

Never raises to callers. Upload errors go to stderr under the debug flag and
are otherwise swallowed: observability must not break the request path
(SAFETY-004).
"""

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from ._har import to_har_entry
from ._text import escape_lone_surrogates
from ._version import SDK_NAME, SDK_VERSION, SPEC_VERSION

DEFAULT_BASE_URL = "https://ingress.restless.ai"

# BATCH-001, BATCH-002, BATCH-004
BATCH_SIZE = 10
FLUSH_INTERVAL_MS = 5000
MAX_QUEUE = 1000


def debug_enabled() -> bool:
    """CONFIG-004."""
    flag = os.environ.get("DEBUG", "")
    return flag == "*" or flag == "restless" or "restless" in flag.replace(",", " ").split()


def _debug(*args: Any) -> None:
    if debug_enabled():
        print("[restless]", *args, file=sys.stderr)


def resolve_base_url(explicit: Optional[str] = None) -> str:
    """CONFIG-003, WIRE-005."""
    return explicit or os.environ.get("RESTLESS_BASE_URL") or DEFAULT_BASE_URL


def is_test_run() -> bool:
    """BATCH-008, per-language.

    The Node reference keys on NODE_ENV=test, VITEST, JEST_WORKER_ID,
    NODE_TEST_CONTEXT and AVA_PATH. These are the Python equivalents.
    """
    if os.environ.get("RESTLESS_ENV") == "test":
        return True
    if os.environ.get("PYTEST_CURRENT_TEST") is not None:
        return True
    if os.environ.get("PYTEST_VERSION") is not None:
        return True
    if "pytest" in sys.modules or "unittest" in sys.modules and _running_under_unittest():
        return True
    if os.environ.get("NOSE_config") is not None:
        return True
    return False


def _running_under_unittest() -> bool:
    argv0 = (sys.argv[0] if sys.argv else "") or ""
    return argv0.endswith("unittest") or "unittest" in argv0.split(os.sep)[-1:]


def _is_localhost(base_url: str) -> bool:
    return "//localhost" in base_url or "//127.0.0.1" in base_url


def _should_flush_immediately(base_url: str) -> bool:
    """BATCH-003. Keep the customer developer loop and self-hosted setups
    low-latency; batch only in production against a remote ingest."""
    if os.environ.get("RESTLESS_ENV", os.environ.get("ENV", "")) != "production":
        return True
    return _is_localhost(base_url)


class Uploader:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        request_id_prefix: Optional[str] = None,
        on_response: Optional[Callable[[Any, List[str]], None]] = None,
        transport: Optional[Callable[[str, bytes, Dict[str, str]], Optional[bytes]]] = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.request_id_prefix = request_id_prefix
        self._on_response = on_response
        self._transport = transport or _http_post
        self._queue: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._warn_if_insecure()

    def _warn_if_insecure(self) -> None:
        """WIRE-006. One-shot warning: the project key and every captured
        header would otherwise ship in the clear with no signal at all."""
        if self.base_url.startswith("http://") and not _is_localhost(self.base_url):
            print(
                "[restless] RESTLESS_BASE_URL={} is plain HTTP - your API key and "
                "every captured header will be transmitted unencrypted. Use https:// "
                "or localhost.".format(self.base_url),
                file=sys.stderr,
            )

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def push(self, captured: Dict[str, Any]) -> None:
        # BATCH-008
        if is_test_run() and os.environ.get("RESTLESS_SETUP_MODE") != "1":
            return

        with self._lock:
            # BATCH-004. Drop the OLDEST: the newest entries are the ones an
            # operator is actively debugging.
            if len(self._queue) >= MAX_QUEUE:
                self._queue.pop(0)
                _debug("queue at {} - dropping oldest captured request".format(MAX_QUEUE))
            self._queue.append(captured)
            should_flush = (
                _should_flush_immediately(self.base_url) or len(self._queue) >= BATCH_SIZE
            )
            if not should_flush and self._timer is None:
                self._timer = threading.Timer(FLUSH_INTERVAL_MS / 1000.0, self._flush_async)
                self._timer.daemon = True
                self._timer.start()

        if should_flush:
            self._flush_async()

    def _flush_async(self) -> None:
        """SAFETY-008. Uploads never block the request path."""
        thread = threading.Thread(target=self.flush, daemon=True)
        thread.start()

    def flush(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if not self._queue:
                return
            # BATCH-007. No key means drop the batch; never accumulate.
            if not self.api_key:
                _debug("no API key - dropping batch")
                self._queue.clear()
                return
            batch = self._queue
            self._queue = []

        batch_fingerprints: List[str] = []
        seen = set()
        for captured in batch:
            fp = captured.get("errorFingerprint")
            if not isinstance(fp, dict):
                continue
            key = fp.get("key")
            if key and key not in seen:
                seen.add(key)
                batch_fingerprints.append(key)

        payload = [self._entry(c) for c in batch]
        url = "{}/v1/request".format(self.base_url)
        _debug("uploading {} entr{} to {}".format(
            len(batch), "y" if len(batch) == 1 else "ies", url))

        try:
            body = escape_lone_surrogates(
                json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            ).encode("utf-8")
            raw = self._transport(
                url,
                body,
                {
                    "Content-Type": "application/json",  # WIRE-002
                    "Authorization": "Bearer {}".format(self.api_key),  # WIRE-003
                    "X-Restless-Spec-Version": SPEC_VERSION,  # META-002
                },
            )
        except Exception as err:  # noqa: BLE001 - SAFETY-004
            _debug("upload error:", err)
            return

        # WIRE-020. A non-JSON or unparseable response is ignored, not an error.
        if raw and self._on_response:
            try:
                self._on_response(json.loads(raw.decode("utf-8")), batch_fingerprints)
            except Exception:  # noqa: BLE001
                pass

    def _entry(self, captured: Dict[str, Any]) -> Dict[str, Any]:
        """WIRE-010..018."""
        user = captured.get("user") or {}
        owner = user.get("project") or {}

        # WIRE-013. Always an array on the wire, whether the user gave us a
        # single string or a list.
        raw_email = owner.get("email")
        if isinstance(raw_email, str):
            emails = [raw_email]
        elif isinstance(raw_email, (list, tuple)):
            emails = list(raw_email)
        else:
            emails = []

        entry: Dict[str, Any] = {
            "_id": captured["requestId"],  # WIRE-011: raw uuid, no prefix
            "routePattern": captured.get("routePattern"),
            "errorFingerprint": captured.get("errorFingerprint"),
            # WIRE-012. owner id, else the masked key, else "anonymous".
            "group": {
                "id": owner.get("id") or user.get("apiKey") or "anonymous",
                "label": owner.get("label") or "",
                "emails": emails,
            },
            "apiKey": user.get("apiKey"),  # WIRE-014
            "projectId": owner.get("id"),  # WIRE-015
            "clientIPAddress": "127.0.0.1",  # WIRE-018, reserved
            "development": False,  # WIRE-018, reserved
            "request": {
                "log": {
                    "version": "1.2",
                    "creator": {"name": SDK_NAME, "version": SDK_VERSION},  # WIRE-016
                    "entries": [to_har_entry(captured)],
                }
            },
        }
        return entry


def _http_post(url: str, body: bytes, headers: Dict[str, str]) -> Optional[bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()
    except urllib.error.HTTPError as err:
        # WIRE-024. Never retried, never raised.
        _debug("upload failed: {} {}".format(err.code, err.read()[:500]))
        return None
