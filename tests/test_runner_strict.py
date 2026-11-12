"""The strict runner's judges: the weaknesses found in the 0.3.0-dev audit must stay closed."""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("stability_runner", ROOT / "tools" / "stability" / "runner.py")
runner = importlib.util.module_from_spec(spec)
sys.modules["stability_runner"] = runner
spec.loader.exec_module(runner)


class TestJudgeQuick(unittest.TestCase):
    def test_numeric_ok_count_is_not_truthy(self):
        # old runner: {"ok": 39, "fallos": [...]} counted as ok because bool(39) is True
        out = json.dumps({"total": 40, "ok": 39, "fallos": [{"label": "x"}]})
        ok, why = runner.judge_quick(0, out)
        self.assertFalse(ok, why)
        ok, why = runner.judge_quick(0, json.dumps({"total": 40, "ok": 40, "fallos": []}))
        self.assertFalse(ok, "ok must be the boolean True, not a count")

    def test_exit_code_is_never_ignored_when_json_has_ok(self):
        self.assertFalse(runner.judge_quick(1, json.dumps({"ok": True}))[0])

    def test_clean_run(self):
        self.assertTrue(runner.judge_quick(0, "noise\n" + json.dumps({"ok": True, "fallos": []}))[0])
        self.assertTrue(runner.judge_quick(0, "plain text, no json")[0])

    def test_failures_list_fails_even_if_ok_missing(self):
        self.assertFalse(runner.judge_quick(0, json.dumps({"fallos": ["a"]}))[0])

    def test_timeout_and_garbage(self):
        self.assertFalse(runner.judge_quick(0, "", timed_out=True)[0])
        self.assertIsNone(runner.last_json("no braces"))
        self.assertEqual(runner.last_json('x {"a": 1} y {"b": 2}'), {"b": 2})


PYTEST_OUT = """\
=========================== short test summary info ============================
FAILED tests/test_a.py::TestA::test_one - AssertionError: boom
FAILED tests/test_b.py::TestB::test_two
ERROR tests/test_c.py::TestC::test_three
3 failed, 188 passed, 2 skipped in 90.1s (0:01:30)
"""


class TestBattery(unittest.TestCase):
    def test_parse(self):
        p = runner.parse_pytest(PYTEST_OUT)
        self.assertEqual(p["failed"], {"tests/test_a.py::TestA::test_one", "tests/test_b.py::TestB::test_two",
                                       "tests/test_c.py::TestC::test_three"})
        self.assertEqual((p["passed"], p["skipped"]), (188, 2))

    def test_same_count_different_names_is_a_regression(self):
        # 12 known failures; one gets fixed and a NEW one appears: counts are identical, names are not.
        known = {f"t{i}" for i in range(12)}
        got = (known - {"t0"}) | {"NEW"}
        parsed = {"failed": got, "passed": 188, "summary_line": "12 failed, 188 passed"}
        ok, why = runner.judge_battery(parsed, known, 188, 1)
        self.assertFalse(ok)
        self.assertIn("NEW", why)

    def test_improvement_must_be_acknowledged(self):
        parsed = {"failed": set(), "passed": 215, "summary_line": "215 passed"}
        ok, why = runner.judge_battery(parsed, {"t1"}, 200, 0)
        self.assertFalse(ok)
        self.assertIn("baseline improved", why)

    def test_clean_baseline(self):
        parsed = {"failed": set(), "passed": 215, "summary_line": "215 passed, 2 skipped"}
        self.assertTrue(runner.judge_battery(parsed, set(), 210, 0)[0])
        self.assertFalse(runner.judge_battery(parsed, set(), 300, 0)[0], "min_passed")
        self.assertFalse(runner.judge_battery({"failed": set(), "passed": 0, "summary_line": ""}, set(), 1, 0)[0])
        self.assertFalse(runner.judge_battery(parsed, set(), 210, 1)[0], "non-zero exit with empty baseline")

    def test_baseline_file_is_valid(self):
        failed, min_passed = runner.load_baseline(ROOT / "tools" / "stability" / "known_failures.json")
        self.assertEqual(failed, set())
        self.assertGreater(min_passed, 100)


class TestNotify(unittest.TestCase):
    def test_reports_failure_when_cli_missing(self):
        res = runner.notify("x", runner=["definitely-not-a-real-binary-xyz"])
        self.assertFalse(res["sent"])

    def test_reports_nonzero_exit_and_scrubs_tokens(self):
        py = sys.executable
        res = runner.notify("x", runner=[py, "-c",
                            "import sys; print('err 123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12345'); sys.exit(3)", "--"])
        self.assertFalse(res["sent"])
        self.assertEqual(res["returncode"], 3)
        self.assertNotIn("ABCDEFGHIJKLMNOP", res["detail"])

    def test_reports_success(self):
        res = runner.notify("hello", runner=[sys.executable, "-c", "import sys; print('queued')", "--"])
        self.assertTrue(res["sent"])


if __name__ == "__main__":
    unittest.main()
