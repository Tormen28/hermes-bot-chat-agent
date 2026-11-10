"""Generic panteón: neutral presets, opt-in profile creation, team wiring, no personal data."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bot_chat_agent import Base, bca  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
import panteon as pt  # noqa: E402

# Strings that must never appear in the PUBLIC plugin's panteón code or presets: the maintainer's
# own god names, projects, vault and path fragments. (Mythological names that are NOT in this list
# are the shipped neutral presets.)
FORBIDDEN = ("hephaestus", "hefesto", "harmonia", "dike", "apollo", "peitho", "rafael", "zeus",
             "poseidon", "hades", "padre", "lunacia", "wordch4t", "segundo-cerebro", "segundo cerebro",
             "vault", "egtyp", "gustavo", "tormen", "c:\\users", "/home/", "api_key", "token=")


class TestPresets(unittest.TestCase):
    def test_presets_are_neutral_and_complete(self):
        self.assertGreaterEqual(len(pt.PRESETS), 6)
        for pid, p in pt.PRESETS.items():
            for k in ("domain_en", "domain_es", "role_en", "role_es", "focus_en", "focus_es"):
                self.assertTrue(p.get(k), f"{pid}.{k}")
            self.assertRegex(pid, r"^[a-z][a-z0-9_-]*$")
        # at least the domains the product promises
        text = " ".join(p["domain_en"] for p in pt.PRESETS.values()).lower()
        for needle in ("code", "design", "research", "games", "writing"):
            self.assertIn(needle, text)

    def test_no_personal_data_anywhere_in_the_feature(self):
        blob = (ROOT / "panteon.py").read_text(encoding="utf-8").lower()
        blob += json.dumps(pt.PRESETS).lower()
        for pid in pt.PRESETS:
            blob += pt.soul_for(pid, "es").lower() + pt.soul_for(pid, "en").lower()
        for bad in FORBIDDEN:
            self.assertNotIn(bad, blob, f"personal/forbidden string in panteón: {bad}")

    def test_soul_has_role_and_delivery_rules_in_both_languages(self):
        for lang in ("en", "es"):
            s = pt.soul_for("daedalus", lang)
            self.assertIn("Daedalus", s)
            self.assertIn("kanban", s)
        self.assertNotEqual(pt.soul_for("iris", "en"), pt.soul_for("iris", "es"))

    def test_roster_line_only_for_preset_ids(self):
        self.assertEqual(pt.roster_line(["my-custom-bot"], "en"), "")
        line = pt.roster_line(["daedalus", "my-custom-bot", "IRIS"], "en")
        self.assertIn("daedalus: code", line)
        self.assertIn("iris:", line)
        self.assertNotIn("my-custom-bot", line)
        self.assertLessEqual(len(line), 420)
        self.assertIn("Equipo", pt.roster_line(["athena"], "es"))

    def test_parse_ids(self):
        ids, unk = pt.parse_ids(["all"])
        self.assertEqual(tuple(ids), pt.PRESET_ORDER)
        ids, unk = pt.parse_ids(["Daedalus", "@iris", "daedalus", "zzz"])
        self.assertEqual(ids, ["daedalus", "iris"])
        self.assertEqual(unk, ["zzz"])

    def test_profile_name_validation_and_prefix(self):
        self.assertEqual(pt.profile_name("iris", "my-"), "my-iris")
        with self.assertRaises(ValueError):
            pt.profile_name("iris", "Bad Prefix ")


class FakeCore:
    def __init__(self, existing=()):
        self.existing = set(existing)
        self.created = []
        self.fail = set()

    def exists(self, name):
        return name in self.existing

    def creator(self, name, *, soul, description):
        if name in self.fail:
            raise RuntimeError("boom")
        self.created.append((name, soul, description))
        self.existing.add(name)
        return {"ok": True}


class TestHandler(unittest.TestCase):
    def setUp(self):
        self.core = FakeCore()
        self.team = []
        self.saved = []

        def set_team(names):
            self.saved.append(list(names))
            return True
        self.set_team = set_team

    def run_cmd(self, raw, lang="en", **kw):
        return pt.handle(raw, lang=lang, current_team=self.team, exists=self.core.exists,
                         creator=self.core.creator, set_team=self.set_team, **kw)

    def test_list_is_read_only_and_marks_state(self):
        self.core.existing.add("iris")
        self.team = ["iris"]
        out = self.run_cmd("")
        self.assertIn("daedalus", out)
        self.assertIn("iris · design and UI/UX · profile exists · in team", out)
        self.assertEqual(self.core.created, [])
        self.assertEqual(self.saved, [])

    def test_nothing_is_created_without_confirm(self):
        for raw in ("plan daedalus iris", "create daedalus iris"):
            out = self.run_cmd(raw)
            self.assertIn("hermes profile create daedalus --clone --no-alias", out)
            self.assertIn("assignees: [daedalus, iris]", out)
        self.assertIn("confirm", self.run_cmd("create daedalus"))
        self.assertEqual(self.core.created, [])

    def test_create_with_confirm_creates_only_missing_and_never_touches_existing(self):
        self.core.existing.add("iris")
        out = self.run_cmd("create daedalus iris confirm")
        self.assertEqual([c[0] for c in self.core.created], ["daedalus"])
        self.assertIn("Created: daedalus", out)
        self.assertIn("Already existed (left untouched): iris", out)
        name, soul, desc = self.core.created[0]
        self.assertIn("Daedalus", soul)
        self.assertIn("code and engineering", desc)
        self.assertEqual(self.saved, [], "create must not change the team by itself")

    def test_create_reports_failures_and_keeps_going(self):
        self.core.fail.add("daedalus")
        out = self.run_cmd("create daedalus iris confirm")
        self.assertIn("Failed: daedalus (boom)", out)
        self.assertEqual([c[0] for c in self.core.created], ["iris"])

    def test_create_all_and_spanish(self):
        out = self.run_cmd("crear all confirm", lang="es")
        self.assertEqual(len(self.core.created), len(pt.PRESETS))
        self.assertIn("Creados:", out)
        self.assertIn("panel Bots", out)

    def test_unknown_preset_and_missing_ids(self):
        self.assertIn("Unknown preset", self.run_cmd("create zeus confirm"))
        self.assertIn("at least one", self.run_cmd("create confirm"))
        self.assertEqual(self.core.created, [])

    def test_team_requires_confirm_and_existing_profiles(self):
        self.assertIn("do not exist yet", self.run_cmd("team daedalus confirm"))
        self.core.existing.update({"daedalus", "iris"})
        out = self.run_cmd("team daedalus iris")
        self.assertIn("confirm", out)
        self.assertEqual(self.saved, [])
        out = self.run_cmd("team daedalus iris confirm")
        self.assertEqual(self.saved, [["daedalus", "iris"]])
        self.assertIn("daedalus, iris", out)

    def test_team_off(self):
        self.team = ["daedalus"]
        out = self.run_cmd("team off confirm")
        self.assertEqual(self.saved, [[]])
        self.assertIn("single-operator", out)

    def test_team_falls_back_to_snippet_when_core_cannot_write_settings(self):
        self.core.existing.add("nike")
        out = pt.handle("team nike confirm", exists=self.core.exists, creator=self.core.creator,
                        set_team=None)
        self.assertIn("assignees: [nike]", out)

    def test_core_without_profile_api_prints_cli_lines(self):
        out = pt.handle("create athena confirm", exists=self.core.exists, creator=self.core.creator,
                        core_ok=False)
        self.assertIn("hermes profile create athena --clone --no-alias", out)
        self.assertEqual(self.core.created, [])

    def test_prefix_is_applied_everywhere(self):
        out = self.run_cmd("create iris confirm", prefix="my-")
        self.assertEqual(self.core.created[0][0], "my-iris")
        self.assertIn("my-iris", out)

    def test_never_raises(self):
        def boom(*a, **k):
            raise RuntimeError("x")
        out = pt.handle("create iris confirm", exists=boom, creator=boom)
        self.assertTrue(out)
        self.assertNotIn("Traceback", out)


class TestPluginWiring(Base):
    def test_commands_registered_both_languages(self):
        for name in ("panteon", "pantheon"):
            self.assertIn(name, self.ctx.commands)
            out = self.ctx.commands[name](raw_args="", session_id="bot")
            self.assertIn("daedalus", out)
        self.assertIn("Panteón", self.ctx.commands["panteon"](raw_args=""))
        self.assertIn("opt-in", self.ctx.commands["pantheon"](raw_args=""))

    def test_command_never_creates_by_itself(self):
        out = self.ctx.commands["pantheon"](raw_args="create daedalus")
        self.assertIn("confirm", out)

    def test_team_command_writes_through_set_config_only_with_confirm(self):
        writes = []
        self.ctx.set_config = lambda k, v: writes.append((k, list(v)))
        orig = pt.core_exists
        pt.core_exists = lambda n: True
        try:
            # handler captured `exists` default at def time, so go through the module default
            import importlib
            fn = self.ctx.commands["pantheon"]
            out = fn(raw_args="team daedalus")
            self.assertEqual(writes, [])
        finally:
            pt.core_exists = orig
        self.assertTrue(out)

    def test_roster_reaches_the_turn_only_for_preset_assignees(self):
        db = self.db
        for assignees, expect in ((["daedalus", "iris"], True), (["my-custom-bot"], False)):
            ctx = type(self.ctx)({"team": {"assignees": assignees}})
            bca.register(ctx)
            bca._state_db = lambda: db
            bca._cache.clear()
            out = ctx.hooks["pre_llm_call"](session_id="bot", turn_id="t-" + assignees[0])["context"]
            self.assertEqual("daedalus: code and engineering" in out, expect, out)

    def test_roster_can_be_turned_off(self):
        db = self.db
        ctx = type(self.ctx)({"team": {"assignees": ["daedalus"]},
                              "panteon": {"roster_in_context": False}})
        bca.register(ctx)
        bca._state_db = lambda: db
        bca._cache.clear()
        out = ctx.hooks["pre_llm_call"](session_id="bot", turn_id="t-off")["context"]
        self.assertNotIn("daedalus: code", out)


REAL_PY = os.environ.get("HERMES_FASE3B_PYTHON", "")


@unittest.skipUnless(REAL_PY and Path(REAL_PY).exists(), "needs a Hermes core Python (HERMES_FASE3B_PYTHON)")
class TestAgainstRealCoreProfiles(unittest.TestCase):
    """Creates real profiles through the core's own API in a throw-away HERMES_HOME."""

    def test_create_profiles_end_to_end(self):
        """The Desktop half creates profiles through the gateway's profiles.create. Here a stand-in creator
        uses the core's own profile API, to prove the plugin's FS-based exists/list checks agree with the
        real on-disk layout and that existing profiles are never touched."""
        home = tempfile.mkdtemp(prefix="bca-panteon-real-")
        core = os.environ.get("HERMES_AGENT_CHECKOUT", "")
        code = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(ROOT)!r}); sys.path.insert(0, {core!r})\n"
            "import panteon as pt\n"
            "from hermes_cli import profiles as P\n"
            "def creator(name, *, soul='', description='', **kw):\n"
            "    P.create_profile(name, clone_from='default', clone_config=False, no_alias=True)\n"
            "    d = P.get_profile_dir(name); (d / 'SOUL.md').write_text(soul, encoding='utf-8')\n"
            "    return {'ok': True}\n"
            "r1 = pt.handle('create daedalus iris confirm', lang='en', current_team=[], creator=creator)\n"
            "r2 = pt.handle('create daedalus confirm', lang='en', current_team=[], creator=creator)\n"
            "d = P.get_profile_dir('daedalus')\n"
            "print(json.dumps({'r1': r1, 'r2': r2, 'names': P.list_profile_names(), 'mine': pt.core_list_names(),\n"
            "  'exists': pt.core_exists('daedalus'), 'soul': (d / 'SOUL.md').read_text(encoding='utf-8')[:80]}))\n")
        env = {**os.environ, "HERMES_HOME": home}
        proc = subprocess.run([REAL_PY, "-c", code], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertIn("Created: daedalus, iris", out["r1"])
        self.assertIn("Already existed (left untouched): daedalus", out["r2"])
        self.assertIn("daedalus", out["names"]); self.assertIn("iris", out["names"])
        self.assertIn("daedalus", out["mine"]); self.assertTrue(out["exists"])
        self.assertTrue(out["soul"].startswith("# Daedalus"), out["soul"])

    def test_slash_command_through_the_real_plugin_manager_without_desktop_half(self):
        """Plugin loaded by the core's PluginManager. With the Desktop half off, /pantheon create never
        creates anything: it prints the terminal lines, and the team is untouched."""
        core = os.environ.get("HERMES_AGENT_CHECKOUT", "")
        proc = subprocess.run([REAL_PY, str(ROOT / "tests" / "_real_core_load.py"), core, "--panteon"],
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        line = [x for x in proc.stdout.splitlines() if x.startswith("{")][-1]
        out = json.loads(line)
        self.assertTrue(out["loaded"] and not out["error"])
        self.assertIn("Would create", out["panteon"]["create_no_confirm"])
        self.assertIn("do not exist yet", out["panteon"]["team_missing"])
        self.assertIn("Desktop half", out["panteon"]["create"])
        self.assertIn("hermes profile create", out["panteon"]["create"])
        self.assertEqual(out["profiles_dirs"], [])


if __name__ == "__main__":
    unittest.main()
