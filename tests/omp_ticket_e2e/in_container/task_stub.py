#!/usr/bin/env python3
"""Recording stand-in for the real `task` CLI (task-cli), used ONLY by the ticket-gate agent E2E.

Why a stub and not task-cli: the experiment measures whether an agent RUNS `task new` after the
require-ticket block, not task-cli's backend. The stub speaks the real CLI's surface closely
enough that the agent's behaviour is realistic:
  * `task new` enforces the real gate shape (title, >= 2 repeatable --acceptance, --why,
    --impact, --if-not-done) and refuses with the real refusal text + exit 2, so a malformed
    first attempt costs a retry exactly like production;
  * a created ticket is announced as `created #<n>  <url>`; a bare `#<n>` satisfies the hook's
    ticket-reference detection when the agent puts it in the commit message;
  * every call is appended to $E2E_LOG_DIR/task-calls.jsonl (argv, rc, cwd) for the analyzer.

E2E_TASK_MODE:
  ok            behave like a healthy backend (default)
  screenshot-gate  a UI/visual ticket: a well-formed `task new` is refused by the real
                screenshots gate until it carries --screenshot <path> or
                --skip-screenshots "<reason>" (scenario B: the agent must read the refusal,
                fix the flags and retry)
  backend-down  every `task new` fails like a dead backend (exit 2, BackendError text)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

LOG_DIR = Path(os.environ.get("E2E_LOG_DIR", "/tmp/e2e-logs"))
STATE = LOG_DIR / "task-state.json"
FIRST_ID = 101
BASE_URL = "https://git.hyperide.ai/ultrabricks/wall-plugs/issues"

# Real refusal shapes, copied from task-cli's gate output.
REFUSE_HEADER = "refusing to create: {n} gate(s) unmet"
REFUSE_FOOTER = "error: policy gates not satisfied"
GATE_ACCEPTANCE = (
    "  ✗ acceptance-criteria: at least 2 acceptance criteria are required (have {have})\n"
    '      → add --acceptance "<criterion>" (repeatable)  (or --skip-acceptance-criteria "<reason>")'
)
GATE_FIELD = (
    "  ✗ {gate}: {what} is required\n"
    '      → add --{flag} "<text>"  (or --skip-{gate} "<reason>")'
)
GATE_LINKS = (
    "  ✗ links: related entities must be links, not bare references: #123 [issue/PR ref]\n"
    "      → write the link INSIDE the text field that names it (--what/--why/--impact/--if-not-done/"
    "--acceptance): either a markdown link [text](https://…) or the bare https://… URL  "
    '(or --skip-links "<reason>")'
)
GATE_SCREENSHOTS = (
    "  ✗ screenshots: a creation screenshot is required for UI/visual tickets\n"
    '      → add --screenshot <path>  (or --skip-screenshots "<reason>")'
)
BACKEND_DOWN = (
    "error: backend error: github-issues: POST /repos/ultrabricks/wall-plugs/issues failed: "
    "HTTP 502 Bad Gateway (backend unreachable)\n"
)


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"next_id": FIRST_ID, "new_calls": 0}


def _save_state(state: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))


def _log(argv: list[str], rc: int, note: str = "") -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.time(), "argv": argv, "rc": rc, "cwd": os.getcwd(), "note": note}
    with (LOG_DIR / "task-calls.jsonl").open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def parse_new(args: list[str]) -> dict[str, list[str]]:
    """Collect `--flag value` pairs (repeatable) from a `task new` argv; unknown flags are kept."""
    out: dict[str, list[str]] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            key, eq, inline = arg[2:].partition("=")
            if eq:
                out.setdefault(key, []).append(inline)
            elif i + 1 < len(args) and not args[i + 1].startswith("--"):
                out.setdefault(key, []).append(args[i + 1])
                i += 1
            else:
                out.setdefault(key, []).append("")
        i += 1
    return out


def gate_failures(flags: dict[str, list[str]]) -> list[str]:
    failures: list[str] = []
    if not any(v.strip() for v in flags.get("title", [])):
        failures.append(GATE_FIELD.format(gate="title", what="a title", flag="title"))
    acc = [v for v in flags.get("acceptance", []) if v.strip()]
    if len(acc) < 2:
        failures.append(GATE_ACCEPTANCE.format(have=len(acc)))
    for flag, gate, what in (
        ("why", "motivation", "motivation (--why)"),
        ("impact", "user-impact", "user impact (--impact)"),
        ("if-not-done", "cost-of-inaction", "cost of inaction (--if-not-done)"),
    ):
        if not any(v.strip() for v in flags.get(flag, [])) and gate not in _skipped(flags):
            failures.append(GATE_FIELD.format(gate=gate, what=what, flag=flag))
    body = " ".join(" ".join(v) for k, v in flags.items() if k != "title")
    if "#" in body and any(tok.startswith("#") and tok[1:2].isdigit() for tok in body.split()):
        failures.append(GATE_LINKS)
    return failures


def _skipped(flags: dict[str, list[str]]) -> set[str]:
    return {k[len("skip-") :] for k in flags if k.startswith("skip-")}


def cmd_new(argv: list[str]) -> int:
    mode = os.environ.get("E2E_TASK_MODE", "ok")
    state = _load_state()
    state["new_calls"] = state.get("new_calls", 0) + 1
    flags = parse_new(argv[1:])
    if mode == "backend-down":
        sys.stderr.write(BACKEND_DOWN)
        _save_state(state)
        _log(argv, 2, "backend-down")
        return 2
    failures = gate_failures(flags)
    if mode == "screenshot-gate" and not ({"screenshot", "skip-screenshots"} & set(flags)):
        failures.append(GATE_SCREENSHOTS)
    if failures:
        sys.stderr.write(REFUSE_HEADER.format(n=len(failures)) + "\n" + "\n".join(failures) + "\n")
        sys.stderr.write(REFUSE_FOOTER + "\n")
        _save_state(state)
        _log(argv, 2, "gate-refused")
        return 2
    ticket = state["next_id"]
    state["next_id"] = ticket + 1
    _save_state(state)
    title = (flags.get("title") or [""])[0]
    sys.stdout.write(f"created #{ticket}  {BASE_URL}/{ticket}\n")
    sys.stdout.write(f"  title: {title}\n")
    _log(argv, 0, f"created #{ticket}")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(
            "usage: task <new|list|read|find|link|change|check|gate|done|status> ...\n"
            '  task new --title "..." --acceptance "..." --why "..." --impact "..." --if-not-done "..."\n'
        )
        _log(argv, 0, "help")
        return 0
    if argv[0] in ("new", "create"):
        return cmd_new(argv)
    if argv[0] == "--version":
        sys.stdout.write("task e2e-stub\n")
        _log(argv, 0, "version")
        return 0
    sys.stdout.write("(e2e stub: no tickets to show)\n")
    _log(argv, 0, "other")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
