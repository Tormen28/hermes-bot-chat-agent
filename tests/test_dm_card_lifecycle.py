"""Delegation lines on the status card: failure path and clutter (found in the simulated new-user test:
'done 20m 1s' kept growing and a day of finished delegations piled up on the card)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dm_delegations as dm  # noqa: E402

ACK = {"status": "queued", "delivery_id": "d-1"}


def notice(code, name="daedalus", out="all good"):
    word = "completed normally" if code == 0 else "exited"
    return (f"[IMPORTANT: Background process proc_x {word} (exit code {code}).\n"
            f"Command: /venv/bin/python /core/tools/bot_relay.py --to {name}\nOutput:\n{out}]")


class TestLifecycle(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(prefix="bca-dm-")) / "dm.json"

    def _call(self, name="daedalus", did="d-1", now=1000.0):
        return dm.record_call({"target": name, "message": "do it"}, {**ACK, "delivery_id": did},
                              session_id="s", origin="Hermes", now=now, path=self.path)

    def test_non_zero_exit_marks_error_not_done(self):
        self._call()
        changed = dm.apply_completion(notice(1, out="Traceback ..."), now=1030.0, path=self.path)
        self.assertEqual([c["state"] for c in changed], ["error"])

    def test_exit_zero_with_scary_words_in_output_is_still_done(self):
        self._call()
        changed = dm.apply_completion(notice(0, out="0 errors, nothing failed"), now=1030.0, path=self.path)
        self.assertEqual([c["state"] for c in changed], ["ready"])

    def test_notice_for_someone_else_changes_nothing(self):
        self._call("daedalus")
        self.assertEqual(dm.apply_completion(notice(0, "athena"), now=1030.0, path=self.path), [])

    def test_two_bots_at_once_are_tracked_independently(self):
        self._call("daedalus", "d-1", 1000.0)
        self._call("athena", "d-2", 1001.0)
        dm.apply_completion(notice(0, "athena"), now=1030.0, path=self.path)
        rows = {r["assignee"]: r["status"] for r in dm.as_status_tasks("s", now=1031.0, path=self.path)}
        self.assertEqual(rows, {"daedalus": "queued", "athena": "ready"})

    def test_finished_work_leaves_the_card_but_failures_stay_longer(self):
        self._call("daedalus", "d-1", 1000.0)
        self._call("athena", "d-2", 1000.0)
        dm.apply_completion(notice(0, "daedalus"), now=1010.0, path=self.path)
        dm.apply_completion(notice(2, "athena"), now=1010.0, path=self.path)
        soon = {r["assignee"] for r in dm.as_status_tasks("s", now=1100.0, path=self.path)}
        self.assertEqual(soon, {"daedalus", "athena"})
        later = {r["assignee"] for r in dm.as_status_tasks("s", now=1010.0 + 20 * 60, path=self.path)}
        self.assertEqual(later, {"athena"}, "done is gone after 15 min, the failure is still visible")
        much_later = dm.as_status_tasks("s", now=1010.0 + 2 * 3600, path=self.path)
        self.assertEqual(much_later, [])

    def test_open_delegation_never_expires_from_the_card(self):
        self._call()
        rows = dm.as_status_tasks("s", now=1000.0 + 5 * 3600, path=self.path)
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
