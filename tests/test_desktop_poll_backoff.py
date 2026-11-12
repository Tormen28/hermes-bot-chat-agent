"""The Desktop half must not hammer a missing Python half (static source check)."""
import pathlib
import unittest

SRC = (pathlib.Path(__file__).resolve().parent.parent / "desktop" / "plugin.js").read_text(encoding="utf-8")


class PollBackoff(unittest.TestCase):
    def test_backs_off_after_repeated_status_failures(self):
        self.assertIn("misses >= 3", SRC)
        self.assertIn("++skipped < 20", SRC)
        self.assertIn("misses = 0", SRC, "a successful poll resets the back-off")


if __name__ == "__main__":
    unittest.main()
