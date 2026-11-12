"""Regression: the bot-chat context engine must actually compact once the core has told it the window.

Found in the full simulated test (150-300 turn run on the real core loop): ``update_model`` was called
before the lazy compactor existed, so the window was dropped, the compactor got ``context_length=0``
(threshold 0) and ``should_compress`` was False forever: the history grew to 600 messages / ~105k tokens
on a 64k model.
"""
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import context_engine as ce  # noqa: E402


def _msgs(n):
    out = []
    for i in range(n):
        out.append({"role": "user", "content": f"question {i} " + "x" * 200})
        out.append({"role": "assistant", "content": f"answer {i} " + "y" * 200})
    return out


class TestEngineWindow(unittest.TestCase):
    def test_update_model_before_first_use_sets_the_window(self):
        eng = ce.BotChatContextEngine(model="m")
        eng.update_model("m", 64000)  # exactly what the core does at init, before any delegate exists
        self.assertFalse(eng.should_compress(1000))
        self.assertTrue(eng.should_compress(60000))
        self.assertFalse(eng.should_compress(40000))  # 75% of 64k = 48k

    def test_usage_drives_should_compress(self):
        eng = ce.BotChatContextEngine(model="m")
        eng.update_model("m", 64000)
        eng.update_from_response({"prompt_tokens": 50000, "completion_tokens": 100})
        self.assertTrue(eng.should_compress())

    def test_clone_keeps_the_window(self):
        eng = ce.BotChatContextEngine(model="m")
        eng.update_model("m", 64000)
        clone = eng.clone_for_agent()
        self.assertTrue(clone.should_compress(60000))

    def test_unknown_window_never_compacts_and_never_raises(self):
        eng = ce.BotChatContextEngine(model="m")
        self.assertFalse(eng.should_compress(10**9))
        eng.update_model("m", 0)
        self.assertFalse(eng.should_compress(10**9))

    def test_compaction_shrinks_history(self):
        eng = ce.BotChatContextEngine(model="m")
        eng.update_model("m", 64000)
        before = _msgs(100)
        after = eng.compress(before)
        self.assertLess(len(after), 20)
        self.assertEqual(eng.compression_count, 1)


if __name__ == "__main__":
    unittest.main()
