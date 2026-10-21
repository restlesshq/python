"""Sampling at runtime: CONTRACT.md section 12.1.

The shared vectors pin the pure half (merging, key normalization, lookup,
the decision table and the config hash). This file covers what they cannot:
one decision per request in each adapter, what a drop and a stub look like
on the response and on the wire, and the config report.
"""

import asyncio
import io
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import restless
from restless import _settings
from restless._sampling import normalize_mode, normalize_sampling, resolve_mode

ID_HEADERS = ("x-request-id", "x-restless-id")
SAMPLING = {
    "routes": {
        "GET /health": "off",
        "GET /status": "errors",
        "GET /pets": 0.1,
        "* /pets/{id}": 0.5,
    }
}


class Transport(object):
    """Records every POST the SDK makes, by path."""

    def __init__(self, response=None, fail=False):
        self.calls = []
        self.response = response
        self.fail = fail
        self.lock = threading.Lock()

    def __call__(self, url, body, headers):
        with self.lock:
            self.calls.append((url, json.loads(body.decode("utf-8")), dict(headers)))
        if self.fail:
            raise OSError("ingest unreachable")
        return json.dumps(self.response).encode("utf-8") if self.response else None

    def to(self, path):
        with self.lock:
            return [c for c in self.calls if c[0].endswith(path)]

    def wait_for(self, path, count=1, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.to(path)) >= count:
                return self.to(path)
            time.sleep(0.005)
        return self.to(path)


class SamplingTestCase(unittest.TestCase):
    def setUp(self):
        # Uploads are suppressed under a test runner (BATCH-008); setup mode
        # lifts that so the wire can be inspected. A production env against
        # a remote base URL batches instead of flushing on a thread, so
        # `flush()` below is synchronous.
        env = {"RESTLESS_SETUP_MODE": "1", "RESTLESS_ENV": "production"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        _settings._cached = None  # no .restless/settings.json
        self.addCleanup(_settings._reset_cache)

    def client(self, sampling=SAMPLING, transport=None, api_key="rk_test"):
        self.transport = transport or Transport()
        return restless.Restless(
            api_key,
            base_url="https://ingest.example",
            transport=self.transport,
            sampling=sampling,
        )

    def uploaded(self, client):
        client.flush()
        entries = []
        for _, payload, _ in self.transport.to("/v1/request"):
            entries.extend(payload)
        return entries


# --------------------------------------------------------------------- WSGI


def wsgi_app(status=200, body=b'{"ok":true}', route=None, raises=False,
             content_type="application/json"):
    def app(environ, start_response):
        if route is not None:
            environ["restless.route"] = route
        if raises:
            raise RuntimeError("boom")
        start_response("{} X".format(status), [("content-type", content_type)])
        return [body]

    return app


def call_wsgi(wrapped, method="GET", path="/", body=b"", headers=None):
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "SERVER_NAME": "localhost",
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body),
        "CONTENT_LENGTH": str(len(body)) if body else "",
        "CONTENT_TYPE": "application/json" if body else "",
    }
    for k, v in (headers or {}).items():
        environ["HTTP_" + k.upper().replace("-", "_")] = v
    seen = {}

    def start_response(status, response_headers, exc_info=None):
        seen["status"] = int(status.split(" ", 1)[0])
        seen["headers"] = {k.lower(): v for k, v in response_headers}

    out = b"".join(wrapped(environ, start_response))
    return seen["status"], seen["headers"], out


