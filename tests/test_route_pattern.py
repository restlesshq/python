"""Where each adapter reads the matched route from.

The route feeds `routePattern` on the wire, the route-only fingerprint and
per-route sampling, so an adapter that silently reports None degrades all
three. The framework objects are stood in for here; the shapes mirror what
Flask (Werkzeug) and Starlette actually put on the environ and the scope.
"""

import unittest

from restless.adapters.asgi import _route_pattern as asgi_route
from restless.adapters.wsgi import _route_pattern as wsgi_route


class _Obj(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


class TestWsgiRoute(unittest.TestCase):
    def test_flask_rule_is_normalized(self):
        request = _Obj(url_rule=_Obj(rule="/pets/<int:pet_id>/toys/<name>"))
        environ = {"werkzeug.request": request}
        self.assertEqual(wsgi_route(environ), "/pets/{pet_id}/toys/{name}")

    def test_flask_clears_the_request_before_returning(self):
        # Flask sets environ["werkzeug.request"] to None when it pops the
        # request context, which is why the adapter reads the rule while
        # start_response runs instead of after the app returns.
        from restless.adapters import wsgi

        def flask_like(environ, start_response):
            environ["werkzeug.request"] = _Obj(url_rule=_Obj(rule="/pets/<int:id>"))
            start_response("200 OK", [("content-type", "text/plain")])
            environ["werkzeug.request"] = None
            return [b"ok"]

        recorded = []

        class Engine(object):
            request_id_prefix = None
            has_api_key = True

            def resolve(self, request):
                return {}

            def sample(self, method, route, status):
                recorded.append(route)
                return "full"

            def build_injection(self, **kw):
                return {}, kw["raw_body"], None

            def record(self, captured):
                recorded.append(captured["routePattern"])

        app = wsgi.wrap_wsgi(flask_like, Engine())
        environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/pets/1", "wsgi.input": None}
        list(app(environ, lambda *a: None))
        self.assertEqual(recorded, ["/pets/{id}", "/pets/{id}"])

    def test_explicit_route_wins(self):
        self.assertEqual(wsgi_route({"restless.route": "/x/{id}"}), "/x/{id}")

    def test_none_without_a_router(self):
        self.assertIsNone(wsgi_route({}))


def endpoint():
    pass


def other():
    pass


class TestAsgiRoute(unittest.TestCase):
    def test_fastapi_puts_the_route_on_the_scope(self):
        scope = {"route": _Obj(path_format="/pets/{id}", path="/pets/{id:int}")}
        self.assertEqual(asgi_route(scope), "/pets/{id}")

    def test_starlette_route_is_found_by_endpoint(self):
        router = _Obj(routes=[
            _Obj(path_format="/health", endpoint=other),
            _Obj(path_format="/pets/{id}", endpoint=endpoint),
        ])
        self.assertEqual(asgi_route({"router": router, "endpoint": endpoint}), "/pets/{id}")

    def test_starlette_mount_prefix_is_kept(self):
        router = _Obj(routes=[
            _Obj(path_format="/health", endpoint=other),
            _Obj(path_format="/api/{path}", routes=[_Obj(path_format="/pets/{id}", endpoint=endpoint)]),
        ])
        self.assertEqual(asgi_route({"router": router, "endpoint": endpoint}), "/api/pets/{id}")

    def test_an_endpoint_under_two_paths_reports_nothing(self):
        router = _Obj(routes=[
            _Obj(path_format="/pets/{id}", endpoint=endpoint),
            _Obj(path_format="/api/{path}", routes=[_Obj(path_format="/pets/{id}", endpoint=endpoint)]),
        ])
        self.assertIsNone(asgi_route({"router": router, "endpoint": endpoint}))

    def test_none_before_routing(self):
        self.assertIsNone(asgi_route({"type": "http"}))


if __name__ == "__main__":
    unittest.main()
