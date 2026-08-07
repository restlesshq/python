"""Name normalization folds the full Unicode lowercase mapping.

REDACT-010 is a SECURITY requirement, not a cosmetic one: normalization is
what decides whether a value is redacted, so the fold has to be at least as
aggressive as the reference's. U+212A KELVIN SIGN lowercases to `k`, which
makes `toKen` spelled with it the denylisted name `token`.

An ASCII-only name fold is the easy mistake here. This SDK
implemented it as written, and the KELVIN SIGN spelling of every denylisted
name then survived unredacted and went to the dashboard in plaintext.
REDACT-010 requires full Unicode folding for exactly that reason. The shared vectors
now pin the KELVIN cases, but they are re-asserted here so a regression fails
`python -m unittest` on its own, without the Node harness.
"""

import unittest

from restless._redact import _normalize, redact_body, redact_headers, redact_url

KELVIN = "K"  # U+212A KELVIN SIGN, lowercases to ASCII 'k'


class TestUnicodeNameNormalization(unittest.TestCase):
    def test_kelvin_sign_normalizes_to_ascii_k(self):
        self.assertEqual(_normalize("to{}en".format(KELVIN)), "token")

    def test_separators_are_still_stripped(self):
        self.assertEqual(_normalize("X-API_Key"), "xapikey")

    def test_body_key_with_kelvin_sign_is_redacted(self):
        key = "to{}en".format(KELVIN)
        body = '{{"{}":"supersecretvalue"}}'.format(key)
        out = redact_body(body, "application/json")
        self.assertEqual(out, '{{"{}":"<REDACTED:16:alue>"}}'.format(key))
        self.assertNotIn("supersecretvalue", out)

    def test_header_with_kelvin_sign_is_redacted(self):
        name = "x-api-{}ey".format(KELVIN)
        out = redact_headers({name: "supersecretvalue"})
        self.assertEqual(out, {name: "<REDACTED:16:alue>"})

    def test_query_param_with_kelvin_sign_is_redacted(self):
        out = redact_url("https://x.test/p?to{}en=supersecretvalue".format(KELVIN))
        self.assertNotIn("supersecretvalue", out)
        self.assertIn("%3CREDACTED%3A16%3Aalue%3E", out)

    def test_ascii_names_are_unaffected(self):
        """The fold got wider, not different. Plain ASCII must not move."""
        self.assertEqual(_normalize("Authorization"), "authorization")
        self.assertEqual(
            redact_headers({"X-Trace-Id": "keepme"}), {"X-Trace-Id": "keepme"}
        )


if __name__ == "__main__":
    unittest.main()