class TestWsgiSampling(SamplingTestCase):
    def run_app(self, client, method, route, status=200, req_body=b"", **kw):
        wrapped = client.wsgi(wsgi_app(status=status, route=route, **kw))
        return call_wsgi(wrapped, method=method, path=route.replace("{id}", "7"),
                         body=req_body)

    def test_off_sends_the_response_untouched_with_no_id_header_and_no_log(self):
        client = self.client()
        status, headers, body = self.run_app(client, "GET", "/health")
        self.assertEqual((status, body), (200, b'{"ok":true}'))
        for name in ID_HEADERS + ("x-debug", "x-api-feedback", "x-log-url"):
            self.assertNotIn(name, headers)
        self.assertEqual(self.uploaded(client), [])

    def test_off_drops_errors_too(self):
        client = self.client()
        status, headers, body = self.run_app(client, "GET", "/health", status=500,
                                             body=b'{"error":"down"}')
        self.assertEqual(status, 500)
        # INJECT-001 would otherwise merge a debug object into this body.
        self.assertEqual(body, b'{"error":"down"}')
        self.assertNotIn("x-debug", headers)
        self.assertNotIn("x-api-feedback", headers)
        self.assertEqual(self.uploaded(client), [])

    def test_errors_drops_a_success(self):
        client = self.client()
        _, headers, _ = self.run_app(client, "GET", "/status")
        self.assertNotIn("x-request-id", headers)
        self.assertEqual(self.uploaded(client), [])

    def test_errors_sends_an_error_in_full(self):
        client = self.client()
        _, headers, _ = self.run_app(client, "GET", "/status", status=503,
                                     body=b'{"error":"x"}')
        self.assertIn("x-request-id", headers)
        self.assertIn("x-debug", headers)
        self.assertIn("x-api-feedback", headers)
        [entry] = self.uploaded(client)
        self.assertNotIn("sampled", entry)
        har = entry["request"]["log"]["entries"][0]
        self.assertIn('"error":"x"', har["response"]["content"]["text"])

    def test_a_rate_miss_uploads_a_bodyless_stub_still_headered(self):
        client = self.client()
        with mock.patch("random.random", return_value=0.99):
            _, headers, body = self.run_app(
                client, "POST", "/pets/{id}", req_body=b'{"name":"rex"}')
        # The stub is a real log, so the caller still gets a pointer to it.
        self.assertIn("x-request-id", headers)
        self.assertIn("x-debug", headers)
        self.assertIn("x-api-feedback", headers)
        self.assertEqual(body, b'{"ok":true}')
        [entry] = self.uploaded(client)
        self.assertIs(entry["sampled"], True)
        har = entry["request"]["log"]["entries"][0]
        self.assertNotIn("postData", har["request"])
        self.assertEqual(har["request"]["bodySize"], -1)
        self.assertEqual(har["response"]["bodySize"], -1)
        self.assertEqual(har["response"]["content"]["text"], "")
        # Headers are kept on a stub, and still redacted.
        self.assertEqual(har["response"]["status"], 200)

    def test_a_rate_hit_uploads_in_full(self):
        client = self.client()
        with mock.patch("random.random", return_value=0.01):
            self.run_app(client, "GET", "/pets")
        [entry] = self.uploaded(client)
        self.assertNotIn("sampled", entry)
        har = entry["request"]["log"]["entries"][0]
        self.assertEqual(har["response"]["content"]["text"], '{"ok":true}')

    def test_flask_style_route_is_normalized_before_lookup(self):
        client = self.client()
        _, headers, _ = self.run_app(client, "GET", "/pets/<int:id>")
        # `* /pets/{id}` is 0.5; whichever way the roll went it was not a drop.
        self.assertIn("x-request-id", headers)
        client = self.client(sampling={"routes": {"GET /pets/:id": "off"}})
        _, headers, _ = self.run_app(client, "GET", "/pets/<int:id>")
        self.assertNotIn("x-request-id", headers)

    def test_an_unconfigured_route_uses_the_default(self):
        client = self.client(sampling={"default": "off"})
        _, headers, _ = self.run_app(client, "GET", "/anything")
        self.assertNotIn("x-request-id", headers)
        client = self.client(sampling={"default": "errors"})
        call_wsgi(client.wsgi(wsgi_app(status=200)))  # no route at all
        self.assertEqual(self.uploaded(client), [])

    def test_draws_u_once_per_request(self):
        client = self.client()
        with mock.patch("random.random", return_value=0.5) as roll:
            self.run_app(client, "GET", "/pets")
        self.assertEqual(roll.call_count, 1)

    def test_a_crash_is_an_error_so_only_off_drops_it(self):
        client = self.client()
        with self.assertRaises(RuntimeError):
            self.run_app(client, "GET", "/status", raises=True)
        self.assertEqual(len(self.uploaded(client)), 1)

        client = self.client()
        with self.assertRaises(RuntimeError):
            self.run_app(client, "GET", "/health", raises=True)
        self.assertEqual(self.uploaded(client), [])

    def test_without_sampling_nothing_changes(self):
        client = self.client(sampling=None)
        with mock.patch("random.random") as roll:
            _, headers, _ = self.run_app(client, "GET", "/health")
        roll.assert_not_called()
        self.assertIn("x-request-id", headers)
        [entry] = self.uploaded(client)
        self.assertNotIn("sampled", entry)


# --------------------------------------------------------------------- ASGI


class Route(object):
    def __init__(self, path_format):
        self.path_format = path_format


def asgi_app(status=200, route=None, chunks=(b'{"ok":true}',),
             content_type=b"application/json"):
    async def app(scope, receive, send):
        await receive()
        if route is not None:
            scope["route"] = Route(route)  # what Starlette's router does
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", content_type)]})
        for i, chunk in enumerate(chunks):
            await send({"type": "http.response.body", "body": chunk,
                        "more_body": i < len(chunks) - 1})

    return app


def call_asgi(wrapped, method="GET", path="/", body=b""):
    scope = {"type": "http", "method": method, "path": path, "query_string": b"",
             "headers": [(b"host", b"localhost")], "scheme": "http"}
    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(wrapped(scope, receive, send))
    start = [m for m in sent if m["type"] == "http.response.start"][0]
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], headers, out


