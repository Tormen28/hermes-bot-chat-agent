import unittest

import lang_detect as L

CASES = {
    "en": ["Can you refactor this python function and fix the bug in the login endpoint?",
           "I want to create a landing page for my bakery", "Please remember that we use postgres"],
    "es": ["Quiero crear una pantalla de login para mi app",
           "Necesito que me ayudes con el diseño de la paleta de colores",
           "Hola, ¿puedes investigar las opciones de pago?"],
    "fr": ["Je voudrais créer une page d'accueil pour ma boulangerie",
           "Peux-tu corriger le bug dans la fonction de connexion ?", "Merci, c'est très bien"],
    "pt": ["Eu quero criar uma página para a minha padaria",
           "Você pode corrigir o erro na função de login?", "Preciso de ajuda com o projeto"],
    "de": ["Ich möchte eine Webseite für meine Bäckerei erstellen",
           "Kannst du den Fehler in der Login-Funktion beheben?", "Bitte hilf mir mit dem Projekt"],
    "": ["ok", "sí", "yes", "no", "refactor", "", "   ", "12345"],
}


class Detect(unittest.TestCase):
    def test_cases(self):
        for want, texts in CASES.items():
            for t in texts:
                self.assertEqual(L.detect(t), want, t)

    def test_resolve_order(self):
        # message beats sticky; unknown message keeps sticky; nothing known -> default
        self.assertEqual(L.resolve("Je voudrais créer une page pour ma boulangerie", sticky="es"), "fr")
        self.assertEqual(L.resolve("ok", sticky="de"), "de")
        self.assertEqual(L.resolve("ok"), "en")
        self.assertEqual(L.resolve("ok", sticky="xx"), "en")

    def test_explicit_config_overrides_detection(self):
        self.assertEqual(L.resolve("I want to create a landing page for my bakery", configured="es"), "es")
        self.assertEqual(L.resolve("I want to create a landing page", configured="auto"), "en")
        self.assertEqual(L.resolve("Quiero crear una pantalla de login para mi app", configured="zz"), "es")

    def test_norm(self):
        self.assertEqual(L.norm("es-VE"), "es")
        self.assertEqual(L.norm("ja"), "en")
        self.assertEqual(L.norm(None, "fr"), "fr")


if __name__ == "__main__":
    unittest.main()
