"""Generic "panteón": a coordinator plus specialised sub-bots, opt-in.

Everybody who installs the plugin gets the same neutral presets (mythological names, role
prompts). Nothing here knows about any person's projects, paths, keys or private notes.

* The **coordinator** is the profile the user is already talking to (the main profile, usually
  called Hermes). It needs no new profile.
* The **gods** are ordinary Hermes profiles. Each preset maps to a real profile name, so it
  shows up as a row of the stock Bots panel. Work reaches them through the existing
  ``team.assignees`` + kanban flow of this plugin (and the core's ``message_agent`` tool).
* Nothing is created automatically. ``/panteon create <ids> confirm`` is the only call that
  writes profiles, and ``/panteon team <ids> confirm`` the only one that writes config.

The module is pure (no hook state); ``create_profiles`` takes injectable callables so tests do
not need a Hermes core. The default creator queues the profile for the Desktop half, which calls the stock
``profiles.create`` RPC (no private core import); without it the command prints the equivalent
CLI lines instead.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

# Neutral names on purpose: no personal project names, and nothing that collides with the
# profile names of any specific user's own setup.
_COMMON_EN = (
    "You are a specialised teammate of the coordinator bot. Work only on the cards or messages "
    "assigned to you. Deliver by completing the kanban card with a short, verifiable summary "
    "(what changed, where, how to check it). If you are blocked, block the card with the reason. "
    "Never read or write secrets, delete data, force-push, publish or message third parties on "
    "your own: ask the coordinator, who asks the user."
)
_COMMON_ES = (
    "Eres un compañero especializado del bot coordinador. Trabaja solo en las tarjetas o "
    "mensajes que te asignen. Entrega completando la tarjeta de kanban con un resumen corto y "
    "verificable (qué cambió, dónde, cómo comprobarlo). Si te bloqueas, bloquea la tarjeta con "
    "el motivo. Nunca leas ni escribas secretos, borres datos, hagas push forzado, publiques o "
    "escribas a terceros por tu cuenta: pídelo al coordinador, que se lo pide al usuario."
)

PRESETS: dict[str, dict[str, str]] = {
    "daedalus": {
        "domain_en": "code and engineering", "domain_es": "código e ingeniería",
        "role_en": "Writes, reads and debugs code; small reviewed changes with tests.",
        "role_es": "Escribe, lee y depura código; cambios pequeños, revisados y con pruebas.",
        "focus_en": "Prefer the smallest change that works. Run the tests before you report. "
                    "Say exactly which files you touched.",
        "focus_es": "Prefiere el cambio más pequeño que funcione. Corre las pruebas antes de "
                    "reportar. Di exactamente qué archivos tocaste.",
    },
    "iris": {
        "domain_en": "design and UI/UX", "domain_es": "diseño y UI/UX",
        "role_en": "Layouts, palettes, flows and visual polish; reviews screens for clarity.",
        "role_es": "Maquetas, paletas, flujos y pulido visual; revisa pantallas por claridad.",
        "focus_en": "Think in hierarchy, contrast and spacing. Offer one clear option, plus at "
                    "most one alternative.",
        "focus_es": "Piensa en jerarquía, contraste y espaciado. Ofrece una opción clara y, a "
                    "lo sumo, una alternativa.",
    },
    "athena": {
        "domain_en": "research and analysis", "domain_es": "investigación y análisis",
        "role_en": "Finds sources, compares options, checks claims and states what is uncertain.",
        "role_es": "Busca fuentes, compara opciones, verifica afirmaciones y dice qué es incierto.",
        "focus_en": "Cite where each fact comes from. Separate what is verified from what is a "
                    "guess. End with a recommendation.",
        "focus_es": "Cita de dónde sale cada dato. Separa lo verificado de lo supuesto. Termina "
                    "con una recomendación.",
    },
    "calliope": {
        "domain_en": "writing and copy", "domain_es": "redacción y textos",
        "role_en": "Drafts, edits and tightens text in the user's voice and language.",
        "role_es": "Redacta, edita y afina textos con la voz y el idioma del usuario.",
        "focus_en": "Short sentences, no filler. Deliver a ready-to-use draft, not an outline.",
        "focus_es": "Frases cortas, sin relleno. Entrega un borrador listo para usar, no un "
                    "esquema.",
    },
    "dionysus": {
        "domain_en": "games and interactive prototypes",
        "domain_es": "juegos y prototipos interactivos",
        "role_en": "Game loops, mechanics, balance and quick playable prototypes.",
        "role_es": "Bucles de juego, mecánicas, balance y prototipos jugables rápidos.",
        "focus_en": "Make it playable first, polished later. Say how to run it and what to try.",
        "focus_es": "Primero que se pueda jugar, después el pulido. Di cómo ejecutarlo y qué "
                    "probar.",
    },
    "nike": {
        "domain_en": "QA, review and verification", "domain_es": "QA, revisión y verificación",
        "role_en": "Tests other people's work, reproduces bugs, reviews diffs and checks claims.",
        "role_es": "Prueba el trabajo de otros, reproduce errores, revisa cambios y verifica.",
        "focus_en": "Try to break it. Report what you ran and what you saw, never just 'looks "
                    "fine'.",
        "focus_es": "Intenta romperlo. Reporta lo que corriste y lo que viste, nunca solo "
                    "'se ve bien'.",
    },
    "mnemosyne": {
        "domain_en": "documentation and knowledge notes",
        "domain_es": "documentación y notas de conocimiento",
        "role_en": "Keeps READMEs, decision logs and handoff notes accurate and findable.",
        "role_es": "Mantiene READMEs, registros de decisiones y notas de traspaso al día.",
        "focus_en": "One page beats ten. Record decisions with their reason and date.",
        "focus_es": "Una página vale más que diez. Registra decisiones con su motivo y fecha.",
    },
}

PRESET_ORDER = tuple(PRESETS)

# The card / command text is shown to the user in their language; role prompts (SOUL.md) stay en / es.
_DOMAIN_X = {
    "daedalus": ('code et ingénierie', 'código e engenharia', 'Code und Entwicklung'),
    "iris": ('design et UI/UX', 'design e UI/UX', 'Design und UI/UX'),
    "athena": ('recherche et analyse', 'pesquisa e análise', 'Recherche und Analyse'),
    "calliope": ('rédaction et textes', 'redação e textos', 'Schreiben und Texte'),
    "dionysus": ('jeux et prototypes interactifs', 'jogos e protótipos interativos', 'Spiele und interaktive Prototypen'),
    "nike": ('QA, revue et vérification', 'QA, revisão e verificação', 'QA, Review und Verifikation'),
    "mnemosyne": ('documentation et notes de connaissance', 'documentação e notas de conhecimento', 'Dokumentation und Wissensnotizen'),
}


def domain_for(preset_id: str, lang: str = "en") -> str:
    code = str(lang or "").lower()[:2]
    if code in ("fr", "pt", "de"):
        return _DOMAIN_X[preset_id][("fr", "pt", "de").index(code)]
    return PRESETS[preset_id]["domain_" + _l(code)]
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _l(lang: str) -> str:
    return "es" if str(lang or "").lower().startswith("es") else "en"


def _ul(lang: str) -> str:
    """The language of text the user reads (en / es / fr / pt / de)."""
    code = str(lang or "").lower()[:2]
    return code if code in ("en", "es", "fr", "pt", "de") else "en"


def soul_for(preset_id: str, lang: str = "en") -> str:
    """SOUL.md text for one preset (role + shared delivery rules). Pure, no personal data."""
    p = PRESETS[preset_id]
    lg = _l(lang)
    common = _COMMON_ES if lg == "es" else _COMMON_EN
    head = (f"# {preset_id.capitalize()} — {p['domain_' + lg]}\n\n"
            f"{p['role_' + lg]}\n\n{p['focus_' + lg]}\n\n{common}\n")
    return head


def description_for(preset_id: str, lang: str = "en") -> str:
    return f"{PRESETS[preset_id]['domain_' + _l(lang)]} (panteón preset)"


def roster_line(assignees: Iterable[str], lang: str = "en", max_chars: int = 420) -> str:
    """One compact line telling the coordinator who does what; only for assignees that are
    preset ids. Empty string if none match (custom profile names get no invented role)."""
    lg = _l(lang)
    parts = []
    for name in assignees or []:
        key = str(name).strip().lower()
        if key in PRESETS:
            parts.append(f"{key}: {PRESETS[key]['domain_' + lg]}")
    if not parts:
        return ""
    head = "Equipo (delega por kanban a quien corresponda): " if lg == "es" else \
        "Team (delegate through kanban to whoever fits): "
    out = head + "; ".join(parts)
    return out if len(out) <= max_chars else out[: max_chars - 1].rstrip() + "…"


def parse_ids(tokens: Iterable[str], prefix: str = "") -> tuple[list[str], list[str]]:
    """Resolve user tokens to preset ids. ``all`` expands to every preset.
    Returns (ids, unknown). Order preserved, no duplicates."""
    ids: list[str] = []
    unknown: list[str] = []
    for tok in tokens:
        t = str(tok).strip().lower().lstrip("@")
        if not t:
            continue
        if t == "all":
            for pid in PRESET_ORDER:
                if pid not in ids:
                    ids.append(pid)
        elif t in PRESETS:
            if t not in ids:
                ids.append(t)
        else:
            unknown.append(t)
    return ids, unknown


def profile_name(preset_id: str, prefix: str = "") -> str:
    name = f"{prefix}{preset_id}".lower()
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid profile name: {name!r}")
    return name


def cli_lines(ids: Iterable[str], prefix: str = "") -> list[str]:
    """Equivalent commands for users who prefer the terminal (or when the core API is missing)."""
    return [f"hermes profile create {profile_name(i, prefix)} --clone --no-alias" for i in ids]


def config_snippet(names: Iterable[str]) -> str:
    items = ", ".join(names)
    return ("plugins:\n  entries:\n    bot-chat-agent:\n      settings:\n"
            f"        team:\n          assignees: [{items}]")


# ── default callables (public surfaces only) ────────────────────────────────────────────────
#
# Profiles are read from the profiles folder (``hermes_constants.get_default_hermes_root``, a
# documented helper) and created through the Desktop half, which calls the stock ``profiles.create``
# gateway RPC. The plugin imports no private core module (catalog rule 9).

def _profiles_dir() -> Path:
    from hermes_constants import get_default_hermes_root  # public plugin API
    return Path(get_default_hermes_root()) / "profiles"


def core_exists(name: str) -> bool:
    try:
        return (_profiles_dir() / name).is_dir()
    except Exception:
        return False


def core_list_names() -> Optional[list[str]]:
    try:
        d = _profiles_dir()
        return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []
    except Exception:
        return None


def core_create(name: str, *, soul: str, description: str, preset: str = "", lang: str = "en",
                clone_from: str = "default", wait_seconds: float = 12.0) -> dict[str, Any]:
    """Queue ONE profile for the Desktop half and wait a bounded time for its answer. Only called
    after the user said yes (auto offer) or typed ``confirm`` (slash command). Raises when the
    Desktop half is not there or does not answer: the command then prints the terminal lines."""
    import panteon_auto as auto
    if not auto.desktop_ready():
        raise RuntimeError("the Desktop half is not active (enable it in Capabilities → Plugins)")
    req = auto.enqueue_request(preset or name, name, lang=lang, clone_from=clone_from)
    result = auto.wait_for_result(req["id"], wait_seconds)
    if result == "created":
        auto.take_finished()
        return {"ok": True}
    if result == "failed":
        err = next((r.get("error") for r in auto.load_state().get("requests", []) if r.get("id") == req["id"]), "")
        auto.take_finished()
        raise RuntimeError(err or "profiles.create failed")
    raise RuntimeError("no answer from the Desktop half in time (it may still finish; check the Bots panel)")


def create_profiles(ids: Iterable[str], *, lang: str = "en", prefix: str = "",
                    exists: Callable[[str], bool] = core_exists,
                    creator: Callable[..., dict[str, Any]] = core_create) -> list[dict[str, Any]]:
    """Create the profiles that do not exist yet. Existing ones are never touched."""
    out: list[dict[str, Any]] = []
    for pid in ids:
        try:
            name = profile_name(pid, prefix)
        except ValueError as exc:
            out.append({"id": pid, "name": pid, "status": "invalid", "detail": str(exc)})
            continue
        try:
            if exists(name):
                out.append({"id": pid, "name": name, "status": "exists"})
                continue
            kwargs = {"soul": soul_for(pid, lang), "description": description_for(pid, lang),
                      "preset": pid, "lang": lang}
            try:
                params = inspect.signature(creator).parameters
                if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
                    kwargs = {k: v for k, v in kwargs.items() if k in params}
            except (TypeError, ValueError):
                kwargs = {k: kwargs[k] for k in ("soul", "description")}
            res = creator(name, **kwargs)
            out.append({"id": pid, "name": name, "status": "created", "detail": res})
        except Exception as exc:
            out.append({"id": pid, "name": name, "status": "failed", "detail": str(exc)})
    return out


# ── the /panteon command ─────────────────────────────────────────────────────────────────────

_T = {
    "en": {
        "title": "Panteón — coordinator + specialised bots (opt-in)",
        "coord": "Coordinator: the profile you chat with in Bot Chat (no new profile needed).",
        "none": "none yet",
        "cols": "preset · domain · profile · team",
        "yes": "in team", "no": "not in team", "ex": "profile exists", "nx": "no profile yet",
        "help": ("Commands:\n  /panteon                      this list\n"
                 "  /panteon show <id>            role prompt of a preset\n"
                 "  /panteon plan <ids|all>       dry run: what would be created\n"
                 "  /panteon create <ids|all> confirm   create the profiles (never automatic)\n"
                 "  /panteon team <ids|all> confirm     add them to team.assignees\n"
                 "  /panteon team off confirm           empty the team (single-operator)\n"
                 "Nothing changes without the word confirm."),
        "unknown": "Unknown preset(s): {x}. Known: {k}.",
        "need_ids": "Name at least one preset id (or `all`).",
        "need_confirm": "Dry run only. Add the word `confirm` to apply.",
        "plan_h": "Would create (if missing) cloned from your main profile's config:",
        "cli_h": "Equivalent terminal lines:",
        "cfg_h": "Team config to apply with `/panteon team … confirm` (or paste):",
        "created": "Created", "exists": "Already existed (left untouched)",
        "failed": "Failed", "invalid": "Invalid name",
        "after": ("Profiles appear as rows in the Bots panel within about 30 s. Open each once to "
                  "start its Bot Chat. Then run `/panteon team <ids> confirm` so work is "
                  "delegated to them."),
        "team_ok": "team.assignees is now: {n}.", "team_off": "team.assignees emptied: single-operator again.",
        "team_nocfg": ("This Hermes cannot write plugin settings from a command. Paste this "
                       "into config.yaml:"),
        "team_missing": "These profiles do not exist yet: {m}. Run /panteon create first.",
        "core_missing": "The Desktop half of this plugin is not active (enable it in Capabilities → Plugins), so profiles cannot be created from here. Run the lines below in a terminal:",
    },
    "es": {
        "title": "Panteón — coordinador + bots especializados (opcional)",
        "coord": "Coordinador: el perfil con el que hablas ahora (no hace falta perfil nuevo).",
        "none": "ninguno todavía",
        "cols": "preset · dominio · perfil · equipo",
        "yes": "en el equipo", "no": "fuera del equipo", "ex": "el perfil existe", "nx": "sin perfil aún",
        "help": ("Comandos:\n  /panteon                      esta lista\n"
                 "  /panteon show <id>            prompt de rol de un preset\n"
                 "  /panteon plan <ids|all>       ensayo: qué se crearía\n"
                 "  /panteon create <ids|all> confirm   crea los perfiles (nunca automático)\n"
                 "  /panteon team <ids|all> confirm     los añade a team.assignees\n"
                 "  /panteon team off confirm           vacía el equipo (modo solo)\n"
                 "Nada cambia sin la palabra confirm."),
        "unknown": "Preset(s) desconocido(s): {x}. Disponibles: {k}.",
        "need_ids": "Indica al menos un id de preset (o `all`).",
        "need_confirm": "Solo ensayo. Añade la palabra `confirm` para aplicar.",
        "plan_h": "Se crearían (si faltan), clonando la config de tu perfil principal:",
        "cli_h": "Líneas equivalentes en terminal:",
        "cfg_h": "Config de equipo para aplicar con `/panteon team … confirm` (o pegar):",
        "created": "Creados", "exists": "Ya existían (no se tocaron)",
        "failed": "Fallaron", "invalid": "Nombre inválido",
        "after": ("Los perfiles salen como filas del panel Bots en unos 30 s. Abre cada uno una "
                  "vez para iniciar su Bot Chat. Luego `/panteon team <ids> confirm` para que se "
                  "les delegue trabajo."),
        "team_ok": "team.assignees ahora es: {n}.", "team_off": "team.assignees vaciado: vuelve el modo solo.",
        "team_nocfg": ("Este Hermes no deja escribir ajustes del plugin desde un comando. Pega "
                       "esto en config.yaml:"),
        "team_missing": "Estos perfiles aún no existen: {m}. Corre /panteon create primero.",
        "core_missing": "La mitad de escritorio de este plugin no está activa (actívala en Capabilities → Plugins), así que no se pueden crear perfiles desde aquí. Corre estas líneas en una terminal:",
    },
    "fr": {
        "title": "Panthéon — coordinateur + bots spécialisés (optionnel)",
        "coord": "Coordinateur : le profil avec lequel tu parles dans Bot Chat (pas besoin de nouveau profil).",
        "none": "aucun pour l'instant",
        "cols": "preset · domaine · profil · équipe",
        "yes": "dans l'équipe", "no": "hors de l'équipe", "ex": "le profil existe", "nx": "pas encore de profil",
        "help": ("Commandes :\n  /panteon                      cette liste\n"
                 "  /panteon show <id>            prompt de rôle d'un preset\n"
                 "  /panteon plan <ids|all>       simulation : ce qui serait créé\n"
                 "  /panteon create <ids|all> confirm   crée les profils (jamais automatique)\n"
                 "  /panteon team <ids|all> confirm     les ajoute à team.assignees\n"
                 "  /panteon team off confirm           vide l'équipe (mode solo)\n"
                 "Rien ne change sans le mot confirm."),
        "unknown": "Preset(s) inconnu(s) : {x}. Disponibles : {k}.",
        "need_ids": "Indique au moins un id de preset (ou `all`).",
        "need_confirm": "Simulation seulement. Ajoute le mot `confirm` pour appliquer.",
        "plan_h": "Serait créé (s'il manque), cloné depuis la config de ton profil principal :",
        "cli_h": "Lignes équivalentes dans un terminal :",
        "cfg_h": "Config d'équipe à appliquer avec `/panteon team … confirm` (ou à coller) :",
        "created": "Créés", "exists": "Existaient déjà (non modifiés)",
        "failed": "Échecs", "invalid": "Nom invalide",
        "after": ("Les profils apparaissent comme des lignes du panneau Bots en environ 30 s. Ouvre-les une "
                  "fois pour démarrer leur Bot Chat. Puis `/panteon team <ids> confirm` pour leur déléguer du travail."),
        "team_ok": "team.assignees est maintenant : {n}.", "team_off": "team.assignees vidé : retour au mode solo.",
        "team_nocfg": "Ce Hermes ne permet pas d'écrire les réglages du plugin depuis une commande. Colle ceci dans config.yaml :",
        "team_missing": "Ces profils n'existent pas encore : {m}. Lance d'abord /panteon create.",
        "core_missing": "La moitié Desktop de ce plugin n'est pas active (active-la dans Capabilities → Plugins), donc les profils ne peuvent pas être créés d'ici. Lance les lignes ci-dessous dans un terminal :",
    },
    "pt": {
        "title": "Panteão — coordenador + bots especializados (opcional)",
        "coord": "Coordenador: o perfil com o qual você conversa no Bot Chat (não precisa de perfil novo).",
        "none": "nenhum ainda",
        "cols": "preset · domínio · perfil · equipe",
        "yes": "na equipe", "no": "fora da equipe", "ex": "o perfil existe", "nx": "ainda sem perfil",
        "help": ("Comandos:\n  /panteon                      esta lista\n"
                 "  /panteon show <id>            prompt de função de um preset\n"
                 "  /panteon plan <ids|all>       ensaio: o que seria criado\n"
                 "  /panteon create <ids|all> confirm   cria os perfis (nunca automático)\n"
                 "  /panteon team <ids|all> confirm     adiciona a team.assignees\n"
                 "  /panteon team off confirm           esvazia a equipe (modo solo)\n"
                 "Nada muda sem a palavra confirm."),
        "unknown": "Preset(s) desconhecido(s): {x}. Disponíveis: {k}.",
        "need_ids": "Informe pelo menos um id de preset (ou `all`).",
        "need_confirm": "Somente ensaio. Adicione a palavra `confirm` para aplicar.",
        "plan_h": "Seriam criados (se faltarem), clonando a config do seu perfil principal:",
        "cli_h": "Linhas equivalentes no terminal:",
        "cfg_h": "Config de equipe para aplicar com `/panteon team … confirm` (ou colar):",
        "created": "Criados", "exists": "Já existiam (não foram alterados)",
        "failed": "Falharam", "invalid": "Nome inválido",
        "after": ("Os perfis aparecem como linhas do painel Bots em cerca de 30 s. Abra cada um uma vez "
                  "para iniciar o Bot Chat dele. Depois `/panteon team <ids> confirm` para delegar trabalho."),
        "team_ok": "team.assignees agora é: {n}.", "team_off": "team.assignees esvaziado: volta o modo solo.",
        "team_nocfg": "Este Hermes não permite escrever as configurações do plugin por comando. Cole isto no config.yaml:",
        "team_missing": "Estes perfis ainda não existem: {m}. Rode /panteon create primeiro.",
        "core_missing": "A metade Desktop deste plugin não está ativa (ative em Capabilities → Plugins), então não dá para criar perfis daqui. Rode as linhas abaixo em um terminal:",
    },
    "de": {
        "title": "Panteón — Koordinator + spezialisierte Bots (optional)",
        "coord": "Koordinator: das Profil, mit dem du im Bot Chat sprichst (kein neues Profil nötig).",
        "none": "noch keine",
        "cols": "Preset · Bereich · Profil · Team",
        "yes": "im Team", "no": "nicht im Team", "ex": "Profil existiert", "nx": "noch kein Profil",
        "help": ("Befehle:\n  /panteon                      diese Liste\n"
                 "  /panteon show <id>            Rollen-Prompt eines Presets\n"
                 "  /panteon plan <ids|all>       Probelauf: was erstellt würde\n"
                 "  /panteon create <ids|all> confirm   erstellt die Profile (nie automatisch)\n"
                 "  /panteon team <ids|all> confirm     fügt sie zu team.assignees hinzu\n"
                 "  /panteon team off confirm           leert das Team (Einzelbetrieb)\n"
                 "Ohne das Wort confirm ändert sich nichts."),
        "unknown": "Unbekannte(s) Preset(s): {x}. Bekannt: {k}.",
        "need_ids": "Nenne mindestens eine Preset-ID (oder `all`).",
        "need_confirm": "Nur Probelauf. Füge das Wort `confirm` hinzu, um anzuwenden.",
        "plan_h": "Würde erstellt (falls es fehlt), geklont aus der Config deines Hauptprofils:",
        "cli_h": "Entsprechende Terminal-Zeilen:",
        "cfg_h": "Team-Config zum Anwenden mit `/panteon team … confirm` (oder einfügen):",
        "created": "Erstellt", "exists": "Existierten bereits (unverändert)",
        "failed": "Fehlgeschlagen", "invalid": "Ungültiger Name",
        "after": ("Die Profile erscheinen in etwa 30 s als Zeilen im Bots-Panel. Öffne jedes einmal, um "
                  "seinen Bot Chat zu starten. Danach `/panteon team <ids> confirm`, damit ihnen Arbeit delegiert wird."),
        "team_ok": "team.assignees ist jetzt: {n}.", "team_off": "team.assignees geleert: wieder Einzelbetrieb.",
        "team_nocfg": "Dieses Hermes erlaubt keine Plugin-Einstellungen per Befehl. Füge das in config.yaml ein:",
        "team_missing": "Diese Profile existieren noch nicht: {m}. Führe zuerst /panteon create aus.",
        "core_missing": "Die Desktop-Hälfte dieses Plugins ist nicht aktiv (aktiviere sie unter Capabilities → Plugins), daher können von hier keine Profile erstellt werden. Führe die Zeilen unten in einem Terminal aus:",
    },
}


def _confirmed(tokens: list[str]) -> tuple[list[str], bool]:
    rest = [t for t in tokens if t.lower() not in ("confirm", "confirmar", "confirmer", "bestätigen", "--confirm")]
    return rest, len(rest) != len(tokens)


def handle(raw_args: str, *, lang: str = "en", prefix: str = "",
           current_team: Optional[list[str]] = None,
           exists: Callable[[str], bool] = core_exists,
           creator: Callable[..., dict[str, Any]] = core_create,
           set_team: Optional[Callable[[list[str]], bool]] = None,
           core_ok: bool = True) -> str:
    """Slash handler. Pure given its callables; never raises."""
    lg = _ul(lang)
    t = _T[lg]
    try:
        toks = (raw_args or "").replace(",", " ").split()
        team = [str(x).lower() for x in (current_team or [])]
        sub = toks[0].lower() if toks else "list"
        sub = {"ls": "list", "listar": "list", "ver": "show", "plan": "plan", "ensayo": "plan",
               "crear": "create", "equipo": "team", "ayuda": "help", "?": "help",
               "liste": "list", "lister": "list", "aide": "help", "créer": "create", "creer": "create",
               "équipe": "team", "equipe": "team", "criar": "create", "ajuda": "help",
               "erstellen": "create", "hilfe": "help"}.get(sub, sub)
        if sub in ("list", "help"):
            lines = [t["title"], t["coord"], t["cols"]]
            for pid in PRESET_ORDER:
                name = profile_name(pid, prefix)
                try:
                    has = exists(name)
                except Exception:
                    has = False
                lines.append(f"- {pid} · {domain_for(pid, lg)} · "
                             f"{t['ex'] if has else t['nx']} · {t['yes'] if name in team else t['no']}")
            lines.append("")
            lines.append(t["help"])
            return "\n".join(lines)
        if sub == "show":
            ids, unk = parse_ids(toks[1:2])
            if unk or not ids:
                return t["unknown"].format(x=", ".join(unk) or "?", k=", ".join(PRESET_ORDER))
            return soul_for(ids[0], lang)
        if sub in ("plan", "create"):
            rest, ok = _confirmed(toks[1:])
            ids, unk = parse_ids(rest)
            if unk:
                return t["unknown"].format(x=", ".join(unk), k=", ".join(PRESET_ORDER))
            if not ids:
                return t["need_ids"]
            names = [profile_name(i, prefix) for i in ids]
            if sub == "plan" or not ok:
                out = [t["plan_h"]] + [f"- {n} ({domain_for(i, lg)})" for i, n in zip(ids, names)]
                out += ["", t["cli_h"]] + cli_lines(ids, prefix)
                out += ["", t["cfg_h"], config_snippet(names)]
                if sub == "create":
                    out += ["", t["need_confirm"]]
                return "\n".join(out)
            if not core_ok:
                return "\n".join([t["core_missing"]] + cli_lines(ids, prefix))
            res = create_profiles(ids, lang=lang, prefix=prefix, exists=exists, creator=creator)
            groups: dict[str, list[str]] = {}
            for r in res:
                groups.setdefault(r["status"], []).append(
                    r["name"] + (f" ({r['detail']})" if r["status"] in ("failed", "invalid") else ""))
            out = []
            for key in ("created", "exists", "failed", "invalid"):
                if groups.get(key):
                    out.append(f"{t[key]}: {', '.join(groups[key])}")
            out.append(t["after"])
            return "\n".join(out)
        if sub == "team":
            rest, ok = _confirmed(toks[1:])
            if rest and rest[0].lower() in ("off", "none", "ninguno", "apagar", "aucun", "nenhum", "keins"):
                names: list[str] = []
            else:
                ids, unk = parse_ids(rest)
                if unk:
                    return t["unknown"].format(x=", ".join(unk), k=", ".join(PRESET_ORDER))
                if not ids:
                    return t["need_ids"]
                names = [profile_name(i, prefix) for i in ids]
                missing = [n for n in names if not exists(n)]
                if missing:
                    return t["team_missing"].format(m=", ".join(missing))
            if not ok:
                return "\n".join([t["need_confirm"], config_snippet(names)])
            if set_team is None or not set_team(names):
                return "\n".join([t["team_nocfg"], config_snippet(names)])
            return t["team_ok"].format(n=", ".join(names)) if names else t["team_off"]
        return t["help"]
    except Exception as exc:
        return {"es": f"/panteon falló ({exc}). No se cambió nada.",
                "fr": f"/panteon a échoué ({exc}). Rien n'a été modifié.",
                "pt": f"/panteon falhou ({exc}). Nada foi alterado.",
                "de": f"/panteon ist fehlgeschlagen ({exc}). Nichts wurde geändert."}.get(
                    _ul(lang), f"/panteon failed ({exc}). Nothing was changed.")
