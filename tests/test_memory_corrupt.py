"""A corrupt memory.db must not leave memory dead forever (found in the simulated new-user test:
random bytes in the file -> every turn fine, nothing ever stored, /memoria says "(vacío)")."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot_chat_memory as bm  # noqa: E402


class TestCorruptStore(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bca-corrupt-"))
        self.path = self.dir / "memory.db"

    def test_garbage_file_is_quarantined_and_memory_works_again(self):
        self.path.write_bytes(os.urandom(4096))
        store = bm.MemoryStore(self.path)
        mid = store.upsert("decision", "use PostgreSQL 16 for Aurora")
        self.assertIsNotNone(mid)
        got = store.retrieve("which database for Aurora", budget_tokens=200)
        self.assertEqual(len(got), 1)
        moved = list(self.dir.glob("memory.db.corrupt-*"))
        self.assertEqual(len(moved), 1, "the broken file is kept aside, not deleted")

    def test_empty_file_is_fine_and_not_quarantined(self):
        self.path.write_bytes(b"")
        store = bm.MemoryStore(self.path)
        self.assertIsNotNone(store.upsert("lesson", "always back up before migrating"))
        self.assertEqual(list(self.dir.glob("memory.db.corrupt-*")), [])

    def test_locked_store_is_not_treated_as_corrupt(self):
        store = bm.MemoryStore(self.path)
        self.assertIsNotNone(store.upsert("decision", "weekly releases on Fridays"))
        import sqlite3
        blocker = sqlite3.connect(self.path, timeout=0.1)
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            quick = bm.MemoryStore(self.path)
            quick.upsert("decision", "this write may fail while locked")  # must not quarantine
        finally:
            blocker.rollback(); blocker.close()
        self.assertEqual(list(self.dir.glob("memory.db.corrupt-*")), [])
        self.assertTrue(self.path.exists())
        self.assertTrue(any("Fridays" in r["text"] for r in
                            bm.MemoryStore(self.path).retrieve("releases Fridays", budget_tokens=200)))


if __name__ == "__main__":
    unittest.main()
