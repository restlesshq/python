"""Python-dialect stack frame tests.

CONTRACT.md FP-044 makes stack frame PARSING per-language and FP-046 requires
each SDK to cover its own dialect in its own suite. The shared vectors carry
v8-shaped stacks, which this SDK reports as an unsupported dialect and the
harness records as skipped; these are the cases that replace them.

Only the OUTPUT shape is contract surface, so what is asserted here is
exactly FP-040 through FP-045: `{status}:{file}:{fn}`, no line numbers,
project-relative paths, vendor frames skipped.
"""

import unittest

from restless._fingerprint import fingerprint, project_relative, top_user_frame

REAL_TRACEBACK = '''Traceback (most recent call last):
  File "/Users/dev/proj/src/routes/users.py", line 42, in handler
    return load(user_id)
  File "/Users/dev/proj/src/db/users.py", line 12, in find_by_id
    raise ValueError("boom")
ValueError: boom
'''

VENDOR_FIRST = '''Traceback (most recent call last):
  File "/venv/lib/python3.11/site-packages/flask/app.py", line 100, in dispatch
    return f()
  File "/proj/src/db/users.py", line 12, in find_by_id
    raise ValueError("boom")
'''


class TestTopUserFrame(unittest.TestCase):
    def test_picks_the_frame_nearest_the_throw(self):
        # FP-043 wants the frame nearest the throw site, because that is what
        # distinguishes two different crashes. In a Python traceback that is
        # the LAST frame, not the first - the opposite end from a v8 stack.
        # Returning `handler` here would mean every crash routed through the
        # same entry point shared one fingerprint.
        self.assertEqual(top_user_frame(REAL_TRACEBACK), ("src/db/users.py", "find_by_id"))

    def test_skips_site_packages(self):
        self.assertEqual(top_user_frame(VENDOR_FIRST), ("src/db/users.py", "find_by_id"))

    def test_accepts_a_list_of_lines(self):
        self.assertEqual(
            top_user_frame(REAL_TRACEBACK.split("\n")), ("src/db/users.py", "find_by_id")
        )

    def test_two_crashes_in_different_functions_do_not_collide(self):
        """The regression this ordering exists to prevent."""
        a = fingerprint(status=500, route="/users", stack_trace=REAL_TRACEBACK)
        other = REAL_TRACEBACK.replace("find_by_id", "delete_by_id").replace(
            "db/users.py", "db/admin.py"
        )
        b = fingerprint(status=500, route="/users", stack_trace=other)
        self.assertNotEqual(a.key, b.key)

    def test_v8_stack_is_unparseable_here(self):
        # A v8 stack has no `File "...", line N, in fn` frames, so the ladder
        # simply falls through rather than guessing.
        v8 = 'Error: boom\n    at findById (/proj/src/db/users.js:12:34)'
        self.assertIsNone(top_user_frame(v8))

    def test_empty(self):
        self.assertIsNone(top_user_frame(None))
        self.assertIsNone(top_user_frame(""))


class TestProjectRelative(unittest.TestCase):
    def test_strips_machine_prefix(self):
        self.assertEqual(
            project_relative("/Users/dev/proj/src/db/users.py"), "src/db/users.py"
        )
        self.assertEqual(
            project_relative("/opt/render/project/src/db/users.py"), "src/db/users.py"
        )

    def test_matches_reference_on_app_rooted_paths(self):
        """Pins the CURRENT reference behaviour, defect included.

        This SDK's job is to agree with the reference, so it must reproduce
        this exactly. See ``test_docker_and_laptop_should_agree`` below for
        the defect itself.
        """
        self.assertEqual(project_relative("/app/src/db/users.py"), "app/src/db/users.py")
        self.assertEqual(
            project_relative("/srv/app/src/db/users.py"), "app/src/db/users.py"
        )

    @unittest.expectedFailure
    def test_docker_and_laptop_should_agree(self):
        """FP-042's stated intent, which the reference does not currently meet.

        Marked expectedFailure rather than deleted: it documents the defect
        executably, and it will start passing (and so fail loudly as an
        unexpected success) the moment the reference adopts last-match
        semantics. See the Known defect note under FP-042 - fixing it moves
        stored fingerprint keys and needs coordination across the ingest,
        the dashboard and every SDK.
        """
        self.assertEqual(
            project_relative("/Users/dev/proj/src/db/users.py"),
            project_relative("/app/src/db/users.py"),
        )

    def test_falls_back_to_last_two_segments(self):
        self.assertEqual(project_relative("/opt/weird/place/thing.py"), "place/thing.py")


class TestStackStrategy(unittest.TestCase):
    def test_key_shape(self):
        # FP-040
        fp = fingerprint(status=500, method="GET", route="/users", stack_trace=REAL_TRACEBACK)
        self.assertEqual(fp.strategy, "stack")
        self.assertEqual(fp.key, "500:src/db/users.py:find_by_id")

    def test_no_line_numbers_in_key(self):
        # FP-041. Adding a line above the raise must not split the group.
        shifted = REAL_TRACEBACK.replace("line 42", "line 91").replace("line 12", "line 77")
        a = fingerprint(status=500, route="/users", stack_trace=REAL_TRACEBACK)
        b = fingerprint(status=500, route="/users", stack_trace=shifted)
        self.assertEqual(a.key, b.key)

    def test_not_used_below_500(self):
        # FP-010. The stack strategy is 5xx only.
        fp = fingerprint(status=400, method="GET", route="/users", stack_trace=REAL_TRACEBACK)
        self.assertNotEqual(fp.strategy, "stack")

    def test_falls_through_when_no_user_frame(self):
        only_vendor = '  File "/venv/lib/python3.11/site-packages/x/y.py", line 1, in f\n'
        fp = fingerprint(status=500, method="GET", route="/users", stack_trace=only_vendor)
        self.assertEqual(fp.strategy, "route-only")


if __name__ == "__main__":
    unittest.main()
