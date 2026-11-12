"""Memory cues beyond the Spanish/English 'Decision:' forms (found in the new-user simulated test:
'Remember that I prefer short answers' was silently not stored)."""
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot_chat_memory as bm  # noqa: E402


def kinds(user, assistant=""):
    return [(k, t) for k, t in bm.extract_memories(user, assistant)]


class TestCues(unittest.TestCase):
    def test_remember_that_english(self):
        self.assertIn(("decision", "I prefer short answers in English."),
                      kinds("Remember that I prefer short answers in English."))

    def test_recuerda_que_spanish(self):
        out = kinds("Recuerda que mi cliente paga los viernes.")
        self.assertTrue(any("cliente paga" in t for _k, t in out), out)

    def test_other_languages(self):
        self.assertTrue(kinds("Décision : nous utilisons PostgreSQL 16 pour Aurora."))
        self.assertTrue(kinds("Decisão: usamos PostgreSQL 16 no projeto Aurora."))
        self.assertTrue(kinds("Entscheidung: wir nutzen PostgreSQL 16 für Aurora."))
        self.assertTrue(any(k == "lesson" for k, _ in kinds("Lição: sempre fazer backup antes de migrar.")))

    def test_assistant_words_never_write_memory(self):
        self.assertEqual(kinds("ok", "I will remember that you prefer short answers always."), [])

    def test_secrets_still_blocked(self):
        self.assertEqual(kinds("Remember that my api_key is sk-abcdefgh12345678"), [])

    def test_mid_sentence_remember_is_not_a_cue(self):
        self.assertEqual(kinds("I can never remember my wifi passphrase these days"), [])
        self.assertEqual(kinds("No recuerdo mi contraseña de la tienda"), [])

    def test_second_sentence_cue(self):
        self.assertTrue(kinds("Thanks. Remember that invoices go out on Fridays."))

    def test_too_short_ignored(self):
        self.assertEqual(kinds("remember it"), [])


if __name__ == "__main__":
    unittest.main()
