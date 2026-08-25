"""Debug injection: which host the URLs are built on, and when they ship.

The shared vectors cover the pure shape of ``build_debug_injection``; this
file covers the parts that are this SDK's own wiring - that the engine only
uses a portal origin the server published, and that the adapters ship the
headers on a success without touching its body.
"""

import unittest

from restless._capture import CaptureEngine
from restless._injection import build_debug_injection, debug_headers

PORTAL = "https://acme.restlessdocs.com"
REQUEST_ID = "9f18a0e2-1c3d-4b5a-8e7f-0a1b2c3d4e5f"


class TestDebugInjection(unittest.TestCase):
    def test_headers_on_a_2xx_but_no_body_mutator(self):
        headers, mutate = build_debug_injection(
            status=200, request_id=REQUEST_ID, portal_url=PORTAL
        )
        self.assertEqual(headers["x-log-url"], "{}/logs/{}".format(PORTAL, REQUEST_ID))
        self.assertIn("npx api debug", headers["x-debug"])
        # A successful body is the caller's data, not ours to reshape.
        self.assertIsNone(mutate)

    def test_both_urls_share_the_portal_origin(self):
        headers, mutate = build_debug_injection(
            status=404,
            request_id=REQUEST_ID,
            portal_url=PORTAL,
            method="GET",
            path="/car/{id}",
        )
        debug = mutate({})["debug"]
        self.assertTrue(headers["x-log-url"].startswith(PORTAL + "/logs/"))
        self.assertTrue(debug["log"].startswith(PORTAL + "/logs/"))
        self.assertIn("{}/p/{}/get-car-id.md".format(PORTAL, REQUEST_ID), debug["recovery"])

    def test_no_portal_origin_emits_no_url_at_all(self):
        for status in (200, 404, 500):
            headers, mutate = build_debug_injection(
                status=status, request_id=REQUEST_ID, method="GET", path="/car/{id}"
            )
            # x-debug carries no URL, so it survives.
            self.assertIn("npx api debug", headers["x-debug"])
            self.assertNotIn("x-log-url", headers)
            self.assertIsNone(mutate)

    def test_ingest_origin_is_never_a_log_host(self):
        # The regression this whole change exists for: the base URL serves
        # /v1/* only, so a log URL built on it 404s.
        headers, _ = build_debug_injection(
            status=404, request_id=REQUEST_ID, portal_url=PORTAL
        )
        self.assertNotIn("ingress", headers["x-log-url"])

    def test_authored_recovery_precedes_the_dig_in_line(self):
        _, mutate = build_debug_injection(
            status=402,
            request_id=REQUEST_ID,
            portal_url=PORTAL,
            recovery="Try another card.",
            method="POST",
            path="/charge",
        )
        recovery = mutate({})["debug"]["recovery"]
        self.assertTrue(recovery.startswith("Try another card.\n\n"))
        self.assertIn("/p/{}/post-charge.md".format(REQUEST_ID), recovery)

    def test_debug_headers_omit_the_url_without_an_origin(self):
        self.assertEqual(list(debug_headers(REQUEST_ID).keys()), ["x-debug"])


class TestEnginePortalOrigin(unittest.TestCase):
    def _engine(self):
        return CaptureEngine(api_key="k", base_url="https://ingress.example")

    def test_cold_engine_has_no_portal_origin(self):
        engine = self._engine()
        self.assertIsNone(engine.portal_url)
        headers, body, _ = engine.build_injection(
            status=404,
            request_id=REQUEST_ID,
            method="GET",
            route="/car/{id}",
            raw_body='{"error":"nope"}',
            content_type="application/json",
            response_headers={"content-type": "application/json"},
        )
        self.assertNotIn("x-log-url", headers)
        # No origin means no debug object either, so the body is unchanged.
        self.assertEqual(body, '{"error":"nope"}')

    def test_server_response_publishes_the_origin(self):
        engine = self._engine()
        # WIRE-023. The wire key is still `docsUrl`, for compatibility with
        # every already-deployed SDK.
        engine._handle_server_response({"docsUrl": PORTAL + "/"}, [])
        # Trailing slash stripped so we never build a `//logs/<id>`.
        self.assertEqual(engine.portal_url, PORTAL)

        headers, _, _ = engine.build_injection(
            status=200,
            request_id=REQUEST_ID,
            method="GET",
            route="/car/{id}",
            raw_body='{"ok":true}',
            content_type="application/json",
            response_headers={"content-type": "application/json"},
        )
        self.assertEqual(headers["x-log-url"], "{}/logs/{}".format(PORTAL, REQUEST_ID))


if __name__ == "__main__":
    unittest.main()
