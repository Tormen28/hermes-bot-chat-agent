#!/usr/bin/env python3
"""Strict continuous stability runner for bot-chat-agent (stdlib only).

Replaces the 0.3.0-dev ad-hoc runner. What changed, and why (audit 2026-11-09):

* a quick check passes ONLY if the process exit code is 0 AND any JSON it prints
  has ``ok is True`` and no ``fallos``/``failed`` entries (the old runner treated a
  numeric ``ok: 39`` as truthy and ignored the exit code whenever ``ok`` existed);
* the battery is compared by TEST NAME against ``known_failures.json`` (set equality:
  a new failure hidden by a fixed one is caught, and a baseline that improved is
  reported), never by counts;
* ``hermes send`` is checked (return code + output) and recorded; during the run NO
  Telegram alerts are sent - failures go to the JSONL log and ONE final message is sent
  (``--notify-final``) whose delivery result is itself logged;
* every check records its detail (first failure line), so a "0 failures" claim can be
  audited from the log instead of trusted.

Usage (PowerShell):
  python runner.py --plugin <installed plugin dir> --python <python with pytest> \
      --live-python <hermes venv python> --minutes 60 --battery-every 8 --notify-final
Exit code 0 only if every cycle passed.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
QUICK_TEST_FILES = [
    "tests/test_guard_hardening.py", "tests/test_core_compat.py", "tests/test_concurrency_spawn.py",
    "tests/test_runner_strict.py", "tests/test_fase0_a07_injection.py", "tests/test_r26_audit.py",
]


# ----------------------------------------------------------------- pure judges
def last_json(text: str) -> Optional[dict]:
    """The last top-level JSON object printed in ``text`` (None if there is none)."""
    if not text or "{" not in text:
        return None
    decoder = json.JSONDecoder()
    found = None
    idx = text.find("{")
    while idx != -1:
        try:
            obj, end = decoder.raw_decode(text, idx)
            if isinstance(obj, dict):
                found = obj
            idx = text.find("{", end)
        except ValueError:
            idx = text.find("{", idx + 1)
    return found


def judge_quick(returncode: int, stdout: str, timed_out: bool = False) -> tuple[bool, str]:
    """Strict verdict for a quick check (exit code AND JSON ``ok is True``)."""
    if timed_out:
        return False, "timeout"
    if returncode != 0:
        return False, f"exit code {returncode}"
    data = last_json(stdout)
    if data is not None:
        if "ok" in data and data["ok"] is not True:
            return False, f"ok={data['ok']!r} (must be True)"
        for key in ("fallos", "failed", "failures", "errors"):
            if data.get(key):
                return False, f"{key} not empty: {str(data[key])[:160]}"
    return True, "ok"


_FAILED_RE = re.compile(r"^(FAILED|ERROR)\s+(\S+)", re.M)
_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed|deselected)")


def parse_pytest(output: str) -> dict[str, Any]:
    """Failed node ids and counts from a pytest ``-q -rfEs`` run."""
    failed = {m.group(2).split(" - ")[0] for m in _FAILED_RE.finditer(output or "")}
    counts: dict[str, int] = {}
    tail = [ln for ln in (output or "").splitlines() if re.search(r"\b(passed|failed|error)", ln)][-1:] or [""]
    for n, word in _COUNT_RE.findall(tail[0]):
        counts[word.rstrip("s") if word.startswith("error") else word] = int(n)
    return {"failed": failed, "counts": counts, "passed": counts.get("passed", 0),
            "skipped": counts.get("skipped", 0), "summary_line": tail[0].strip()}


def compare_failures(expected: set[str], got: set[str]) -> dict[str, list[str]]:
    """Exact set comparison: unexpected = new regressions; fixed = baseline improved."""
    return {"unexpected": sorted(got - expected), "fixed": sorted(expected - got)}


def judge_battery(parsed: dict[str, Any], expected_failed: set[str], min_passed: int,
                  returncode: int) -> tuple[bool, str]:
    if not parsed["summary_line"]:
        return False, "no pytest summary (crash or hang)"
    cmp = compare_failures(expected_failed, parsed["failed"])
    if cmp["unexpected"]:
        return False, "NEW failures: " + ", ".join(cmp["unexpected"][:6])
    if cmp["fixed"]:
        return False, "baseline improved, update known_failures.json: " + ", ".join(cmp["fixed"][:6])
    if parsed["passed"] < min_passed:
        return False, f"only {parsed['passed']} passed (< {min_passed})"
    if returncode not in (0, 1):
        return False, f"pytest exit code {returncode}"
    if not expected_failed and returncode != 0:
        return False, f"pytest exit code {returncode} with an empty baseline"
    return True, parsed["summary_line"]


def notify(message: str, runner: list[str] | None = None, timeout: int = 60) -> dict[str, Any]:
    """Send one Telegram message through the official CLI and REPORT the outcome."""
    cmd = (runner or ["hermes", "send", "-t", "telegram"]) + [message]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # missing binary, timeout...
        return {"sent": False, "detail": f"{type(exc).__name__}: {exc}"}
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    out = re.sub(r"\d{6,}:[A-Za-z0-9_-]{30,}", "<token>", out)[:300]
    return {"sent": proc.returncode == 0, "returncode": proc.returncode, "detail": out}


# --------------------------------------------------------------- orchestration
def run(cmd: list[str], cwd: Path, timeout: int, env: dict | None = None) -> tuple[int, str, bool]:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, **(env or {})}, encoding="utf-8", errors="replace")
        return proc.returncode, (proc.stdout or "") + (proc.stderr or ""), False
    except subprocess.TimeoutExpired as exc:
        return -9, str(exc.stdout or "")[-2000:], True


def load_baseline(path: Path) -> tuple[set[str], int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    plat = "windows" if platform.system() == "Windows" else "posix"
    section = data.get(plat, {})
    return set(section.get("failed", [])), int(section.get("min_passed", 0))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plugin", required=True, help="plugin directory under test (the installed copy)")
    ap.add_argument("--python", default=sys.executable, help="python with pytest (no hermes core needed)")
    ap.add_argument("--live-python", default="", help="hermes venv python for live_probe.py (skipped if empty)")
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--cycles", type=int, default=0, help="stop after N cycles (0 = use --minutes)")
    ap.add_argument("--battery-every", type=int, default=8)
    ap.add_argument("--sleep", type=float, default=8.0)
    ap.add_argument("--log", default="runner-strict.jsonl")
    ap.add_argument("--notify-final", action="store_true", help="ONE message at the end; none during the run")
    ap.add_argument("--hermes-agent", default=os.environ.get("HERMES_AGENT_CHECKOUT", ""))
    ap.add_argument("--hermes-fase3b-python", default=os.environ.get("HERMES_FASE3B_PYTHON", ""))
    args = ap.parse_args(argv)

    plugin = Path(args.plugin).resolve()
    log = Path(args.log).resolve()
    expected_failed, min_passed = load_baseline(HERE / "known_failures.json")
    env = {}
    if args.hermes_agent:
        env["HERMES_AGENT_CHECKOUT"] = args.hermes_agent
    if args.hermes_fase3b_python:
        env["HERMES_FASE3B_PYTHON"] = args.hermes_fase3b_python

    def record(entry: dict) -> None:
        entry["t"] = datetime.now().isoformat(timespec="seconds")
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"[{entry['t'][11:]}] cycle {entry.get('cycle')} {entry['check']}: "
              f"{'ok' if entry['ok'] else 'FAIL'} - {entry['detail']}", flush=True)

    started = time.time()
    deadline = started + args.minutes * 60
    cycle, failures = 0, []
    while True:
        cycle += 1
        checks: list[tuple[str, list[str], int, str]] = []
        py = args.python
        checks.append(("quick-tests", [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfEs",
                                       "--timeout=120", "--timeout-method=thread", *QUICK_TEST_FILES],
                       600, "pytest"))
        if args.live_python:
            checks.append(("live-probe", [args.live_python, str(HERE / "live_probe.py"), str(plugin)], 300, "json"))
        checks.append(("r26-audit", [py, str(HERE / "r26_audit.py"), str(plugin)], 300, "json"))
        if args.battery_every and cycle % args.battery_every == 0:
            checks.append(("battery", [py, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-rfEs",
                                       "--timeout=180", "--timeout-method=thread"], 1500, "battery"))
        for name, cmd, to, kind in checks:
            t0 = time.time()
            rc, out, timed_out = run(cmd, plugin, to, env)
            if kind == "pytest":
                parsed = parse_pytest(out)
                ok, detail = judge_quick(rc, out, timed_out)
                if ok and (parsed["failed"] or not parsed["passed"]):
                    ok, detail = False, f"failed={sorted(parsed['failed'])[:4]} passed={parsed['passed']}"
                elif ok:
                    detail = parsed["summary_line"]
            elif kind == "battery":
                parsed = parse_pytest(out)
                ok, detail = judge_battery(parsed, expected_failed, min_passed, rc) if not timed_out else (False, "timeout")
            else:
                ok, detail = judge_quick(rc, out, timed_out)
                if not ok:
                    detail += " | " + out.strip().replace("\n", " ")[-200:]
            record({"cycle": cycle, "check": name, "ok": ok, "rc": rc, "seconds": round(time.time() - t0, 1),
                    "detail": detail})
            if not ok:
                failures.append(f"cycle {cycle} {name}: {detail}")
        if args.cycles and cycle >= args.cycles:
            break
        if not args.cycles and time.time() >= deadline:
            break
        time.sleep(args.sleep)

    minutes = round((time.time() - started) / 60, 1)
    summary = (f"bot-chat-agent stability run finished: {cycle} cycles in {minutes} min, "
               f"{len(failures)} failure(s)" + ("" if not failures else ": " + failures[0][:160]))
    result = {"cycle": cycle, "check": "summary", "ok": not failures, "detail": summary}
    if args.notify_final:
        sent = notify(("OK " if not failures else "FAIL ") + summary)
        result["telegram"] = sent
        result["detail"] += f" | telegram sent={sent['sent']}"
    record(result)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
