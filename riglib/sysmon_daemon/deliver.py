"""Type a sysmon note into a running agent's terminal — a tmux pane or a cmux surface — only
while its input box is provably empty (rig-cli#622).

The notify rung prefers channels that cannot collide with a human (``tg-ctl omp-live send``,
the Stop-hook inbox), but an agent in a tmux pane without the omp-live extension has neither:
in incident 20260929T180907-live both notifications failed. So the note is typed into the
agent's terminal the way ``tmux send-keys`` / ``cmux send`` do, under three guards:

1. the target is resolved LIVE from the host pid — tmux: the pane whose process is an ancestor
   of the host right now (never a window index from a census up to a minute old); cmux: the
   workspace/surface ids the host inherited (``CMUX_WORKSPACE_ID``/``CMUX_SURFACE_ID``);
2. the screen must show the harness's input box — the lines between the two lowest horizontal
   rules, right above its status line(s), as omp and Claude Code draw it — and the box must be
   EMPTY (a prompt glyph at most). Typed user text, a placeholder, a menu, a dialog, tmux copy
   mode or an unknown layout all refuse, and the caller falls back to the Telegram alert;
3. after typing, the box must show the whole note and nothing else before Enter is pressed; if
   a human typed in the meantime, or the box shows only part of the note (it scrolled or cut it
   short), Enter is withheld so neither their keystrokes nor a partial note are ever submitted.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import probe
from .model import AgentHost

#: Ink-style TUIs drop an Enter that arrives with the text (tg-cli learned this); pause first,
#: and re-read the box up to this many times while the TUI renders what was typed.
SUBMIT_DELAY_S = 0.5
RENDER_POLLS = 3
CMD_TIMEOUT_S = 10.0
#: Lines read from the bottom of the terminal.
SCREEN_LINES = 60
#: The input box sits right above the status line(s): omp draws one, Claude Code two.
MAX_LINES_BELOW_BOX = 4
MAX_BOX_LINES = 40
MIN_RULE_WIDTH = 10
RULE_CHARS = frozenset("─━")
PROMPT_GLYPHS = ("❯", "›", ">")
BOX_EDGES = ("│", "┃")
MAX_ANCESTRY = 64
#: A typed note must fit an agent's input box without pushing its top rule off a small pane (the
#: box would no longer be readable, so Enter would be withheld and the note left behind).
MAX_NOTE_CHARS = 600

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def note_line(text: str) -> str:
    """The note as ONE line of printable text, at most ``MAX_NOTE_CHARS``: a newline typed into a
    TUI submits it half-way, and ``cmux send`` turns ``\\n``/``\\t`` escapes into keypresses."""
    printable = "".join(ch if ch.isprintable() else " " for ch in text.replace("\\", "/"))
    line = " ".join(printable.split())
    return line if len(line) <= MAX_NOTE_CHARS else line[:MAX_NOTE_CHARS - 1].rstrip() + "…"


def input_box(screen: str) -> Optional[List[str]]:
    """The input box's rows (between the two lowest horizontal rules, near the bottom of the
    screen), or ``None`` when the screen does not show one."""
    rows = [row.rstrip() for row in screen.splitlines()]
    while rows and not rows[-1].strip():
        rows.pop()
    rules = [i for i, row in enumerate(rows) if _is_rule(row)]
    if len(rules) < 2:
        return None
    top, bottom = rules[-2], rules[-1]
    if len(rows) - 1 - bottom > MAX_LINES_BELOW_BOX or not 0 < bottom - top - 1 <= MAX_BOX_LINES:
        return None
    return rows[top + 1:bottom]


def _is_rule(row: str) -> bool:
    s = row.strip()
    if s[:1] in ("╭", "╰") and s[-1:] in ("╮", "╯"):
        s = s[1:-1]
    return len(s) >= MIN_RULE_WIDTH and set(s) <= RULE_CHARS


def box_text(box: Sequence[str]) -> str:
    """What the box holds, without the prompt glyph, box edges or any whitespace (``""`` = empty)."""
    parts = []
    for i, row in enumerate(box):
        s = row.strip()
        if s.startswith(BOX_EDGES):
            s = s[1:]
        if s.endswith(BOX_EDGES):
            s = s[:-1]
        s = s.strip()
        if i == 0 and s.startswith(PROMPT_GLYPHS):
            s = s[1:]
        parts.append(s)
    return "".join("".join(parts).split())


def to_pane(
    host: AgentHost,
    text: str,
    *,
    tmux_bin: str = "tmux",
    cmux_bin: str = "cmux",
    run: Runner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    ppid_of: Optional[Callable[[int], Optional[int]]] = None,
) -> Tuple[bool, str]:
    """``(delivered, how/why)``. Never raises. ``ppid_of`` defaults to the live libproc/proc read."""
    ppid_of = ppid_of or probe.proc_ppid
    pane = host.pane or ""
    note = note_line(text)
    if pane.startswith("tmux:"):
        target, why = _tmux_pane_of(host.pid, tmux_bin, run, ppid_of)
        if target is None:
            return False, why
        return _deliver(
            f"tmux pane {target}", note, sleep,
            read=lambda: _run([tmux_bin, "capture-pane", "-p", "-t", target], run),
            type_=lambda: _run([tmux_bin, "send-keys", "-t", target, "-l", "--", note], run) is not None,
            enter=lambda: _run([tmux_bin, "send-keys", "-t", target, "Enter"], run) is not None,
        )
    workspace, _, surface = pane[len("cmux:"):].partition("/") if pane.startswith("cmux:") else ("", "", "")
    if workspace and surface:
        where = ["--workspace", workspace, "--surface", surface]
        return _deliver(
            f"cmux surface {surface}", note, sleep,
            read=lambda: _run([cmux_bin, "read-screen", *where, "--lines", str(SCREEN_LINES)], run),
            type_=lambda: _run([cmux_bin, "send", *where, "--", note], run) is not None,
            enter=lambda: _run([cmux_bin, "send-key", *where, "--", "enter"], run) is not None,
        )
    return False, f"pane {pane or 'unknown'}: no way to address its terminal"


def _deliver(
    where: str,
    note: str,
    sleep: Callable[[float], None],
    *,
    read: Callable[[], Optional[str]],
    type_: Callable[[], bool],
    enter: Callable[[], bool],
) -> Tuple[bool, str]:
    screen = read()
    if screen is None:
        return False, f"{where}: screen unreadable"
    box = input_box(screen)
    if box is None:
        return False, f"{where}: no empty agent input box on screen"
    if box_text(box):
        return False, f"{where}: the input box holds text (a human's input is never submitted)"
    if not type_():
        return False, f"{where}: typing failed"
    wanted = "".join(note.split())
    for _ in range(RENDER_POLLS):
        sleep(SUBMIT_DELAY_S)
        after = read()
        box = input_box(after) if after is not None else None
        typed = box_text(box) if box is not None else ""
        # The whole note: submit. A prefix is still being rendered (or was cut short): wait.
        # Anything else — a scrolled box showing only the tail included — may hold somebody
        # else's keystrokes in the part it hides.
        if typed == wanted:
            if not enter():
                return False, f"{where}: typed, but Enter failed"
            return True, f"typed into {where}"
        if typed and not wanted.startswith(typed):
            break
    return False, f"{where}: typed but NOT submitted (the input box changed while typing)"


def _tmux_pane_of(
    pid: int, tmux_bin: str, run: Runner, ppid_of: Callable[[int], Optional[int]]
) -> Tuple[Optional[str], str]:
    """The id (``%N``, never reused within a server) of the pane that holds ``pid`` now."""
    out = _run([tmux_bin, "list-panes", "-a", "-F", "#{pane_pid}\t#{pane_id}\t#{pane_in_mode}"], run)
    if out is None:
        return None, "tmux list-panes failed"
    panes: Dict[int, Tuple[str, str]] = {}
    for line in out.splitlines():
        pane_pid, _, rest = line.partition("\t")
        pane_id, _, in_mode = rest.partition("\t")
        if pane_pid.isdigit() and pane_id:
            panes[int(pane_pid)] = (pane_id, in_mode.strip())
    cur: Optional[int] = pid
    for _ in range(MAX_ANCESTRY):
        if cur is None or cur <= 1:
            break
        if cur in panes:
            pane_id, in_mode = panes[cur]
            if in_mode != "0":
                return None, f"tmux pane {pane_id} is in copy mode"
            return pane_id, ""
        cur = ppid_of(cur)
    return None, f"no tmux pane holds pid {pid} now"


def _run(cmd: List[str], run: Runner) -> Optional[str]:
    """stdout on exit 0, else ``None``. UTF-8 forced: under launchd the locale is bare and tmux
    would print every box-drawing rule as ``_``."""
    try:
        proc = run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=CMD_TIMEOUT_S,
                   stdin=subprocess.DEVNULL, env={**os.environ, "LC_ALL": "en_US.UTF-8"})
    except (OSError, subprocess.SubprocessError):
        return None
    return (proc.stdout or "") if proc.returncode == 0 else None
