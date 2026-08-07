"""Replay the shared conformance vectors in-process.

The cross-language harness (``node spec/harness/run-vectors.mjs``) drives this
SDK through the stdio driver. This file does the same thing natively, so
``python -m unittest`` alone tells a Python developer whether they broke the
contract, without needing Node installed.

The vectors in ``spec/vectors/`` are vendored from the Node SDK at the
version in ``spec/VECTORS_VERSION``. They are generated from the reference
implementation; do not edit them here.
"""

import json
import os
import unittest

from restless._conformance.__main__ import OPS, UnsupportedDialect

VECTOR_DIR = os.path.join(os.path.dirname(__file__), "..", "spec", "vectors")


def _load():
    cases = []
    for name in sorted(os.listdir(VECTOR_DIR)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(VECTOR_DIR, name), "r", encoding="utf-8") as handle:
            doc = json.load(handle)
        for case in doc["cases"]:
            case["_file"] = name
            case["_specVersion"] = doc["specVersion"]
            cases.append(case)
    return cases


def _normalize(mode, value):
    """`compare: "json"` cases hold JSON *strings*; compare them parsed."""
    if mode == "json" and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


class TestConformanceVectors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = _load()

    def test_vectors_are_present(self):
        self.assertGreater(len(self.cases), 150, "vectors missing or truncated")

    def test_vector_version_matches_declared_spec_version(self):
        from restless._version import SPEC_VERSION

        versions = {c["_specVersion"] for c in self.cases}
        self.assertEqual(
            versions,
            {SPEC_VERSION},
            "vendored vectors are a different spec version than _version.SPEC_VERSION "
            "declares; re-vendor from the Node SDK or fix the declaration",
        )

    def test_all_vectors(self):
        failures = []
        skipped = 0
        for case in self.cases:
            op = OPS.get(case["op"])
            if op is None:
                skipped += 1
                continue
            try:
                actual = op(case["input"])
            except UnsupportedDialect:
                # FP-046. Reference-dialect cases; covered natively in
                # test_stack_frames.py instead.
                skipped += 1
                continue
            except Exception as err:  # noqa: BLE001
                failures.append("{} ({}): raised {!r}".format(case["id"], case["requirement"], err))
                continue

            mode = case.get("compare")
            if _normalize(mode, actual) != _normalize(mode, case["expected"]):
                failures.append(
                    "{} ({}):\n    expected {!r}\n    actual   {!r}".format(
                        case["id"], case["requirement"], case["expected"], actual
                    )
                )

        if failures:
            self.fail(
                "{} of {} vectors failed ({} skipped):\n\n".format(
                    len(failures), len(self.cases), skipped
                )
                + "\n".join(failures)
            )


if __name__ == "__main__":
    unittest.main()