class TestAsgiSampling(SamplingTestCase):
    def run_app(self, client, method, route, req_body=b"", **kw):
        wrapped = client.asgi(asgi_app(route=route, **kw))
        return call_asgi(wrapped, method=method, path=route.replace("{id}", "7"),
                         body=req_body)

    def test_off_sends_the_response_untouched_with_no_id_header_and_no_log(self):
        client = self.client()
        status, headers, body = self.run_app(client, "GET", "/health")
        self.assertEqual((status, body), (200, b'{"ok":true}'))
        for name in ID_HEADERS + ("x-debug", "x-api-feedback", "x-log-url"):
            self.assertNotIn(name, headers)
        self.assertEqual(self.uploaded(client), [])

    def test_off_drops_errors_too(self):
        client = self.client()
        status, headers, body = self.run_app(client, "GET", "/health", status=404,
                                             chunks=(b'{"error":"x"}',))
        self.assertEqual((status, body), (404, b'{"error":"x"}'))
        self.assertNotIn("x-debug", headers)
        self.assertNotIn("x-api-feedback", headers)
        self.assertEqual(self.uploaded(client), [])

    def test_errors_drops_a_streamed_success_before_any_header_leaves(self):
        client = self.client()
        status, headers, body = self.run_app(
            client, "GET", "/status", chunks=(b"data: 1\n\n", b"data: 2\n\n"),
            content_type=b"text/event-stream")
        self.assertEqual(body, b"data: 1\n\ndata: 2\n\n")
        self.assertNotIn("x-request-id", headers)
        self.assertEqual(self.uploaded(client), [])

    def test_errors_sends_an_error_in_full(self):
        client = self.client()
        _, headers, _ = self.run_app(client, "GET", "/status", status=500,
                                     chunks=(b'{"error":"x"}',))
        self.assertIn("x-request-id", headers)
        [entry] = self.uploaded(client)
        self.assertNotIn("sampled", entry)

    def test_a_rate_miss_uploads_a_bodyless_stub_still_headered(self):
        client = self.client()
        with mock.patch("random.random", return_value=0.99):
            _, headers, body = self.run_app(
                client, "PUT", "/pets/{id}", req_body=b'{"name":"rex"}',
                chunks=(b'{"a":', b'1}'))
        self.assertIn("x-request-id", headers)
        self.assertEqual(body, b'{"a":1}')
        [entry] = self.uploaded(client)
        self.assertIs(entry["sampled"], True)
        har = entry["request"]["log"]["entries"][0]
        self.assertNotIn("postData", har["request"])
        self.assertEqual(har["response"]["bodySize"], -1)

    def test_a_rate_hit_uploads_in_full(self):
        client = self.client()
        with mock.patch("random.random", return_value=0.0):
            self.run_app(client, "GET", "/pets")
        [entry] = self.uploaded(client)
        self.assertNotIn("sampled", entry)
        har = entry["request"]["log"]["entries"][0]
        self.assertEqual(har["response"]["content"]["text"], '{"ok":true}')

    def test_draws_u_once_per_request(self):
        client = self.client()
        with mock.patch("random.random", return_value=0.5) as roll:
            self.run_app(client, "GET", "/pets", chunks=(b"a", b"b", b"c"))
        self.assertEqual(roll.call_count, 1)


# ------------------------------------------------------------ config report


