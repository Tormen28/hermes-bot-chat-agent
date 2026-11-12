"""Which language is the user writing in? (es / en / fr / pt / de, "" when unsure)

Tiny, offline, deterministic: it scores a message against short lists of very common function
words and a few accented characters per language. It is meant for chat-length text; a bare
"ok" or a single technical word returns "" so the caller keeps the language it already had
(sticky per session) or falls back to English.
"""
from __future__ import annotations

import re
from typing import Optional

SUPPORTED = ("en", "es", "fr", "pt", "de")
NAMES = {"en": "English", "es": "Spanish", "fr": "French", "pt": "Portuguese", "de": "German"}

_WORDS = {
    "en": "the and is are was you your with for that this have what can please not from will would "
          "how why when about make want need create build give tell into just it's don't i'm i'd "
          "my me do does did should could there their which where who".split(),
    "es": "el la los las de que y en un una es por con para no se su al lo como más pero sus le ya "
          "o este sí porque esta entre cuando muy sin sobre también me hasta hay donde quien desde "
          "todo nos durante estos mi tu quiero necesito puedes hazme dime crea crear ayuda gracias "
          "hola está estoy tengo".split(),
    "fr": "le la les des du de un une et est en que qui dans pour pas sur ce il elle nous vous "
          "ils avec sont au aux mais ou où comme plus par je tu j'ai c'est ça veux voudrais peux "
          "besoin merci bonjour s'il très aussi être avoir fais fait créer crée".split(),
    "pt": "o a os as de que e em um uma é por com para não se seu ao como mais mas sua já este "
          "isso você vocês está estou tenho quero preciso pode obrigado olá também muito sem sobre "
          "foi são do da dos das no na nos nas um uma criar cria faz".split(),
    "de": "der die das und ist nicht ein eine zu den dem mit für auf ich du er sie es wir ihr "
          "von sich auch als an nach wie aber bei oder wenn noch nur kann bitte danke hallo "
          "möchte brauche habe bin sind wird mir mich dir erstelle mach machen".split(),
}
_CHARS = {
    "es": "ñ¿¡áíóú", "fr": "çœèêàùâîôû", "pt": "ãõçâêôà", "de": "äöüß",
}
_WORD_SETS = {k: set(v) for k, v in _WORDS.items()}
_TOKEN = re.compile(r"[a-zA-ZÀ-ÿñÑ']+")


def _scores(text: str) -> tuple[dict[str, float], dict[str, int]]:
    low = str(text or "").lower()
    toks = _TOKEN.findall(low)
    out = {k: 0.0 for k in SUPPORTED}
    hits = {k: 0 for k in SUPPORTED}
    if not toks:
        return out, hits
    for t in toks:
        for k, ws in _WORD_SETS.items():
            if t in ws:
                out[k] += 1.0
                hits[k] += 1
    for k, chars in _CHARS.items():
        out[k] += 1.5 * sum(low.count(c) for c in chars)
    return out, hits


def detect(text: str) -> str:
    """Best language for ``text`` or "" when the evidence is thin or ambiguous."""
    s, hits = _scores(text)
    ranked = sorted(s.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    if top < 2 or top - second < 1 or hits.get(best, 0) < 2:
        return ""
    return best


def norm(lang: Optional[str], default: str = "en") -> str:
    """'es-VE' -> 'es'; anything unsupported -> ``default``."""
    code = str(lang or "").strip().lower()[:2]
    return code if code in SUPPORTED else default


def resolve(text: str, sticky: str = "", configured: str = "auto", default: str = "en") -> str:
    """Language for this turn.

    ``configured`` other than "auto" is a deliberate override. Otherwise: what the message says,
    else the language already used in this session, else ``default``."""
    cfg = str(configured or "auto").strip().lower()
    if cfg and cfg != "auto" and cfg[:2] in SUPPORTED:
        return cfg[:2]
    found = detect(text)
    if found:
        return found
    return norm(sticky, default)
