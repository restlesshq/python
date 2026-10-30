"""Upload backpressure: CONTRACT.md WIRE-026..028 and BATCH-005, 009, 010.

No vector reaches the uploader, so these drive it directly through an
injected transport, plus one real HTTP round trip through the default one.
"""

import email.utils
import http.server
import io
import json
import os
import socket
import threading
import time
import unittest
import urllib.error
from unittest import mock

from restless import _uploader
from restless._uploader import Uploader

REMOTE = "https://remote.example"
LOCAL = "http://localhost:3003"


def captured(request_id):
    return {
        "requestId": request_id,
        "startedAt": "2026-01-01T00:00:00.000Z",
        "request": {"method": "GET", "url": "http://localhost/test", "headers": {}},
        "response": {"status": 200, "headers": {}},
        "duration": 1,
    }


def http_error(status, retry_after=None, body=b"busy"):
    headers = email.message_from_string(
        "Retry-After: {}\n\n".format(retry_after) if retry_after is not None else "\n"
    )
    return urllib.error.HTTPError(REMOTE, status, "refused", headers, io.BytesIO(body))


class Transport(object):
    """Answers each POST with the next scripted outcome: bytes, an exception,
    or a threading.Event that blocks the upload until it is set."""

    def __init__(self, *outcomes, default=b"{}"):
        self.outcomes = list(outcomes)
        self.default = default
        self.calls = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def __call__(self, url, body, headers):
        with self.lock:
            self.calls.append(json.loads(body.decode("utf-8")))
            outcome = self.outcomes.pop(0) if self.outcomes else self.default
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if callable(outcome):
                outcome = outcome()
            if isinstance(outcome, threading.Event):
                outcome.wait(5)
                return b"{}"
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        finally:
            with self.lock:
                self.active -= 1

    def sizes(self):
        with self.lock:
            return [len(c) for c in self.calls]

    def wait_for_calls(self, count, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline and len(self.sizes()) < count:
            time.sleep(0.002)
        return self.sizes()


class UploaderTestCase(unittest.TestCase):
    def setUp(self):
        # Setup mode lifts the test-runner gate (BATCH-008) so uploads happen.
        patcher = mock.patch.dict(os.environ, {"RESTLESS_SETUP_MODE": "1", "RESTLESS_ENV": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sleeps = []
        sleeper = mock.patch.object(_uploader, "_sleep", side_effect=self.sleeps.append)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def production(self):
        patcher = mock.patch.dict(os.environ, {"RESTLESS_ENV": "production"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def upload_one(self, transport, base_url=LOCAL):
        up = Uploader(api_key="k", base_url=base_url, transport=transport)
        up.push(captured("a"))
        up.flush()
        return up


class TestCap(UploaderTestCase):
    def test_never_more_than_4_in_flight_and_the_backlog_goes_100_at_a_time(self):
        self.production()
        gate = threading.Event()
        transport = Transport(*[gate] * 4)
        up = Uploader(api_key="k", base_url=REMOTE, transport=transport)

        for i in range(300):
            up.push(captured("r-{}".format(i)))
        self.assertEqual(transport.wait_for_calls(4), [10, 10, 10, 10])
        time.sleep(0.02)
        self.assertEqual(len(transport.sizes()), 4)

        # Each freed slot takes up to 100 of what queued behind the cap.
        gate.set()
        self.assertEqual(sorted(transport.wait_for_calls(7)[4:]), [60, 100, 100])

        up.flush()
        self.assertEqual(sum(transport.sizes()), 300)
        self.assertLessEqual(transport.max_active, _uploader.MAX_IN_FLIGHT)

    def test_a_partial_batch_waits_for_the_timer_or_a_flush(self):
        self.production()
        transport = Transport()
        up = Uploader(api_key="k", base_url=REMOTE, transport=transport)
        for i in range(5):
            up.push(captured("r-{}".format(i)))
        time.sleep(0.02)
        self.assertEqual(transport.sizes(), [])
        up.flush()
        self.assertEqual(transport.sizes(), [5])

    def test_flush_drains_more_than_the_cap_holds(self):
        self.production()
        transport = Transport()
        up = Uploader(api_key="k", base_url=REMOTE, transport=transport)
        with up._cond:  # queue everything before any slot can take it
            up._queue.extend(captured("r-{}".format(i)) for i in range(950))
        up.flush()
        self.assertEqual(sum(transport.sizes()), 950)
        self.assertTrue(all(n <= _uploader.MAX_UPLOAD_ENTRIES for n in transport.sizes()))

    def test_flush_with_no_key_drops_the_queue(self):
        transport = Transport()
        up = Uploader(api_key="", base_url=LOCAL, transport=transport)
        up.push(captured("a"))
        up.flush()
        self.assertEqual(transport.sizes(), [])
        self.assertEqual(up._queue, [])


class TestRetries(UploaderTestCase):
    def test_503_and_429_are_retried_with_the_same_body_honouring_retry_after(self):
        transport = Transport(http_error(503, "2"), http_error(429, "0"), b"{}")
        self.upload_one(transport)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(len({json.dumps(c) for c in transport.calls}), 1)
        self.assertEqual(self.sleeps, [2.0, 0.0])

    def test_retry_after_as_an_http_date(self):
        when = email.utils.formatdate(time.time() + 5, usegmt=True)
        self.upload_one(Transport(http_error(503, when), b"{}"))
        [wait] = self.sleeps
        self.assertTrue(3.0 <= wait <= 5.0, wait)

    def test_every_wait_is_capped_at_10s(self):
        self.upload_one(Transport(http_error(503, "120"), b"{}"))
        self.assertEqual(self.sleeps, [10.0])

    def test_backoff_without_retry_after_is_jittered_and_exponential(self):
        self.upload_one(Transport(http_error(503), http_error(503, "soon"), b"{}"))
        first, second = self.sleeps
        self.assertTrue(0.5 <= first <= 1.5, first)
        self.assertTrue(1.0 <= second <= 3.0, second)

    def test_gives_up_after_2_retries(self):
        transport = Transport(default=lambda: http_error(503, "0"))
        self.upload_one(transport)
        self.assertEqual(len(transport.calls), 3)

    def test_retries_a_connection_that_never_reached_the_ingest(self):
        for reason in (ConnectionRefusedError(61, "refused"), socket.gaierror(8, "no such host")):
            with self.subTest(reason=type(reason).__name__):
                transport = Transport(urllib.error.URLError(reason), b"{}")
                self.upload_one(transport)
                self.assertEqual(len(transport.calls), 2)

    def test_does_not_retry_what_the_ingest_may_have_stored(self):
        cases = {
            "502": http_error(502),
            "504": http_error(504),
            "500": http_error(500),
            "timeout": socket.timeout("timed out"),
            "connect timeout": urllib.error.URLError(socket.timeout("timed out")),
            "reset after send": ConnectionResetError(54, "reset"),
            "disconnected": urllib.error.URLError(ConnectionResetError(54, "reset")),
        }
        for label, outcome in cases.items():
            with self.subTest(label):
                transport = Transport(outcome)
                self.upload_one(transport)
                self.assertEqual(len(transport.calls), 1)

    def test_a_retrying_upload_keeps_its_slot_and_flush_waits_for_it(self):
        transport = Transport(http_error(503, "0"), b'{"docsUrl":"x"}')
        responses = []
        up = Uploader(
            api_key="k", base_url=LOCAL, transport=transport,
            on_response=lambda body, fps: responses.append(body),
        )
        up.push(captured("a"))
        up.flush()
        self.assertEqual(responses, [{"docsUrl": "x"}])
        self.assertEqual(up._in_flight, 0)

    def test_a_failure_reading_the_error_body_never_escapes(self):
        err = http_error(500)
        err.read = mock.Mock(side_effect=OSError("body stream broke"))
        hook = mock.Mock()
        with mock.patch.object(threading, "excepthook", hook):
            self.upload_one(Transport(err))
        hook.assert_not_called()


class TestDefaultTransport(UploaderTestCase):
    def test_every_upload_has_the_10s_timeout(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b"{}"
            self.upload_one(None)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 10.0)

    def test_a_real_503_with_retry_after_is_retried(self):
        hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                hits.append(self.path)
                if len(hits) == 1:
                    self.send_response(503)
                    self.send_header("Retry-After", "1")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b"{}"
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        self.upload_one(None, base_url="http://127.0.0.1:{}".format(server.server_port))
        self.assertEqual(hits, ["/v1/request", "/v1/request"])
        self.assertEqual(self.sleeps, [1.0])


if __name__ == "__main__":
    unittest.main()
