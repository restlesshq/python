"""HAR-013: the logged URL carries the scheme the client used (RES-21).

Render, Heroku, an ALB or nginx terminate TLS and hand the app plain HTTP.
gunicorn and uvicorn only rewrite ``wsgi.url_scheme`` / ``scope["scheme"]``
from forwarded headers for a configured list of proxy IPs, which a typical
PaaS deploy never sets, so the transport scheme alone logs every production
request as ``http://``. The pure rule is pinned by
``spec/vectors/request-scheme.json``; these tests pin that both adapters use
it.
"""

import asyncio
import unittest

from restless._capture import CaptureEngine
from restless.adapters.asgi import wrap_asgi
from restless.adapters.wsgi import wrap_wsgi


def _engine(recorded):
    engine = CaptureEngine(api_key="k", base_url="https://ingress.example")
    engine.record = lambda captured: recorded.append(captured["request"]["url"])
    return engine


def wsgi_url(extra_environ):
    recorded = []

    def app(environ, start_response):
        start_response("200 OK", [("content-type", "text/plain")])
        return [b"ok"]

    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/v1/ping",
        "QUERY_STRING": "x=1",
        "HTTP_HOST": "api.example.com",
        "wsgi.url_scheme": "http",
        "wsgi.input": None,
    }
    environ.update(extra_environ)
    list(wrap_wsgi(app, _engine(recorded))(environ, lambda *a: None))
    return recorded[0]


def asgi_url(headers, scheme="http"):
    recorded = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    scope = {
        "type": "http",
        "method": "GET",
        "scheme": scheme,
        "path": "/v1/ping",
        "query_string": b"x=1",
        "headers": [(b"host", b"api.example.com")]
        + [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
    }
    asyncio.run(wrap_asgi(app, _engine(recorded))(scope, receive, send))
    return recorded[0]


class TestWsgiScheme(unittest.TestCase):
    def test_x_forwarded_proto_https(self):
        self.assertEqual(wsgi_url({"HTTP_X_FORWARDED_PROTO": "https"}),
                         "https://api.example.com/v1/ping?x=1")

    def test_forwarded_proto(self):
        self.assertEqual(wsgi_url({"HTTP_FORWARDED": "for=192.0.2.60;proto=https"}),
                         "https://api.example.com/v1/ping?x=1")

    def test_plain_request_keeps_transport_scheme(self):
        self.assertEqual(wsgi_url({}), "http://api.example.com/v1/ping?x=1")
        self.assertEqual(wsgi_url({"wsgi.url_scheme": "https"}),
                         "https://api.example.com/v1/ping?x=1")


class TestAsgiScheme(unittest.TestCase):
    def test_x_forwarded_proto_https(self):
        self.assertEqual(asgi_url({"x-forwarded-proto": "https"}),
                         "https://api.example.com/v1/ping?x=1")

    def test_forwarded_proto(self):
        self.assertEqual(asgi_url({"forwarded": 'proto="https"'}),
                         "https://api.example.com/v1/ping?x=1")

    def test_plain_request_keeps_transport_scheme(self):
        self.assertEqual(asgi_url({}), "http://api.example.com/v1/ping?x=1")
        self.assertEqual(asgi_url({}, scheme="https"), "https://api.example.com/v1/ping?x=1")


if __name__ == "__main__":
    unittest.main()
