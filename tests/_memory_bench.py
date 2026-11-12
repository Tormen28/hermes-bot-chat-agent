"""Reproducible long-session memory benchmark (synthetic, offline, no model).

Measures, against whatever plugin code is on disk:

* injected context size from ``pre_llm_call`` (chars and tokens)
* hook latency
* conversation_history size the core would still send (the plugin cannot
  replace it on Hermes v0.23 unless ``context.engine: bot-chat`` is active)
* optional bounded-view size when ``bot_chat_memory.bounded_view`` exists
  (recent messages + rolling summary + pending asks + long-term hits)

Token estimate: ``context_budget.estimate_tokens`` (tiktoken cl100k_base
when present, otherwise chars/4). Seed and turn lengths are fixed, so the
same code produces the same sizes.

Usage:
    python tests/_memory_bench.py --label before --out docs/memory-bench-before.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("bot_chat_agent", ROOT / "__init__.py")
bca = importlib.util.module_from_spec(spec)
sys.modules["bot_chat_agent"] = bca
spec.loader.exec_module(bca)

import context_budget  # noqa: E402
import sqlite3  # noqa: E402


SEED_TOPICS = (
    "the Bot Chat plugin and how context is injected each turn",
    "the video editor service on port 8770 and its cinematic flow",
    "the arena prototype and its current camera scale",
    "pending asks lost when a mid-turn steer arrives",
    "Hermes core v0.23 and the missing durable-delivery API",
    "the safety guard presets and the memory-injection rule",
    "the project card and incremental team note budgets",
)


def _home() -> Path:
    home = Path(tempfile.mkdtemp(prefix="memory-bench-"))
    with sqlite3.connect(home / "state.db") as c:
        c.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, "
            "parent_session_id TEXT, source TEXT)"
        )
        c.execute("INSERT INTO sessions VALUES ('bench','Bot Chat',NULL,'desktop')")
    os.environ["HERMES_HOME"] = str(home)
    return home


class FakeCtx:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.hooks = {}

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_context_engine(self, *a, **k):
        return None


def setup_plugin(home: Path, cfg: dict | None = None):
    bca._cache.clear()
    if hasattr(bca, "_questions"):
        bca._questions.clear()
    ctx = FakeCtx(cfg=cfg or {"language": "en", "team": {"assignees": []}})
    bca.register(ctx)
    bca._state_db = lambda: home / "state.db"
    return ctx


def tokens(text: str) -> int:
    return context_budget.estimate_tokens(text or "")


def history_text(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        role = str(m.get("role") or "")
        content = m.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                str(p.get("text") or "") if isinstance(p, dict) else str(p)
                for p in content
            )
        parts.append(f"{role}: {content}")
    return "\n".join(parts)


def make_turn(i: int) -> list[dict]:
    """One synthetic turn (user + optional tools + assistant), deterministic."""
    topic = SEED_TOPICS[i % len(SEED_TOPICS)]
    # ~80-120 words of padding so a long session has realistic bulk, not stubs.
    pad = (" details about " + topic) * 8
    user = (
        f"Turn {i}: please review {topic}. I need a short status and the next "
        f"step.{pad}"
    )
    msgs: list[dict] = [{"role": "user", "content": user}]
    if i % 7 == 3:
        msgs.append({
            "role": "assistant",
            "content": f"Checking {topic}.",
            "tool_calls": [{"id": f"c{i}", "function": {"name": "terminal"}}],
        })
        msgs.append({"role": "tool", "content": ("log line for " + topic + "\n") * 12})
        # Mid-turn steer, so pending_asks has something to scan on later turns.
        msgs.append({
            "role": "user",
            "content": (
                "[OUT-OF-BAND USER MESSAGE]\n"
                f"steer on turn {i}: also tell me about {SEED_TOPICS[(i + 1) % len(SEED_TOPICS)]}\n"
                "[/OUT-OF-BAND USER MESSAGE]"
            ),
        })
    assistant = (
        f"On it — turn {i}. Status of {topic}: still in progress, no blockers "
        f"verified beyond the plugin tests. Next step is to keep the budgets tight."
        + (" More notes on " + topic) * 10
    )
    if i % 11 == 0:
        assistant += (
            " Decision: we will store long-term notes in the plugin local store, "
            "not in an external vault."
        )
    if i % 13 == 0:
        assistant += " Lesson learned: a mid-turn steer must not drop the earlier ask."
    msgs.append({"role": "assistant", "content": assistant})
    return msgs


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    if len(xs) == 1:
        return float(xs[0])
    s = sorted(xs)
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def run_session(lengths: list[int], reps_last: int = 5) -> dict:
    """For each session length, grow a history and measure the last turn.

    ``reps_last`` repeats only the final pre_llm_call for a stable latency.
    """
    rows = []
    estimator = context_budget.estimator_name()
    for n in lengths:
        home = _home()
        ctx = setup_plugin(home)
        hook = ctx.hooks["pre_llm_call"]
        post = ctx.hooks.get("post_llm_call")
        history: list[dict] = []
        last = None
        for i in range(n):
            history.extend(make_turn(i))
            t0 = time.perf_counter()
            out = hook(session_id="bench", turn_id=f"t{i}",
                       conversation_history=history) or {}
            dt = (time.perf_counter() - t0) * 1000.0
            injected = str(out.get("context") or "")
            if post is not None:
                try:
                    post(session_id="bench", turn_id=f"t{i}",
                         conversation_history=history,
                         final_response=history[-1].get("content") or "")
                except Exception:
                    pass
            last = {
                "turns": n,
                "messages": len(history),
                "injected_chars": len(injected),
                "injected_tokens": tokens(injected),
                "history_chars": len(history_text(history)),
                "history_tokens": tokens(history_text(history)),
                "pre_llm_ms": dt,
            }
            # Bounded view, if the memory module is present (after only).
            try:
                import bot_chat_memory as mem  # type: ignore
                view = mem.bounded_view(history, session_id="bench",
                                        lang="en", store_path=home / "bot-chat-agent" / "memory.db")
                last["bounded_view_chars"] = len(view)
                last["bounded_view_tokens"] = tokens(view)
            except Exception:
                last["bounded_view_chars"] = None
                last["bounded_view_tokens"] = None
        # Stable latency on the final turn (history already at length n).
        samples = []
        for _ in range(reps_last):
            t0 = time.perf_counter()
            hook(session_id="bench", turn_id=f"t{n}-lat",
                 conversation_history=history)
            samples.append((time.perf_counter() - t0) * 1000.0)
        last["pre_llm_ms_p50"] = round(percentile(samples, 50), 3)
        last["pre_llm_ms_p95"] = round(percentile(samples, 95), 3)
        last["pre_llm_ms_samples"] = [round(x, 3) for x in samples]
        last["total_prompt_tokens"] = last["history_tokens"] + last["injected_tokens"]
        rows.append(last)
        # do not delete home until process ends; OS cleans tmp
    first, final = rows[0], rows[-1]
    injected_slope = (
        None if first["turns"] == 0
        else round(final["injected_tokens"] / max(1, first["injected_tokens"]), 3)
    )
    history_slope = round(final["history_tokens"] / max(1, first["history_tokens"]), 3)
    return {
        "estimator": estimator,
        "python": sys.version.split()[0],
        "lengths": lengths,
        "rows": rows,
        "injected_tokens_last_vs_first": injected_slope,
        "history_tokens_last_vs_first": history_slope,
        "plugin_can_replace_core_history": False,
        "notes": (
            "Injected context is what pre_llm_call returns. History is the "
            "synthetic conversation the core would still send on v0.23 "
            "(hook cannot replace it). bounded_view is only present after "
            "the memory module lands."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--label", default="run")
    p.add_argument("--out", default="")
    p.add_argument("--lengths", default="10,25,50,100,200")
    args = p.parse_args(argv)
    lengths = [int(x) for x in args.lengths.split(",") if x.strip()]
    result = run_session(lengths)
    result["label"] = args.label
    result["commit"] = (
        os.popen(f"git -C {ROOT} rev-parse --short HEAD").read().strip()
    )
    result["branch"] = (
        os.popen(f"git -C {ROOT} branch --show-current").read().strip()
    )
    text = json.dumps(result, indent=2, ensure_ascii=False)
    print(text)
    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
