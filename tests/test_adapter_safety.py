"""Adapter wiring the shared vectors cannot reach: an app that raises runs
exactly once (SAFETY-001), and a response whose start goes out before its
body still carries the debug headers (INJECT-002).
"""

import asyncio
import unittest

from restless._capture import CaptureEngine
from restless.adapters.asgi import wrap_asgi
from restless.adapters.wsgi import wrap_wsgi

PORTAL = "https://acme.restlessdocs.com"


def _engine(portal=PORTAL):
    engine = CaptureEngine(api_key="k", base_url="https://ingress.example")
    engine._portal_url = portal
    return engine


def _wsgi_environ():
    return {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/pets",
        "QUERY_STRING": "",
        "SERVER_NAME": "api.example.com",
        "SERVER_PORT": "80",
        "wsgi.url_scheme": "http",
    }


def _asgi_scope():
    return {
        "type": "http",
        "method": "GET",
        "path": "/pets",
        "query_string": b"",
        "headers": [(b"host", b"api.example.com")],
        "scheme": "http",
    }


async def _run_asgi(app, scope=None):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope or _asgi_scope(), receive, send)
    return sent


def _start_headers(sent):
    start = next(m for m in sent if m["type"] == "http.response.start")
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in start["headers"]}


class TestAppRunsOnce(unittest.TestCase):
    def test_wsgi_app_that_raises_is_not_rerun(self):
        calls = []

        def app(environ, start_response):
            calls.append(1)
            raise RuntimeError("boom")

        wrapped = wrap_wsgi(app, _engine())
        with self.assertRaises(RuntimeError):
            wrapped(_wsgi_environ(), lambda *a: None)
        self.assertEqual(len(calls), 1)

    def test_asgi_app_that_raises_is_not_rerun(self):
        calls = []

        async def app(scope, receive, send):
            calls.append(1)
            raise RuntimeError("boom")

        wrapped = wrap_asgi(app, _engine())
        with self.assertRaises(RuntimeError):
            asyncio.run(_run_asgi(wrapped))
        self.assertEqual(len(calls), 1)

    def test_asgi_app_that_raises_after_responding_is_not_rerun(self):
        calls = []

        async def app(scope, receive, send):
            calls.append(1)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
            raise RuntimeError("after the response")

        sent = []

        async def run():
            async def receive():
                return {"type": "http.request", "body": b""}

            async def send(message):
                sent.append(message)

            await wrap_asgi(app, _engine())(_asgi_scope(), receive, send)

        with self.assertRaises(RuntimeError):
            asyncio.run(run())
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(1 for m in sent if m["type"] == "http.response.start"), 1)

    def test_injection_failure_still_sends_the_response(self):
        engine = _engine()

        def broken(**kwargs):
            raise ValueError("injection bug")

        engine.build_injection = broken

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 404,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"error":"nope"}'})

        sent = asyncio.run(_run_asgi(wrap_asgi(app, engine)))
        self.assertEqual(sent[-1]["body"], b'{"error":"nope"}')

        def wsgi_app(environ, start_response):
            start_response("404 Not Found", [("content-type", "application/json")])
            return [b'{"error":"nope"}']

        body = wrap_wsgi(wsgi_app, engine)(_wsgi_environ(), lambda *a: None)
        self.assertEqual(b"".join(body), b'{"error":"nope"}')

    def test_middleware_failure_before_the_app_still_passes_through(self):
        engine = _engine()

        def broken(*args, **kwargs):
            raise ValueError("resolve bug")

        engine.resolve = broken
        calls = []

        async def app(scope, receive, send):
            calls.append(1)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        sent = asyncio.run(_run_asgi(wrap_asgi(app, engine)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(sent[-1]["body"], b"ok")


class TestAsgiDebugHeadersOnEarlyStart(unittest.TestCase):
    def _app(self, status, content_type=b"application/json"):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", content_type)]})
            await send({"type": "http.response.body", "body": b'{"ok":true}'})
        return app

    def test_success_carries_x_debug_and_x_log_url(self):
        sent = asyncio.run(_run_asgi(wrap_asgi(self._app(200), _engine())))
        headers = _start_headers(sent)
        self.assertIn("npx api debug", headers["x-debug"])
        self.assertTrue(headers["x-log-url"].startswith(PORTAL + "/logs/"))
        # A success body is never touched.
        self.assertEqual(sent[-1]["body"], b'{"ok":true}')

    def test_no_portal_origin_means_no_log_url(self):
        sent = asyncio.run(_run_asgi(wrap_asgi(self._app(200), _engine(portal=None))))
        headers = _start_headers(sent)
        self.assertIn("x-debug", headers)
        self.assertNotIn("x-log-url", headers)

    def test_streamed_error_carries_the_headers(self):
        app = self._app(500, content_type=b"text/event-stream")
        sent = asyncio.run(_run_asgi(wrap_asgi(app, _engine())))
        self.assertIn("x-debug", _start_headers(sent))

    def test_an_app_header_of_the_same_name_is_replaced_not_duplicated(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"X-Debug", b"theirs")]})
            await send({"type": "http.response.body", "body": b""})

        sent = asyncio.run(_run_asgi(wrap_asgi(app, _engine())))
        start = next(m for m in sent if m["type"] == "http.response.start")
        names = [k.decode("latin-1").lower() for k, _ in start["headers"]]
        self.assertEqual(names.count("x-debug"), 1)

    def test_recorded_headers_match_what_was_sent(self):
        engine = _engine()
        recorded = []
        engine.record = recorded.append
        sent = asyncio.run(_run_asgi(wrap_asgi(self._app(200), engine)))
        self.assertEqual(recorded[0]["response"]["headers"]["x-debug"],
                         _start_headers(sent)["x-debug"])



