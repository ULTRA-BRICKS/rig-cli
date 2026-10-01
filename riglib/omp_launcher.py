"""The omp tmux pane save/restore executors ``riglib.tmux``'s rendered ``omp-save``/``omp-restore``
scripts shell out to (``rig omp save-panes|restore-panes``).

Hosting a harness in abduco — run, attach, list, the crash supervisor, the PATH wrapper — is
:mod:`riglib.harness_launcher` / :mod:`riglib.harness_host` (``rig harness``). A hosted session's
identity is a random name rig generates (never the cwd: the same cwd legitimately hosts different
sessions, see :mod:`riglib.omp_recovery`'s module docstring), recovered for OUR OWN inspection via
abduco-argv ancestry (see :mod:`riglib.omp_inspect`'s module docstring) — NOT by reading the
child's environment back, which macOS restricts even for the owning user.

Every one-shot inspection subprocess this module runs uses ``stdin=subprocess.DEVNULL``.
"""

from __future__ import annotations

import os
import shutil
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable

from . import omp_inspect
from .harness_host import validate_name
from .omp_recovery import RestorePlan, SavedPane, plan_restore

DEFAULT_TMUX_BIN = "tmux"

#: shells a pane must be idle in for a restore to touch it — never clobber a running program.
_FRESH_SHELLS = {
    "sh",
    "bash",
    "zsh",
    "fish",
    "dash",
    "ksh",
    "tcsh",
    "-sh",
    "-bash",
    "-zsh",
}


# ── omp-save: cwd -> newest session file (ported from riglib.tmux's bash renderer) ──────────
def encode_cwd(cwd: str, home: Path) -> str:
    """cwd relative to ``home``, every ``/`` -> ``-`` (dots preserved) — verified against a
    real on-disk omp session dir; NOT Claude Code's full-absolute-path/dot-folding encoding."""
    home_str = str(home)
    if cwd == home_str:
        rel = ""
    elif cwd.startswith(home_str + "/"):
        rel = cwd[len(home_str) :]
    else:
        rel = cwd
    return rel.replace("/", "-")


def newest_session_file(sessions_dir: Path) -> Path | None:
    """Newest top-level ``*.jsonl`` (by mtime) directly under ``sessions_dir`` — a nested
    Task/eval-child transcript lives in a SIBLING ``<stem>/`` dir, never matching this glob."""
    if not sessions_dir.is_dir():
        return None
    candidates = [p for p in sessions_dir.glob("*.jsonl") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# ── the pane map (addr, cwd, session_file, durable_session) ─────────────────────────────────
def render_map_line(
    addr: str,
    cwd: str,
    session_file: str,
    durable_session: str | None,
    session_id: str | None = None,
) -> str:
    fields = [addr, cwd, session_file, durable_session or "", session_id or ""]
    if any("\n" in f or "\r" in f or "\t" in f for f in fields):
        raise ValueError("pane map fields cannot contain control separators")
    return "\t".join(fields)


def parse_map_record(line: str) -> tuple[str, str, str, str | None, str | None] | None:
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 3 or not parts[0]:
        return None
    return (
        parts[0],
        parts[1],
        parts[2],
        parts[3] if len(parts) > 3 and parts[3] else None,
        parts[4] if len(parts) > 4 and parts[4] else None,
    )


def parse_map_line(line: str) -> tuple[str, str, str, str | None] | None:
    record = parse_map_record(line)
    return record[:4] if record else None


def read_map_records(
    map_path: Path,
) -> list[tuple[str, str, str, str | None, str | None]]:
    try:
        text = map_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [
        record
        for line in text.splitlines()
        if (record := parse_map_record(line)) is not None
    ]


def read_map(map_path: Path) -> list[tuple[str, str, str, str | None]]:
    return [record[:4] for record in read_map_records(map_path)]


def write_map_atomic(
    map_path: Path,
    entries: list[
        tuple[str, str, str, str | None] | tuple[str, str, str, str | None, str | None]
    ],
) -> None:
    """Atomic replace (write to a sibling temp file, then ``os.replace``) so a crash mid-save
    never leaves a half-written map — a reader always sees either the old or the new map."""
    import tempfile

    body = "".join(render_map_line(*e) + "\n" for e in entries)
    map_path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=map_path.name + ".tmp-", dir=map_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, map_path)
    finally:
        Path(name).unlink(missing_ok=True)


