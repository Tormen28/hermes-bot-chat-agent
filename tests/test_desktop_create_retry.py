"""Source contract for the Desktop half's profile creation (a live run on a brand-new HERMES_HOME failed the
first ``profiles.create`` with ENOENT while the first skills sync was still writing; the second try works).
Static check: the JS cannot run without the Hermes renderer."""
import re
import unittest
from pathlib import Path

JS = (Path(__file__).resolve().parents[1] / "desktop" / "plugin.js").read_text(encoding="utf-8")


class TestCreateRetry(unittest.TestCase):
    def test_profiles_create_is_retried_and_exists_counts_as_done(self):
        block = JS[JS.index("host.request('profiles.create'") - 400: JS.index("host.request('profiles.create'") + 900]
        self.assertRegex(block, r"for \(let attempt = 0; attempt < 3; attempt\+\+\)")
        self.assertIn("/exist/i.test(error)", block)
        self.assertTrue(re.search(r"setTimeout\(resolve, 1500\)", block))


if __name__ == "__main__":
    unittest.main()
