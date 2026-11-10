"""The core's bot loop guard refuses a message_agent call: say why and what to do, in the user's language."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_bot_chat_agent import Base, bca  # noqa: E402

import dm_delegations as dm  # noqa: E402
import status_panel as sp  # noqa: E402

HOP = ("This bot-to-bot exchange already reached its limit of 4 messages (gateway.bot_loop_guard.max_hops), "
       "so nothing was sent. Do not retry; finish your turn and tell your user where it stands.")
NOREPLY = ("That bot sent you a reply that needs no answer, so this exchange is over. Do not message them "
           "again in this turn; tell your user what you learned instead.")
DUP = ("You already sent this exact message to that bot in this exchange, so it was not sent again. "
       "Wait for their answer or say something new.")
LANGS = ("en", "es", "fr", "pt", "de")
SAY = {"en": "I want to create a landing page for my bakery please",
       "es": "Quiero crear una página de inicio para mi panadería, por favor",
       "fr": "Je voudrais créer une page d'accueil pour ma boulangerie",
       "pt": "Eu quero criar uma página para a minha padaria",
       "de": "Ich möchte eine Webseite für meine Bäckerei erstellen"}


class Classify(unittest.TestCase):
    def test_the_three_core_refusals(self):
        self.assertEqual(dm.classify_refusal(HOP), ("hop_limit", 4))
        self.assertEqual(dm.classify_refusal(NOREPLY), ("no_reply_needed", 0))
        self.assertEqual(dm.classify_refusal(DUP), ("duplicate", 0))
        self.assertEqual(dm.classify_refusal("connection refused"), ("", 0))
        self.assertEqual(dm.classify_refusal(HOP.replace("4 messages", "7 messages"))[1], 7)

    def test_record_keeps_the_reason(self):
        p = pathlib.Path(tempfile.mkdtemp()) / "dm.json"
        item = dm.record_call({"target": "daedalus", "message": "fix it"}, json.dumps({"error": HOP}),
                              session_id="s", origin="Hermes", path=p)
        self.assertEqual((item["state"], item["reason"], item["limit"]), ("error", "hop_limit", 4))
        rows = dm.as_status_tasks("s", path=p)
        self.assertEqual((rows[0]["status"], rows[0]["reason"], rows[0]["limit"]), ("error", "hop_limit", 4))

    def test_plain_error_has_no_reason(self):
        p = pathlib.Path(tempfile.mkdtemp()) / "dm.json"
        item = dm.record_call({"target": "daedalus"}, json.dumps({"error": "timeout"}), path=p)
        self.assertNotIn("reason", item)


class Card(unittest.TestCase):
    def snapshot(self, lang, reason="hop_limit"):
        now = 1_000_000.0
        task = {"task_id": "dm:1", "title": "fix it", "status": "error", "assignee": "daedalus",
                "created_at": now - 5, "updated_at": now - 5, "reason": reason, "limit": 4}
        return sp.snapshot(lang=lang, origin="Hermes", session_id="s", now=now, tasks=[task])

    def test_card_explains_in_every_language(self):
        for lang in LANGS:
            d = self.snapshot(lang)["delegations"][0]
            self.assertTrue(d["note"], lang)
            self.assertIn("4", d["note"], lang)
            line = sp.delegation_line(d, lang)
            self.assertIn(sp._DELEGATION_STATE[lang]["stopped"], line, lang)
            self.assertNotIn(sp._DELEGATION_STATE[lang]["error"] + "\n", line)

    def test_english_note_says_what_to_do(self):
        note = self.snapshot("en")["delegations"][0]["note"]
        self.assertIn("answer directly", note)
        self.assertIn("new message", note)

    def test_a_plain_error_still_reads_error(self):
        snap = self.snapshot("en", reason="")
        d = snap["delegations"][0]
        self.assertNotIn("note", d)
        self.assertIn("error", sp.delegation_line(d, "en"))

    def test_note_survives_refresh_live(self):
        snap = sp.refresh_live(self.snapshot("es"), now=1_000_100.0)
        self.assertIn("límite de 4", snap["delegations"][0]["note"])

    def test_desktop_card_shows_the_note(self):
        src = (pathlib.Path(__file__).resolve().parent.parent / "desktop" / "plugin.js").read_text(encoding="utf-8")
        self.assertIn("if (key === 'error' && row.reason) key = 'stopped'", src)
        self.assertIn("${text} — ${note}", src)
        for word in ("stopped", "detenida", "arrêtée", "interrompida", "gestoppt"):
            self.assertIn(f"stopped: '{word}'", src)


class ChatNotice(Base):
    def run_transform(self, lang, error=HOP):
        bca._reset_language_state()
        self.ctx.hooks["pre_llm_call"](session_id="bot", user_message=SAY[lang])
        fn = self.ctx.hooks["transform_tool_result"]
        return fn(tool_name="message_agent", args={"target": "daedalus", "message": "x"},
                  result=json.dumps({"error": error, "reason": "unknown"}), session_id="bot")

    def test_notice_in_every_language_and_still_json(self):
        names = {"en": "English", "es": "Spanish", "fr": "French", "pt": "Portuguese", "de": "German"}
        for lang in LANGS:
            out = self.run_transform(lang)
            data = json.loads(out)
            self.assertEqual(data["error"], HOP, "the core's own text is left untouched")
            note = data["bot_chat_note"]
            self.assertIn(f"in {names[lang]}", note, lang)
            self.assertIn("Do not retry", note)
            self.assertNotIn("[bot-chat-agent]", note, "the Desktop bar treats that marker as a guard block")
            self.assertIn("4", note)

    def test_each_reason_gets_a_notice(self):
        for text in (HOP, NOREPLY, DUP):
            self.assertIn("bot_chat_note", json.loads(self.run_transform("en", text)))

    def test_other_errors_and_tools_are_untouched(self):
        self.assertIsNone(self.run_transform("en", "no such bot"))
        fn = self.ctx.hooks["transform_tool_result"]
        self.assertIsNone(fn(tool_name="terminal", args={}, result=json.dumps({"error": HOP}), session_id="bot"))

    def test_not_applied_outside_bot_chat(self):
        fn = self.ctx.hooks["transform_tool_result"]
        self.assertIsNone(fn(tool_name="message_agent", args={"target": "x"},
                             result=json.dumps({"error": HOP}), session_id="other"))

    def test_idempotent(self):
        once = self.run_transform("en")
        fn = self.ctx.hooks["transform_tool_result"]
        self.assertIsNone(fn(tool_name="message_agent", args={"target": "daedalus"}, result=once, session_id="bot"))

    def test_post_tool_call_still_records_the_reason_from_the_noted_result(self):
        out = self.run_transform("fr")
        home = pathlib.Path(bca._hermes_home())
        dm.record_call({"target": "daedalus", "message": "x"}, out, session_id="bot", origin="Hermes",
                       path=home / "bot-chat-agent" / "dm-test.json")
        rows = dm._load(home / "bot-chat-agent" / "dm-test.json")
        self.assertEqual(rows[-1]["reason"], "hop_limit")


if __name__ == "__main__":
    unittest.main()
