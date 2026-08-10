"""The setup callback's request view.

CONTRACT.md section 14 leaves the callback's ARGUMENT per-language (only the
returned shape in section 15 is normative), so this is a parity requirement
rather than a conformance one: Ruby hands the callback a `RequestInfo` with
`header`, `request_method`, `path`, `query_string`, `url` and the raw `env`,
and Go hands it a `*RequestInfo` with `Header`, `Request` and `Route`. Python
handed over the raw protocol dict, so the documented
`request.headers.get(...)` raised AttributeError, SAFETY-002 swallowed it,
and the result was an install that captured traffic while attributing none of
it. These tests pin the shape that fixed it.
"""

import unittest

from restless import RequestInfo
from restless._request import Headers


def wsgi_request(**over):
    environ = {
        "REQUEST_METHOD": "POST",
        "SCRIPT_NAME": "/api",
        "PATH_INFO": "/pets",
        "QUERY_STRING": "limit=10",
        "HTTP_AUTHORIZATION": "Bearer sk_live_abcd1234",
        "CONTENT_TYPE": "application/json",
    }
    environ.update(over)
    return RequestInfo(
        headers={"authorization": environ.get("HTTP_AUTHORIZATION", ""),
                 "content-type": environ.get("CONTENT_TYPE", "")},
        method=environ["REQUEST_METHOD"],
        url="http://api.example.com/api/pets?limit=10",
        environ=environ,
    )


def asgi_request(**over):
    scope = {
        "type": "http",
        "method": "GET",
        "root_path": "",
        "path": "/pets",
        "query_string": b"limit=10",
    }
    scope.update(over)
    return RequestInfo(
        headers={"authorization": "Bearer sk_live_abcd1234"},
        method=scope["method"],
        url="http://api.example.com/pets?limit=10",
        scope=scope,
    )


class TestHeaders(unittest.TestCase):
    def test_lookup_is_case_insensitive_both_ways(self):
        # The trap this replaced: a header that is present reading as absent
        # because the caller capitalized it. That returns None, and a None
        # api_key is indistinguishable from an unauthenticated request.
        h = Headers({"authorization": "Bearer x", "content-type": "application/json"})
        for name in ("authorization", "Authorization", "AUTHORIZATION"):
            self.assertEqual(h.get(name), "Bearer x", name)
            self.assertEqual(h[name], "Bearer x", name)
            self.assertIn(name, h)

    def test_missing_header_returns_default(self):
        h = Headers({"a": "1"})
        self.assertIsNone(h.get("nope"))
        self.assertEqual(h.get("nope", "fallback"), "fallback")
        self.assertNotIn("nope", h)

    def test_is_a_read_only_mapping(self):
        h = Headers({"a": "1", "b": "2"})
        self.assertEqual(len(h), 2)
        self.assertEqual(sorted(h), ["a", "b"])
        self.assertEqual(dict(h), {"a": "1", "b": "2"})
        with self.assertRaises(TypeError):
            h["c"] = "3"


class TestRequestInfoParity(unittest.TestCase):
    """The accessors Ruby and Go both expose."""

    def test_header_accessor_matches_ruby_and_go(self):
        r = wsgi_request()
        self.assertEqual(r.header("Authorization"), "Bearer sk_live_abcd1234")
        self.assertEqual(r.header("authorization"), "Bearer sk_live_abcd1234")
        self.assertIsNone(r.header("x-missing"))

    def test_bracket_alias_matches_rubys(self):
        self.assertEqual(wsgi_request()["Authorization"], "Bearer sk_live_abcd1234")

    def test_headers_mapping_makes_the_documented_form_work(self):
        # `request.headers.get("authorization")` is what README.md and the
        # module docstring have always shown. It has to work.
        self.assertEqual(
            wsgi_request().headers.get("authorization"), "Bearer sk_live_abcd1234"
        )

    def test_method_and_url(self):
        r = wsgi_request()
        self.assertEqual(r.method, "POST")
        self.assertEqual(r.url, "http://api.example.com/api/pets?limit=10")

    def test_raw_protocol_dict_is_still_reachable(self):
        # Ruby exposes `env`, Go exposes `Request`. Nothing is taken away by
        # normalizing: anything the wrapper does not model is still there.
        r = wsgi_request()
        self.assertEqual(r.environ["PATH_INFO"], "/pets")
        self.assertIsNone(r.scope)
        self.assertIs(r.raw, r.environ)

        a = asgi_request()
        self.assertEqual(a.scope["path"], "/pets")
        self.assertIsNone(a.environ)
        self.assertIs(a.raw, a.scope)


class TestOneCallbackBothProtocols(unittest.TestCase):
    """The reason this is one class and not two.

    Ruby and Go each speak a single protocol. Python speaks WSGI and ASGI, so
    a view that differed between them would recreate the original problem in
    a smaller shape: a callback that works under Flask and silently returns
    nothing under FastAPI.
    """

    def test_same_accessors_resolve_under_both(self):
        for r in (wsgi_request(), asgi_request()):
            self.assertEqual(r.header("authorization"), "Bearer sk_live_abcd1234")
            self.assertEqual(r.headers.get("Authorization"), "Bearer sk_live_abcd1234")
            self.assertEqual(r.query_string, "limit=10")
            self.assertTrue(r.path.endswith("/pets"))

    def test_path_includes_the_mount_prefix(self):
        # WSGI: SCRIPT_NAME + PATH_INFO, matching Ruby's `path`. ASGI:
        # root_path + path. An app mounted under a prefix should report the
        # path its callers actually used.
        self.assertEqual(wsgi_request().path, "/api/pets")
        self.assertEqual(asgi_request(root_path="/api").path, "/api/pets")

    def test_query_string_decodes_asgi_bytes(self):
        self.assertEqual(asgi_request(query_string=b"a=1&b=2").query_string, "a=1&b=2")
        self.assertEqual(asgi_request(query_string=b"").query_string, "")


class TestEscapeHatch(unittest.TestCase):
    def test_framework_request_is_exposed_when_set(self):
        # `environ["restless.request"]` used to REPLACE the callback argument.
        # It now rides along instead, so the argument type is stable and the
        # capability is kept.
        sentinel = object()
        r = wsgi_request(**{"restless.request": sentinel})
        self.assertIs(r.framework_request, sentinel)

    def test_framework_request_is_none_by_default(self):
        self.assertIsNone(wsgi_request().framework_request)
        self.assertIsNone(asgi_request().framework_request)


class TestRouteIsHonestlyEmpty(unittest.TestCase):
    def test_route_is_none_before_the_router_has_run(self):
        # The setup callback runs BEFORE the application, so no router has
        # matched: Flask's url_rule and Starlette's scope["route"] do not
        # exist yet. Reporting a route here would be a lie; the captured log
        # still gets the real one, read after the response.
        self.assertIsNone(wsgi_request().route)
        self.assertIsNone(asgi_request().route)

    def test_route_is_carried_when_something_upstream_set_it(self):
        self.assertEqual(RequestInfo(route="/pets/{id}").route, "/pets/{id}")


class TestDegradesQuietly(unittest.TestCase):
    def test_empty_request_does_not_raise(self):
        # SAFETY-001: nothing here may take down a request path.
        r = RequestInfo()
        self.assertEqual(r.method, "GET")
        self.assertEqual(r.path, "")
        self.assertEqual(r.query_string, "")
        self.assertIsNone(r.header("authorization"))
        self.assertEqual(r.raw, {})


if __name__ == "__main__":
    unittest.main()