class TestConfigReport(SamplingTestCase):
    def test_reports_once_on_the_first_request_with_the_normalized_config(self):
        client = self.client(sampling={"routes": {"get /pets/:id/": 0.12345, "GET /health": "off"}})
        # SAMPLE-051. Never at construction.
        time.sleep(0.02)
        self.assertEqual(self.transport.to("/v1/sampling"), [])

        wrapped = client.wsgi(wsgi_app(route="/health"))
        call_wsgi(wrapped)
        call_wsgi(wrapped)
        [(_, report, headers)] = self.transport.wait_for("/v1/sampling")
        self.assertEqual(report["default"], "on")
        self.assertEqual(report["routes"], {"GET /health": "off", "GET /pets/{id}": 0.1235})
        self.assertEqual(report["creator"], {"name": restless.SDK_NAME,
                                             "version": restless.SDK_VERSION})
        self.assertRegex(report["hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(headers["Authorization"], "Bearer rk_test")
        time.sleep(0.02)
        self.assertEqual(len(self.transport.to("/v1/sampling")), 1)

    def test_uploads_carry_the_hash(self):
        client = self.client()
        call_wsgi(client.wsgi(wsgi_app(route="/status", status=500)))
        [(_, report, _)] = self.transport.wait_for("/v1/sampling")
        client.flush()
        [(_, _, headers)] = self.transport.to("/v1/request")
        self.assertEqual(headers["x-restless-sampling"], report["hash"])

    def test_resends_when_the_ingest_asks(self):
        transport = Transport(response={"needsSamplingConfig": True})
        client = self.client(transport=transport)
        call_wsgi(client.wsgi(wsgi_app(route="/status", status=500)))
        transport.wait_for("/v1/sampling")
        # SAMPLE-053 skips a resend while a report is in flight.
        deadline = time.time() + 2
        while client.engine._report_state != "sent" and time.time() < deadline:
            time.sleep(0.005)
        client.flush()
        self.assertEqual(len(transport.wait_for("/v1/sampling", count=2)), 2)

    def test_no_report_and_no_header_without_sampling(self):
        client = self.client(sampling={"default": "on", "routes": {}})
        call_wsgi(client.wsgi(wsgi_app(route="/x")))
        client.flush()
        time.sleep(0.02)
        self.assertEqual(self.transport.to("/v1/sampling"), [])
        [(_, _, headers)] = self.transport.to("/v1/request")
        self.assertNotIn("x-restless-sampling", headers)

    def test_no_report_without_a_key(self):
        with mock.patch.dict(os.environ, {"RESTLESS_KEY": "", "README_API_KEY": ""}):
            client = self.client(api_key=None)
        call_wsgi(client.wsgi(wsgi_app(route="/health")))
        time.sleep(0.02)
        self.assertEqual(self.transport.to("/v1/sampling"), [])

    def test_no_report_under_a_test_runner(self):
        client = self.client()
        with mock.patch.dict(os.environ, {"RESTLESS_SETUP_MODE": ""}):
            call_wsgi(client.wsgi(wsgi_app(route="/health")))
            time.sleep(0.02)
        self.assertEqual(self.transport.to("/v1/sampling"), [])

    def test_a_failed_report_never_reaches_the_request_path(self):
        client = self.client(transport=Transport(fail=True))
        status, _, body = call_wsgi(client.wsgi(wsgi_app(route="/pets")))
        self.assertEqual((status, body), (200, b'{"ok":true}'))
        self.transport.wait_for("/v1/sampling")

    def test_a_failing_decision_sends_in_full(self):
        client = self.client()
        with mock.patch("random.random", side_effect=ValueError("no entropy")):
            _, headers, _ = call_wsgi(client.wsgi(wsgi_app(route="/pets")))
        self.assertIn("x-request-id", headers)
        [entry] = self.uploaded(client)
        self.assertNotIn("sampled", entry)


# ------------------------------------------------------- settings and merge


class TestSettingsFile(SamplingTestCase):
    def test_merges_with_code_code_winning_per_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".restless"))
            with open(os.path.join(tmp, ".restless", "settings.json"), "w") as handle:
                json.dump({"apis": [{"name": "a", "sampling": {
                    "default": "errors",
                    "routes": {"GET /health": "off", "GET /pets": 0.5},
                }}]}, handle)
            _settings._reset_cache()
            self.assertEqual(
                _settings.resolve_api(_settings.load_settings(tmp))["sampling"]["default"],
                "errors",
            )
            client = self.client(sampling={"routes": {"GET /pets": "on"}})

        config = client.engine._sampling
        self.assertEqual(config.default, "errors")
        self.assertEqual(config.routes, {"GET /health": "off", "GET /pets": "on"})


class TestPythonTraps(SamplingTestCase):
    """Places where the obvious Python disagrees with the contract."""

    def test_bool_is_not_a_rate(self):
        # bool subclasses int: True would otherwise be the rate 1.
        self.assertIsNone(normalize_mode(True))
        self.assertIsNone(normalize_mode(False))

    def test_quantization_is_half_up_not_half_even(self):
        # round(0.5) == 0 in Python; SAMPLE-002 wants floor(x + 0.5).
        self.assertEqual(normalize_mode(0.00005), 0.0001)
        self.assertEqual(normalize_mode(0.00025), 0.0003)

    def test_methods_uppercase_ascii_only(self):
        # str.upper() turns "ß" into "SS".
        config, _ = normalize_sampling(code={"routes": {"SS /x": "off"}})
        self.assertEqual(resolve_mode(config, "ß", "/x"), "on")

    def test_trailing_newline_is_not_a_valid_path(self):
        # `$` in Python also matches before a final newline.
        config, warnings = normalize_sampling(code={"routes": {"GET /x\n": "off"}})
        self.assertEqual(config.routes, {})
        self.assertEqual(len(warnings), 1)

    def test_invalid_config_never_raises(self):
        for bad in ("off", 1, [1], {"routes": [1]}, {"default": object()},
                    {"routes": {1: "off", "GET /x": float("nan")}}):
            normalize_sampling(code=bad)
        client = restless.Restless("k", sampling={"default": "sometimes"})
        self.assertIsNone(client.engine._sampling)


if __name__ == "__main__":
    unittest.main()
