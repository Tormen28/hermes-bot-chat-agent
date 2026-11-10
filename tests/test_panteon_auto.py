"""Auto panteón (offer -> yes -> create via the Desktop half -> team), message_agent delegations for
the status card, the plugin-local compactor, the registry cron adapter and the dashboard API."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import panteon_auto as pa  # noqa: E402
import dm_delegations as dm  # noqa: E402
import context_engine as ce  # noqa: E402
import cron_bridge as cb  # noqa: E402
from dashboard import plugin_api as api  # noqa: E402


class AutoBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bca-auto-"))
        self.path = self.tmp / "panteon-auto.json"
        self.t = 1_000_000.0

    def ready(self):
        pa.mark_desktop_seen(now=self.t, path=self.path)

    def step(self, msg, **kw):
        kw.setdefault("now", self.t)
        kw.setdefault("lang", "en")
        return pa.step("s1", msg, path=self.path, **kw)


CODE_ASK = "Please refactor this python function and fix the failing unit test in my repo"


class TestDetect(unittest.TestCase):
    def test_clear_request_matches_one_specialty(self):
        self.assertEqual(pa.detect(CODE_ASK), "daedalus")

    def test_vague_or_short_requests_match_nothing(self):
        self.assertIsNone(pa.detect("hola"))
        self.assertIsNone(pa.detect("what do you think about the weather today?"))

    def test_yes_no(self):
        for t in ("sí", "si", "yes", "ok créalo", "dale"):
            self.assertTrue(pa.is_yes(t), t)
        for t in ("no", "no gracias", "nope"):
            self.assertTrue(pa.is_no(t), t)
        self.assertFalse(pa.is_yes("sí pero primero explícame qué hace " * 3))
        self.assertFalse(pa.is_yes("no sé"))


class TestOfferFlow(AutoBase):
    def test_no_offer_without_the_desktop_half(self):
        self.assertEqual(self.step(CODE_ASK)["context"], "")

    def test_offer_is_one_line_instruction_and_creates_nothing(self):
        self.ready()
        out = self.step(CODE_ASK)
        self.assertIn("Daedalus", out["context"])
        self.assertIn("ONE short", out["context"])
        self.assertEqual(out["queued"], [])
        self.assertEqual(pa.pending_requests(now=self.t, path=self.path), [])

    def test_unrelated_message_after_offer_creates_nothing(self):
        self.ready(); self.step(CODE_ASK)
        out = self.step("tell me a joke about cats please")
        self.assertEqual(out["queued"], [])
        self.assertEqual(pa.pending_requests(now=self.t, path=self.path), [])

    def test_yes_queues_exactly_one_request_and_waits_bounded(self):
        self.ready(); self.step(CODE_ASK)
        waited = []
        out = self.step("yes", wait=lambda rid, s: waited.append((rid, s)) or "pending", cfg={"create_wait_seconds": 3})
        self.assertEqual(out["queued"], ["daedalus"])
        self.assertEqual(waited[0][1], 3)
        req = pa.pending_requests(now=self.t, path=self.path)
        self.assertEqual([r["name"] for r in req], ["daedalus"])
        self.assertEqual(req[0]["ui_meta"]["title"], "Daedalus")  # the Bots row shows the god's name
        self.assertTrue(req[0]["soul"])

    def test_created_result_adds_to_team_and_tells_the_coordinator(self):
        self.ready(); self.step(CODE_ASK)

        def wait(rid, secs):
            pa.record_result(rid, True, now=self.t, path=self.path)
            return "created"
        out = self.step("sí", wait=wait)
        self.assertEqual(out["team_add"], ["daedalus"])
        self.assertIn("message_agent", out["context"])

    def test_failed_result_is_reported_not_hidden(self):
        self.ready(); self.step(CODE_ASK)

        def wait(rid, secs):
            pa.record_result(rid, False, error="boom", now=self.t, path=self.path)
            return "failed"
        out = self.step("yes", wait=wait)
        self.assertEqual(out["team_add"], [])
        self.assertIn("boom", out["context"])

    def test_no_declines_and_cooldown_blocks_re_offer(self):
        self.ready(); self.step(CODE_ASK)
        self.assertEqual(self.step("no")["queued"], [])
        self.assertEqual(self.step(CODE_ASK)["context"], "")
        later = self.t + pa.DECLINE_COOLDOWN_SECONDS + 5
        pa.mark_desktop_seen(now=later, path=self.path)
        self.assertIn("Daedalus", pa.step("s1", CODE_ASK, path=self.path, now=later, lang="en")["context"])

    def test_offer_expires(self):
        self.ready(); self.step(CODE_ASK)
        later = self.t + pa.OFFER_TTL_SECONDS + 5
        pa.mark_desktop_seen(now=later, path=self.path)
        out = pa.step("s1", "yes", path=self.path, now=later, lang="en")
        self.assertEqual(out["queued"], [])

    def test_spanish_offer(self):
        self.ready()
        out = self.step("Arregla esta función de python y el test unitario que falla en mi repo", lang="es")
        self.assertIn("Puedo crear a Daedalus", out["context"])

    def test_existing_god_in_team_is_not_offered_again(self):
        self.ready()
        out = self.step(CODE_ASK, team=["daedalus"], existing=["daedalus"])
        self.assertEqual(out["context"], "")

    def test_existing_god_not_in_team_is_added_on_yes_without_creating(self):
        self.ready(); self.step(CODE_ASK)  # offered
        out = self.step("yes", existing=["daedalus"])
        self.assertEqual(out["queued"], [])
        self.assertEqual(out["team_add"], ["daedalus"])

    def test_auto_off_and_god_chat_are_inert(self):
        self.ready()
        self.assertEqual(self.step(CODE_ASK, cfg={"auto": False})["context"], "")
        self.assertEqual(self.step(CODE_ASK, is_god_chat=True)["context"], "")

    def test_first_run_suggestion_is_one_time_and_needs_a_yes(self):
        self.ready()
        out = self.step("hola, ¿me ayudas con varias cosas hoy?", lang="es")
        self.assertTrue(out["context"])
        self.assertEqual(pa.pending_requests(now=self.t, path=self.path), [])
        again = self.step("otra cosa distinta de hoy por favor", lang="es")
        self.assertEqual(again["context"], "")

    def test_handoff_reminder_when_never_delegated_then_stops(self):
        self.ready(); self.step(CODE_ASK)
        self.step("yes", wait=lambda rid, s: (pa.record_result(rid, True, now=self.t, path=self.path), "created")[1])
        r1 = self.step("hello again", delegated=lambda n, since: False)
        r2 = self.step("hello once more", delegated=lambda n, since: False)
        r3 = self.step("hello a third time", delegated=lambda n, since: False)
        self.assertIn("daedalus", r1["context"].lower())
        self.assertIn("daedalus", r2["context"].lower())
        self.assertNotIn("daedalus", r3["context"].lower())

    def test_no_reminder_once_delegated(self):
        self.ready(); self.step(CODE_ASK)
        self.step("yes", wait=lambda rid, s: (pa.record_result(rid, True, now=self.t, path=self.path), "created")[1])
        out = self.step("hello again", delegated=lambda n, since: True)
        self.assertNotIn("daedalus", out["context"].lower())


class TestQueue(AutoBase):
    def test_desktop_heartbeat_expires(self):
        self.ready()
        self.assertTrue(pa.desktop_ready(now=self.t + 10, path=self.path))
        self.assertFalse(pa.desktop_ready(now=self.t + pa.DESKTOP_SEEN_TTL_SECONDS + 1, path=self.path))

    def test_one_live_request_per_name_and_result_is_recorded_once(self):
        a = pa.enqueue_request("daedalus", "daedalus", now=self.t, path=self.path)
        b = pa.enqueue_request("daedalus", "daedalus", now=self.t, path=self.path)
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(pa.record_result(a["id"], True, now=self.t, path=self.path))
        self.assertFalse(pa.record_result(a["id"], True, now=self.t, path=self.path))
        self.assertEqual(pa.wait_for_result(a["id"], 1, path=self.path), "created")

    def test_wait_is_bounded(self):
        a = pa.enqueue_request("iris", "iris", now=self.t, path=self.path)
        clock = iter(range(0, 100)).__next__
        self.assertEqual(pa.wait_for_result(a["id"], 2, path=self.path, sleep=lambda s: None,
                                            clock=lambda: float(clock())), "pending")

    def test_stale_requests_are_not_served(self):
        pa.enqueue_request("iris", "iris", now=self.t, path=self.path)
        self.assertEqual(pa.pending_requests(now=self.t + pa.REQUEST_TTL_SECONDS + 5, path=self.path), [])


class TestDashboardApi(AutoBase):
    def setUp(self):
        super().setUp()
        import os
        self._old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.tmp)

    def tearDown(self):
        import os
        if self._old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old

    def test_hello_result_and_status_roundtrip(self):
        self.assertTrue(api.hello()["ok"])
        self.assertTrue(pa.desktop_ready())
        req = pa.enqueue_request("iris", "iris")
        self.assertEqual([r["id"] for r in api.get_status()["panteon_requests"]], [req["id"]])
        self.assertTrue(api.record_result({"id": req["id"], "ok": True})["ok"])
        self.assertEqual(api.get_status()["panteon_requests"], [])
        self.assertFalse(api.record_result("garbage")["ok"])


class TestDelegations(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(prefix="bca-dm-")) / "dm.json"

    def ack(self, to="daedalus", did="d1", **kw):
        return dm.record_call({"target": to, "message": "do the thing"}, json.dumps(
            {"status": "queued", "delivery_id": did, "to": to}), session_id="s1", origin="hermes",
            path=self.path, **kw)

    def test_ack_is_waiting_never_done(self):
        self.assertEqual(self.ack()["state"], "queued")
        row = dm.as_status_tasks("s1", path=self.path)[0]
        self.assertEqual((row["status"], row["assignee"]), ("queued", "daedalus"))

    def test_error_ack_and_garbage_results(self):
        it = dm.record_call({"target": "x"}, json.dumps({"error": "no such bot"}), path=self.path)
        self.assertEqual(it["state"], "error")
        self.assertIsNone(dm.record_call({"target": "x"}, "not json", path=self.path))
        self.assertIsNone(dm.record_call({"target": ""}, json.dumps({"status": "queued", "delivery_id": "q"}), path=self.path))

    def test_duplicate_ack_ignored(self):
        self.ack(); self.assertIsNone(self.ack())

    NOTICE_OK = ("[IMPORTANT: Background process proc_1 completed normally (exit code 0)]\n"
                 "message_agent daedalus finished")
    NOTICE_BAD = "[IMPORTANT: Background process proc_1 failed (exit code 2)]\nmessage_agent daedalus"

    def test_completion_with_evidence_marks_ready(self):
        self.ack()
        done = dm.apply_completion(self.NOTICE_OK, path=self.path)
        self.assertEqual([d["state"] for d in done], ["ready"])
        self.assertEqual(dm.as_status_tasks("s1", path=self.path)[0]["status"], "ready")

    def test_failure_notice_marks_error(self):
        self.ack()
        self.assertEqual(dm.apply_completion(self.NOTICE_BAD, path=self.path)[0]["state"], "error")

    def test_ordinary_text_never_marks_done(self):
        self.ack()
        for txt in ("daedalus finished the thing, thanks!", "I completed my homework", "", None,
                    "[IMPORTANT: Background process proc_9 completed normally (exit code 0)] for someone else"):
            self.assertEqual(dm.apply_completion(txt, path=self.path), [], txt)
        self.assertEqual(dm.as_status_tasks("s1", path=self.path)[0]["status"], "queued")

    def test_notice_for_another_bot_does_not_close_this_one(self):
        self.ack(); self.ack("iris", "d2")
        dm.apply_completion(self.NOTICE_OK, path=self.path)
        st = {r["assignee"]: r["status"] for r in dm.as_status_tasks("s1", path=self.path)}
        self.assertEqual(st, {"daedalus": "ready", "iris": "queued"})

    def test_has_call_to(self):
        self.ack(now=500.0)
        self.assertTrue(dm.has_call_to("Daedalus", 400.0, path=self.path))
        self.assertFalse(dm.has_call_to("daedalus", 600.0, path=self.path))
        self.assertFalse(dm.has_call_to("iris", 0.0, path=self.path))


class TestLocalCompactor(unittest.TestCase):
    def msgs(self, n):
        out = []
        for i in range(n):
            out.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"message number {i} " * 30})
        return out

    def test_short_history_is_untouched(self):
        c = ce.LocalCompactor(protect_first_n=3, protect_last_n=6)
        m = self.msgs(8)
        self.assertEqual(c.compress(m), m)

    def test_middle_replaced_by_one_note_head_and_tail_kept(self):
        c = ce.LocalCompactor(protect_first_n=2, protect_last_n=3)
        m = self.msgs(30)
        out = c.compress(m, memory_context="MEMORY BLOCK", focus_topic="x")
        self.assertEqual(out[:2], m[:2]); self.assertEqual(out[-3:], m[-3:])
        self.assertEqual(len(out), 6)
        note = out[2]["content"]
        self.assertTrue(note.startswith(ce.LocalCompactor.NOTE_HEADING))
        self.assertIn("MEMORY BLOCK", note)
        self.assertLessEqual(len(note), ce.LocalCompactor.NOTE_MAX_CHARS + 10)
        self.assertEqual(c.compression_count, 1)

    def test_tool_call_and_result_never_split(self):
        c = ce.LocalCompactor(protect_first_n=1, protect_last_n=2)
        m = self.msgs(10)
        m[7] = {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]}
        m[8] = {"role": "tool", "tool_call_id": "t1", "content": "result"}
        out = c.compress(m)
        for i, x in enumerate(out):
            if x.get("role") == "tool":
                self.assertTrue(out[i - 1].get("tool_calls"), "tool result lost its call")

    def test_threshold(self):
        c = ce.LocalCompactor(context_length=1000, threshold_percent=0.5)
        self.assertTrue(c.should_compress(600)); self.assertFalse(c.should_compress(100))
        self.assertFalse(ce.LocalCompactor().should_compress(10**9))


class TestRegistryCronAdapter(unittest.TestCase):
    class Reg:
        def __init__(self, reply): self.reply, self.calls = reply, []
        def dispatch(self, name, args): self.calls.append((name, args)); return self.reply

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="bca-cron-"))
        (self.home / "cron").mkdir()
        (self.home / "cron" / "jobs.json").write_text(json.dumps([{"id": "j1", "name": "n", "state": "paused"}]))

    def test_writes_go_through_cronjob_manage_only(self):
        reg = self.Reg(json.dumps({"success": True}))
        a = cb._RegistryAdapter(self.home, reg)
        a.pause_job("j1", "why"); a.resume_job("j1"); self.assertTrue(a.remove_job("j1"))
        self.assertEqual([c[0] for c in reg.calls], ["cronjob_manage"] * 3)
        self.assertEqual([c[1]["action"] for c in reg.calls], ["pause", "resume", "remove"])
        self.assertEqual(json.loads((self.home / "cron" / "jobs.json").read_text())[0]["id"], "j1")  # not written by us

    def test_tool_error_surfaces_as_cron_unavailable(self):
        a = cb._RegistryAdapter(self.home, self.Reg(json.dumps({"error": "nope"})))
        with self.assertRaises(cb.CronUnavailable):
            a.pause_job("j1")

    def test_no_private_core_imports_left(self):
        for f in ("cron_bridge.py", "context_engine.py", "panteon.py", "panteon_auto.py", "dm_delegations.py"):
            src = (ROOT / f).read_text(encoding="utf-8")
            for bad in ("from cron.", "import cron.", "agent.context_compressor", "hermes_cli.profiles"):
                code = [l for l in src.splitlines() if bad in l and not l.lstrip().startswith(("#", '"', "*"))
                        and "import" in l]
                self.assertEqual(code, [], f"{f}: {bad}")


if __name__ == "__main__":
    unittest.main()