class TestHeldErrorResponse(unittest.TestCase):
    """A 4xx/5xx start is held so the body can be rewritten. What finally goes
    out must still be the app's response."""

    def _raw_404(self, body=b'{"error":"nope"}', chunks=None):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 404, "headers": [
                (b"content-type", b"application/json"),
                (b"set-cookie", b"a=1"), (b"set-cookie", b"b=2"),
                (b"x-name", "caf\xe9".encode("latin-1")),
            ]})
            for i, part in enumerate(chunks or [body]):
                await send({"type": "http.response.body", "body": part,
                            "more_body": i < len(chunks or [body]) - 1})
        return app

    def test_duplicate_and_latin1_headers_survive_the_rewrite(self):
        sent = asyncio.run(_run_asgi(wrap_asgi(self._raw_404(), _engine())))
        start = next(m for m in sent if m["type"] == "http.response.start")
        self.assertEqual([v for k, v in start["headers"] if k == b"set-cookie"],
                         [b"a=1", b"b=2"])
        self.assertIn((b"x-name", b"caf\xe9"), start["headers"])
        self.assertIn(b'"debug"', sent[-1]["body"])

    def test_recorded_content_length_matches_the_rewritten_body(self):
        engine = _engine()
        recorded = []
        engine.record = recorded.append
        sent = asyncio.run(_run_asgi(wrap_asgi(self._raw_404(), engine)))
        self.assertEqual(recorded[0]["response"]["headers"]["content-length"],
                         str(len(sent[-1]["body"])))

    def test_a_large_error_body_is_passed_through_whole(self):
        part = b"x" * (600 * 1024)
        app = self._raw_404(chunks=[part, part, part])
        sent = asyncio.run(_run_asgi(wrap_asgi(app, _engine())))
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        self.assertEqual(body, part * 3)
        self.assertIn("x-debug", _start_headers(sent))


class TestPassThroughEdges(unittest.TestCase):
    def test_blocked_request_is_not_run_when_the_send_fails(self):
        engine = _engine()
        engine.resolve = lambda request: {"block": True}
        calls = []

        async def app(scope, receive, send):
            calls.append(1)

        async def run():
            async def receive():
                return {"type": "http.request", "body": b""}

            async def send(message):
                raise OSError("client went away")

            await wrap_asgi(app, engine)(_asgi_scope(), receive, send)

        with self.assertRaises(OSError):
            asyncio.run(run())
        self.assertEqual(calls, [])

    def test_wsgi_closes_the_app_iterable(self):
        closed = []

        class Body(object):
            def __iter__(self):
                return iter([b"ok"])

            def close(self):
                closed.append(1)

        def app(environ, start_response):
            start_response("200 OK", [("content-type", "text/plain")])
            return Body()

        body = wrap_wsgi(app, _engine())(_wsgi_environ(), lambda *a: None)
        self.assertEqual(b"".join(body), b"ok")
        self.assertEqual(closed, [1])


if __name__ == "__main__":
    unittest.main()
