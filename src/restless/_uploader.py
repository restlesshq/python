"""Batched uploader.

Implements CONTRACT.md sections 8 (wire format) and 9 (batching).

Never raises to callers. Upload errors go to stderr under the debug flag and
are otherwise swallowed: observability must not break the request path
(SAFETY-004).

Uploads run on daemon threads, at most MAX_IN_FLIGHT at once (BATCH-009).
A custom ``transport(url, body, headers)`` returns the response body on a
2xx and raises on anything else; raising ``urllib.error.HTTPError`` carries
the status and ``Retry-After`` that decide a retry (WIRE-026).
"""

import email.utils
import json
import math
import os
import random
import socket
import sys
import threading
import time
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
# BATCH-009, BATCH-010
MAX_IN_FLIGHT = 4
MAX_UPLOAD_ENTRIES = 100
# WIRE-026, WIRE-028
UPLOAD_TIMEOUT_S = 10.0
MAX_RETRIES = 2
RETRY_BASE_S = 1.0
MAX_RETRY_DELAY_S = 10.0
RETRY_STATUSES = (429, 503)

# Its own generator, so retry jitter never consumes draws from the module-level
# `random` that sampling decides with (SAMPLE-032).
_jitter = random.Random()
_sleep = time.sleep


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
        sampling_hash: Optional[str] = None,
    ):
        self.api_key = api_key
        # SAMPLE-052. Stamped on every upload as `x-restless-sampling`. None
        # when sampling is not configured, and then no header is sent at all.
        self.sampling_hash = sampling_hash
        self.base_url = base_url.rstrip("/")
        self.request_id_prefix = request_id_prefix
        self._on_response = on_response
        self._transport = transport or _http_post
        self._queue: List[Dict[str, Any]] = []
        self._cond = threading.Condition()
        self._in_flight = 0
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
        if _uploads_suppressed():
            return

        with self._cond:
            # BATCH-004. Drop the OLDEST: the newest entries are the ones an
            # operator is actively debugging.
            if len(self._queue) >= MAX_QUEUE:
                self._queue.pop(0)
                _debug("queue at {} - dropping oldest captured request".format(MAX_QUEUE))
            self._queue.append(captured)
            self._schedule()

    def flush(self) -> None:
        """BATCH-005. Upload everything queued, within the in-flight cap, and
        block until it and every upload already running has finished,
        retries included."""
        with self._cond:
            self._cancel_timer()
            while True:
                self._pump(partial=True)
                # Nothing running means nothing queued, or BATCH-007 dropped it.
                if self._in_flight == 0:
                    return
                self._cond.wait()

    # Everything below that touches the queue runs with self._cond held.

    def _schedule(self) -> None:
        """Send now, or make sure a remainder still has a timer (BATCH-001..003)."""
        if not self._queue:
            return
        immediate = _should_flush_immediately(self.base_url)
        if immediate or len(self._queue) >= BATCH_SIZE:
            self._pump(partial=immediate)
        if self._queue and self._timer is None and not immediate:
            self._timer = threading.Timer(FLUSH_INTERVAL_MS / 1000.0, self._on_timer)
            self._timer.daemon = True
            self._timer.start()

    def _on_timer(self) -> None:
        with self._cond:
            self._timer = None
            self._pump(partial=True)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _pump(self, partial: bool) -> None:
        """Start uploads while a slot is free (BATCH-009). Without ``partial``
        only full batches go; a remainder waits for the timer (BATCH-010)."""
        # BATCH-007. No key means drop the batch; never accumulate.
        if not self.api_key:
            if self._queue:
                _debug("no API key - dropping batch")
            self._queue.clear()
            return
        while (
            self._in_flight < MAX_IN_FLIGHT
            and self._queue
            and (partial or len(self._queue) >= BATCH_SIZE)
        ):
            batch = self._queue[:MAX_UPLOAD_ENTRIES]
            del self._queue[:MAX_UPLOAD_ENTRIES]
            # SAFETY-008. Uploads never block the request path.
            thread = threading.Thread(target=self._run_upload, args=(batch,), daemon=True)
            self._in_flight += 1
            try:
                thread.start()
            except Exception as err:  # noqa: BLE001 - SAFETY-001
                self._in_flight -= 1
                _debug("could not start upload:", err)
                return

    def _run_upload(self, batch: List[Dict[str, Any]]) -> None:
        try:
            self._send(batch)
        except Exception as err:  # noqa: BLE001 - SAFETY-004
            _debug("upload error:", err)
        finally:
            with self._cond:
                self._in_flight -= 1
                self._cond.notify_all()
                try:
                    self._schedule()
                except Exception as err:  # noqa: BLE001 - SAFETY-001
                    _debug("upload error:", err)

    def _send(self, batch: List[Dict[str, Any]]) -> None:
        """One upload, holding its slot through any retries. Only what the
        ingest certainly did not store is retried (WIRE-026, WIRE-027)."""
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
        headers = self._headers()
        if self.sampling_hash:
            headers["x-restless-sampling"] = self.sampling_hash  # SAMPLE-052
        body = escape_lone_surrogates(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        ).encode("utf-8")

        retry = 0
        while True:
            _debug("uploading {} entr{} to {}{}".format(
                len(batch), "y" if len(batch) == 1 else "ies", url,
                " (retry {})".format(retry) if retry else ""))
            try:
                raw = self._transport(url, body, headers)
            except urllib.error.HTTPError as err:
                status = err.code
                wait = _retry_delay(err.headers, retry)
                detail = _read_error_body(err)
                if status in RETRY_STATUSES and retry < MAX_RETRIES:
                    _debug("upload refused ({}); retrying in {:.2f}s".format(status, wait))
                    _sleep(wait)
                    retry += 1
                    continue
                _debug("upload failed: {} {}".format(status, detail))
                return
            except Exception as err:  # noqa: BLE001 - SAFETY-004
                if _not_sent(err) and retry < MAX_RETRIES:
                    _sleep(_retry_delay(None, retry))
                    retry += 1
                    continue
                _debug("upload error:", err)
                return

            # WIRE-020. A non-JSON or unparseable response is ignored, not an error.
            if raw and self._on_response:
                try:
                    self._on_response(json.loads(raw.decode("utf-8")), batch_fingerprints)
                except Exception:  # noqa: BLE001
                    pass
            return

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",  # WIRE-002
            "Authorization": "Bearer {}".format(self.api_key),  # WIRE-003
            "X-Restless-Spec-Version": SPEC_VERSION,  # META-002
        }

    def send_sampling_report(self, report: Dict[str, Any]) -> bool:
        """SAMPLE-050, SAMPLE-051. POST the sampling config report.

        Blocking: the engine calls this from a background thread. Same gates
        as an upload, nothing under a test runner or without a project key.
        Returns True when the transport returned without raising; never
        raises, and a failure is not retried.
        """
        if _uploads_suppressed() or not self.api_key:
            return False
        url = "{}/v1/sampling".format(self.base_url)
        try:
            body = json.dumps(report, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self._transport(url, body, self._headers())
            _debug("sampling report sent to", url)
            return True
        except Exception as err:  # noqa: BLE001 - SAFETY-004
            _debug("sampling report error:", err)
            return False

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
        # WIRE-030. Present, and true, only on a sampled stub; never false.
        if captured.get("sampled"):
            entry["sampled"] = True
        return entry


def _uploads_suppressed() -> bool:
    """BATCH-008. RESTLESS_SETUP_MODE lets the setup CLI's own test run
    upload anyway."""
    return is_test_run() and os.environ.get("RESTLESS_SETUP_MODE") != "1"


def _http_post(url: str, body: bytes, headers: Dict[str, str]) -> Optional[bytes]:
    """Raises HTTPError on a non-2xx, which `_send` reads for a retry."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    # WIRE-028. urllib applies this to each blocking socket operation, not
    # to the request as a whole.
    with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT_S) as resp:
        return resp.read()


def _retry_delay(headers: Any, retry: int) -> float:
    """WIRE-026. The server's Retry-After (seconds or an HTTP date), else
    jittered exponential backoff; either way capped."""
    delay: Optional[float] = None
    try:
        value = headers.get("Retry-After") if headers is not None else None
    except Exception:  # noqa: BLE001
        value = None
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(value)
                delay = when.timestamp() - time.time()
            except Exception:  # noqa: BLE001 - not a date either
                delay = None
    if delay is None or not math.isfinite(delay) or delay < 0:
        delay = RETRY_BASE_S * (2 ** retry) * (0.5 + _jitter.random())
    return min(delay, MAX_RETRY_DELAY_S)


def _not_sent(err: BaseException) -> bool:
    """Refused or unresolvable: nothing reached the ingest, so a retry
    cannot store the batch twice (WIRE-026). A timeout or reset may have."""
    reason: Any = err
    if isinstance(err, urllib.error.URLError) and isinstance(err.reason, BaseException):
        reason = err.reason
    return isinstance(reason, (ConnectionRefusedError, socket.gaierror))


def _read_error_body(err: urllib.error.HTTPError) -> str:
    try:
        return repr(err.read()[:500])
    except Exception:  # noqa: BLE001
        return ""
    finally:
        try:
            err.close()
        except Exception:  # noqa: BLE001
            pass