# ── tmux subprocess helpers ──────────────────────────────────────────────────────────────────
def list_tmux_panes(
    tmux_bin: str = DEFAULT_TMUX_BIN, timeout: float = 10.0
) -> list[tuple[str, int, str]] | None:
    """``[(addr, pane_pid, cwd), ...]`` for every live pane, or ``None`` on any tmux failure
    (no server, tmux missing, timeout) — callers must treat that as "nothing to save/restore
    right now", never as "zero panes"."""
    try:
        proc = subprocess.run(
            [
                tmux_bin,
                "list-panes",
                "-a",
                "-F",
                "#{session_name}:#{window_index}.#{pane_index}\t#{pane_pid}\t#{pane_current_path}",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    panes = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        panes.append((parts[0], pid, parts[2]))
    return panes


def pane_current_command(
    addr: str, tmux_bin: str = DEFAULT_TMUX_BIN, timeout: float = 10.0
) -> str | None:
    try:
        proc = subprocess.run(
            [tmux_bin, "display-message", "-p", "-t", addr, "#{pane_current_command}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def send_keys(
    addr: str, command: str, tmux_bin: str = DEFAULT_TMUX_BIN, timeout: float = 10.0
) -> bool:
    try:
        proc = subprocess.run(
            [tmux_bin, "send-keys", "-t", addr, command, "Enter"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def shell_quote(value: str) -> str:
    return shlex.quote(value)


# ── rig omp save-panes ───────────────────────────────────────────────────────────────────────
def save_panes(
    map_path: Path, *, tmux_bin: str = DEFAULT_TMUX_BIN, tg_ctl_bin: str = "tg-ctl"
) -> bool:
    """Save only exact verified live session identities; uncertainty preserves prior map."""
    panes = list_tmux_panes(tmux_bin)
    snapshot = omp_inspect.capture_ps_snapshot()
    if panes is None or snapshot is None:
        return False
    rows = omp_inspect.parse_ps_rows(snapshot)
    hosts, ok = omp_inspect.live_omp_sessions(tg_ctl_bin)
    if not ok:
        return False
    entries = []
    for addr, pane_pid, cwd in panes:
        match = omp_inspect.find_omp_descendant(pane_pid, rows)
        if match is None:
            continue
        durable = omp_inspect.pane_durable_session(match)
        choices = [
            h
            for h in hosts
            if (h.durable_session == durable if durable else h.pid == match.pid)
        ]
        if len(choices) != 1:
            return False
        host = choices[0]
        file = getattr(host, "session_file", None)
        if (
            not host.session_id
            or not file
            or Path(host.cwd).resolve() != Path(cwd).resolve()
        ):
            return False
        header = omp_inspect.session_header(file)
        if not header or header["id"] != host.session_id:
            return False
        entries.append(
            (addr, host.cwd, file, host.durable_session or durable, host.session_id)
        )
    write_map_atomic(map_path, entries)
    return True


# ── rig omp restore-panes ────────────────────────────────────────────────────────────────────
def _pane_is_empty_shell(addr: str, tmux_bin: str) -> bool | None:
    cur = pane_current_command(addr, tmux_bin)
    if cur is None:
        return None
    return cur in _FRESH_SHELLS


def _blocked_message(cwd: str, plan: RestorePlan) -> str:
    owner = f" (owner pid {plan.owner_pid})" if plan.owner_pid is not None else ""
    return f"omp restore blocked for {cwd}: {plan.reason}{owner} — NOT auto-resuming, would risk duplicating it"


def resolve_rig_binary(requested: str | None = None) -> str | None:
    """Pin the executable before sending into a shell with a possibly smaller PATH."""
    candidates = (
        [requested]
        if requested
        else [
            shutil.which("rig"),
            str(Path(__file__).resolve().parents[1] / "bin" / "rig"),
        ]
    )
    for candidate in candidates:
        if candidate:
            found = shutil.which(candidate)
            if found:
                return str(Path(found).resolve())
    return None


def restore_one_pane(
    addr: str,
    cwd: str,
    session_file: str,
    durable_session: str | None,
    *,
    live: list,
    live_ok: bool,
    durable_sockets: set[str],
    durable_ok: bool,
    tmux_bin: str = DEFAULT_TMUX_BIN,
    warn_log_path: Path | None = None,
    rig_bin: str | None = None,
    session_id: str | None = None,
) -> RestorePlan:
    """Decide + EXECUTE (via ``tmux send-keys``) the restore action for one saved pane, using
    :func:`riglib.omp_recovery.plan_restore` for the decision. Returns the plan for logging;
    callers own inspection (``live``/``durable_sockets``) so this stays testable without a
    live tmux server. A ``blocked`` decision is never silent: it echoes a visible diagnostic
    into the pane (impossible to miss, mirrors the pre-#619 duplicate-guard's own UX) and, when
    ``warn_log_path`` is given, appends an audit-trail line — unless the pane itself could not
    even be reached (``pane_unreachable``), in which case there is nowhere to echo into."""
    pane_empty = _pane_is_empty_shell(addr, tmux_bin)
    if pane_empty is None:
        return RestorePlan("blocked", "pane_unreachable")
    session_id = session_id or omp_inspect.saved_session_id_for(session_file)
    saved = SavedPane(addr, cwd, session_id, session_file, durable_session)
    # A legacy saved pane with no durable id does not need an abduco listing to
    # prove absence of a live OMP owner: live_ok already covers that safety
    # boundary. Requiring abduco here made clean Linux runners (and hosts that
    # have not opted into durable sessions yet) block an otherwise exact
    # resume. Once a durable id is recorded, however, failing to inspect the
    # durable namespace must still fail closed because an attachable original
    # may exist outside tmux.
    durable_inspection_needed = bool(durable_session)
    plan = plan_restore(
        saved,
        live,
        durable_sockets,
        inspection_ok=live_ok and (durable_ok or not durable_inspection_needed),
        session_file_exists=Path(session_file).exists(),
        pane_is_empty_shell=pane_empty,
    )
    if plan.action in {"attach", "resume"}:
        executable = resolve_rig_binary(rig_bin)
        if executable is None:
            return RestorePlan("blocked", "rig_binary_unavailable")
        rig_command = shell_quote(executable)
    if plan.action == "attach":
        if not send_keys(
            addr,
            f"cd {shell_quote(cwd)} && {rig_command} harness attach {shell_quote(plan.argv[-1])}",
            tmux_bin,
        ):
            return RestorePlan("blocked", "pane_delivery_failed")
    elif plan.action == "resume":
        import hashlib

        durable = (
            durable_session
            or "omp-recover-" + hashlib.sha256(session_id.encode()).hexdigest()[:20]
        )
        validate_name(durable)
        command = f"cd {shell_quote(cwd)} && {rig_command} harness run omp --host-id {shell_quote(durable)} --resume {shell_quote(plan.argv[-1])}"
        if not send_keys(addr, command, tmux_bin):
            return RestorePlan("blocked", "pane_delivery_failed")
    elif plan.action == "blocked":
        message = _blocked_message(cwd, plan)
        send_keys(addr, f"echo {shell_quote('⚠ ' + message)}", tmux_bin)
        if warn_log_path is not None:
            _append_warn_log(warn_log_path, addr, message)
    return plan


def _append_warn_log(path: Path, addr: str, message: str) -> None:
    import time as _time

    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{_time.strftime('%Y-%m-%dT%H:%M:%SZ', _time.gmtime())} {message} (addr={addr})\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def restore_panes(
    map_path: Path,
    *,
    tmux_bin: str = DEFAULT_TMUX_BIN,
    tg_ctl_bin: str = "tg-ctl",
    rig_bin: str | None = None,
    log_fn: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr),
) -> int:
    """The Python body of the ``omp-restore`` script ``render_omp_restore`` renders as a thin
    wrapper. Returns the number of panes BLOCKED (never silently duplicated) so a caller can
    surface a non-zero-but-not-fatal signal."""
    entries = read_map_records(map_path)
    if not entries:
        return 0
    live, live_ok = omp_inspect.live_omp_sessions(tg_ctl_bin)
    durable_sockets, durable_ok = omp_inspect.list_abduco_sessions()
    blocked = 0
    warn_log_path = map_path.parent / "omp-restore-warnings.log"
    for addr, cwd, session_file, durable, session_id in entries:
        plan = restore_one_pane(
            addr,
            cwd,
            session_file,
            durable,
            live=live,
            live_ok=live_ok,
            durable_sockets=durable_sockets,
            durable_ok=durable_ok,
            tmux_bin=tmux_bin,
            warn_log_path=warn_log_path,
            rig_bin=rig_bin,
            session_id=session_id,
        )
        log_fn(f"{addr}: {plan.action} ({plan.reason})")
        if plan.action == "blocked":
            blocked += 1
    return blocked
