"""FP-047: the transitional previous fingerprint key.

Turning the `stack` strategy on MOVES the fingerprint of every uncaught
5xx. Before the adapters captured exceptions, `stackTrace` was never
populated, so those errors keyed on their normalized message and customers
attached Agent Recovery guidance to THAT key. Without a fallback, the
guidance silently stops being injected the day the strategy starts firing,
with nothing anywhere to signal it. These are the tests for the fallback.
"""

import json
import os
import unittest
from unittest import mock

from restless._capture import CaptureEngine
from restless._fingerprint import Fingerprint

TRACEBACK = '''Traceback (most recent call last):
  File "/proj/src/db/users.py", line 12, in find_by_id
    raise ValueError("boom")
ValueError: boom
'''

#: What this error keyed on before the stack strategy became reachable.
LEGACY_KEY = "500:GET:/users:something-came-apart"


def _captured():
    return {
        "requestId": "id-1",
        "startedAt": "2026-01-01T00:00:00.000Z",
        "duration": 1,
        "routePattern": "/users",
        "request": {"method": "GET", "url": "http://x/users", "headers": {}},
        "response": {
            "status": 500,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"message": "Something came apart"}),
        },
        "stackTrace": TRACEBACK,
    }


class TestPreviousKey(unittest.TestCase):
    def test_injects_a_message_still_attached_to_the_pre_stack_key(self):
        sent = {}

        def transport(url, body, headers):
            sent["payload"] = json.loads(body.decode("utf-8"))
            # The ingest only knows the OLD key, because that is what it
            # stored back when the message strategy produced it.
            return json.dumps(
                {"recoveryMessages": {LEGACY_KEY: "Check the widget."}}
            ).encode("utf-8")

        engine = CaptureEngine(
            api_key="k", base_url="https://ingest.example", transport=transport
        )

        captured = _captured()
        fp = engine.compute_fingerprint(captured)
        self.assertEqual(fp.strategy, "stack")
        self.assertEqual(fp.key, "500:src/db/users.py:find_by_id")
        self.assertEqual(fp.previous_key, LEGACY_KEY)

        captured["errorFingerprint"] = fp.to_wire()
        # RESTLESS_ENV keeps the uploader batching (so the flush below is the
        # synchronous one, not a background thread); RESTLESS_SETUP_MODE
        # opts out of BATCH-008's drop-everything-under-a-test-runner rule.
        env = dict(os.environ, RESTLESS_ENV="production", RESTLESS_SETUP_MODE="1")
        with mock.patch.dict(os.environ, env, clear=True):
            engine.record(captured)
            engine.flush()

        # Both keys go up, so the ingest can answer for whichever it holds.
        uploaded = sent["payload"][0]["errorFingerprint"]
        self.assertEqual(uploaded["key"], fp.key)
        self.assertEqual(uploaded["previousKey"], LEGACY_KEY)

        # Nothing is cached under the NEW key, so a plain lookup finds
        # nothing...
        self.assertIsNone(engine.lookup_recovery(fp.key))
        # ...but the fingerprint-aware lookup falls back and finds it.
        self.assertEqual(engine.lookup_recovery_for(fp), "Check the widget.")

    def test_prefers_a_message_on_the_current_key(self):
        engine = CaptureEngine(api_key="k", base_url="https://ingest.example")
        fp = Fingerprint(
            "stack",
            "500:src/db/users.py:find_by_id",
            "",
            previous_key="500:GET:/users:old",
        )
        engine.recovery_cache.set(fp.previous_key, "stale guidance")
        engine.recovery_cache.set(fp.key, "current guidance")
        # Once the group has migrated, the new message wins.
        self.assertEqual(engine.lookup_recovery_for(fp), "current guidance")

    def test_no_previous_key_outside_the_stack_strategy(self):
        engine = CaptureEngine(api_key="k", base_url="https://ingest.example")
        captured = _captured()
        del captured["stackTrace"]
        fp = engine.compute_fingerprint(captured)
        self.assertEqual(fp.strategy, "message")
        self.assertIsNone(fp.previous_key)
        # Nothing displaced means nothing extra on the wire.
        self.assertNotIn("previousKey", fp.to_wire())


if __name__ == "__main__":
    unittest.main()
