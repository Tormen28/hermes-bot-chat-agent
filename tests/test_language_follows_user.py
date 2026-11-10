"""Every user-facing string the plugin generates follows the language the user writes in."""
from __future__ import annotations

import json
import pathlib
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_bot_chat_agent import Base, bca  # noqa: E402

import memory_command  # noqa: E402
import panteon  # noqa: E402
import panteon_auto  # noqa: E402
import status_panel  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent

HELLO = {
    "en": "I want to create a landing page for my bakery please",
    "es": "Quiero crear una página de inicio para mi panadería, por favor",
    "fr": "Je voudrais créer une page d'accueil pour ma boulangerie",
    "pt": "Eu quero criar uma página para a minha padaria",
    "de": "Ich möchte eine Webseite für meine Bäckerei erstellen",
}
EMPTY = {"en": "(empty)", "es": "(vacío)", "fr": "(vide)", "pt": "(vazio)", "de": "(leer)"}
WAITING = {"en": "waiting for reply", "es": "esperando respuesta", "fr": "en attente de réponse",
           "pt": "aguardando resposta", "de": "wartet auf Antwort"}


class SessionLanguage(Base):
    def hook(self, text, sid="bot"):
        return self.ctx.hooks["pre_llm_call"](session_id=sid, user_message=text)

    def test_detected_from_the_message_each_language(self):
        for code, text in HELLO.items():
            bca._reset_language_state()
            self.hook(text)
            self.assertEqual(bca._ulang("bot"), code)

    def test_non_english_non_spanish_gets_a_reply_language_directive(self):
        out = self.hook(HELLO["fr"])["context"]
        self.assertIn("The user writes in French", out)
        out = self.hook(HELLO["de"])["context"]
        self.assertIn("The user writes in German", out)
        self.assertNotIn("writes in", self.hook(HELLO["en"])["context"])

    def test_spanish_user_gets_spanish_instructions(self):
        self.assertIn("[Estilo Bot Chat]", self.hook(HELLO["es"])["context"])

    def test_sticky_when_the_message_is_too_short_to_tell(self):
        self.hook(HELLO["fr"])
        self.hook("ok")
        self.assertEqual(bca._ulang("bot"), "fr")

    def test_system_notices_are_not_the_users_words(self):
        self.hook(HELLO["es"])
        self.hook("[IMPORTANT: Background process finished. The process completed with exit code 0 and the results are ready for you]")
        self.assertEqual(bca._ulang("bot"), "es")

    def test_follows_a_change_of_language(self):
        self.hook(HELLO["es"])
        self.hook(HELLO["en"])
        self.assertEqual(bca._ulang("bot"), "en")

    def test_explicit_config_beats_detection(self):
        self.ctx.cfg["language"] = "pt"
        self.hook(HELLO["fr"])
        self.assertEqual(bca._ulang("bot"), "pt")

    def test_default_is_english_when_nothing_is_known(self):
        self.assertEqual(bca._ulang("bot"), "en")
        self.assertEqual(bca._known_lang("bot"), "")

    def test_language_survives_a_restart(self):
        self.hook(HELLO["pt"])
        bca._session_lang.clear()
        bca._last_lang = ""
        bca._persisted = None  # as after a fresh process: re-read lang.json
        self.assertEqual(bca._ulang("another-session"), "pt")

    def test_status_card_snapshot_carries_the_language(self):
        self.hook(HELLO["fr"])
        bca._publish_status("bot")
        snap = json.loads(status_panel.status_path(bca._hermes_home()).read_text(encoding="utf-8"))
        self.assertEqual(snap["lang"], "fr")


class SlashCommands(Base):
    def test_botmemory_and_memoria_follow_the_user_not_the_command_name(self):
        for code, text in HELLO.items():
            bca._reset_language_state()
            self.ctx.hooks["pre_llm_call"](session_id="bot", user_message=text)
            for name in ("memoria", "botmemory"):
                out = self.ctx.commands[name]("list", session_id="bot")
                self.assertIn(EMPTY[code], out, f"{name} in {code}: {out}")

    def test_pantheon_list_follows_the_user(self):
        titles = {"en": "coordinator + specialised bots", "es": "coordinador + bots especializados",
                  "fr": "coordinateur + bots spécialisés", "pt": "coordenador + bots especializados",
                  "de": "Koordinator + spezialisierte Bots"}
        for code, text in HELLO.items():
            bca._reset_language_state()
            self.ctx.hooks["pre_llm_call"](session_id="bot", user_message=text)
            out = self.ctx.commands["pantheon"]("", session_id="bot")
            self.assertIn(titles[code], out, f"{code}: {out[:80]}")

    def test_unknown_language_falls_back_to_hints_then_english(self):
        self.assertIn("(empty)", self.ctx.commands["botmemory"]("list", session_id="x"))
        self.assertIn("(vacío)", self.ctx.commands["memoria"]("list", session_id="x"))


class Pure(unittest.TestCase):
    def test_memory_command_every_message_exists_in_every_language(self):
        for key, row in memory_command._MSG.items():
            for code in ("en", "es", "fr", "pt", "de"):
                self.assertTrue(row.get(code), f"{key}/{code}")
        for code in ("en", "es", "fr", "pt", "de"):
            self.assertTrue(memory_command._help(code))
            for head in memory_command._HEADS.values():
                self.assertTrue(head.get(code))

    def test_panteon_tables_cover_every_language(self):
        keys = set(panteon._T["en"])
        for code in ("es", "fr", "pt", "de"):
            self.assertEqual(set(panteon._T[code]), keys, code)
        for pid in panteon.PRESET_ORDER:
            for code in ("en", "es", "fr", "pt", "de"):
                self.assertTrue(panteon.domain_for(pid, code))

    def test_card_labels_in_every_language(self):
        for code, word in WAITING.items():
            line = status_panel.delegation_line({"from": "Hermes", "to": "daedalus", "state": "waiting"}, code)
            self.assertIn(word, line)
        for code in ("fr", "pt", "de"):
            stalled = status_panel.delegation_line(
                {"from": "Hermes", "to": "athena", "state": "waiting", "stalled": True}, code)
            self.assertIn(status_panel._STALLED[code], stalled)

    def test_panteon_offer_instruction_names_the_language(self):
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp()) / "p.json"
        panteon_auto.mark_desktop_seen(path=d)
        res = panteon_auto.step("s", "Peux-tu corriger le bug et écrire un test unitaire pour la fonction ?",
                                lang="fr", path=d, now=None, cfg={"auto": True})
        self.assertIn("French", res["context"])
        res_en = panteon_auto.step("s2", "Please refactor this python function and add a unit test",
                                   lang="en", path=d, cfg={"auto": True})
        self.assertNotIn("[Language]", res_en["context"])

    def test_desktop_card_has_every_language(self):
        src = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
        for code in ("fr:", "pt:", "de:"):
            self.assertGreaterEqual(src.count("\n  " + code), 3, code)  # I18N, TOOL_LABELS, DELEGATION_STATE
        self.assertIn("I18N[src.lang] ? src.lang : ''", src)
        self.assertIn("s.lang || (I18N[lang] ? lang : 'en')", src)


if __name__ == "__main__":
    unittest.main()


class YesNoAndKeywordsInEveryLanguage(unittest.TestCase):
    def test_yes_and_no(self):
        for t in ("oui", "Oui, crée-le", "d'accord", "Sim", "Sim, pode criar", "Ja", "Ja, bitte", "klar", "sí", "yes"):
            self.assertTrue(panteon_auto.is_yes(t), t)
            self.assertFalse(panteon_auto.is_no(t), t)
        for t in ("non", "non merci", "pas maintenant", "não", "agora não", "nein", "nein danke", "no", "not now"):
            self.assertTrue(panteon_auto.is_no(t), t)
            self.assertFalse(panteon_auto.is_yes(t), t)

    def test_specialty_detected_in_other_languages(self):
        self.assertEqual(panteon_auto.detect(
            "Peux-tu corriger le bug dans la fonction de connexion, il y a une erreur dans le test unitaire ?"), "daedalus")
        self.assertEqual(panteon_auto.detect(
            "Você pode corrigir o bug na função de login? O teste unitário falha"), "daedalus")
        self.assertEqual(panteon_auto.detect(
            "Kannst du den Fehler in der Funktion beheben? Es gibt einen Unit-Test der fehlschlägt"), "daedalus")
        self.assertEqual(panteon_auto.detect(
            "Je voudrais une maquette et une palette de couleurs pour mon écran d'accueil"), "iris")

    def test_no_false_offer_on_small_talk(self):
        for t in ("Hola qué tal cómo estás hoy", "Bonjour, comment vas-tu aujourd'hui ?",
                  "Ich möchte eine Webseite für meine Bäckerei erstellen"):
            self.assertIsNone(panteon_auto.detect(t), t)
