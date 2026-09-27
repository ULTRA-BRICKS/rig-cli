"""The durable OMP launcher — ``rig omp run/attach/list`` — plus the pane save/restore
executors that ``riglib.tmux``'s rendered ``omp-save``/``omp-restore`` scripts shell out to.

``rig omp run``/``attach`` wrap the real interactive OMP CLI in a small, established
process/session manager (``abduco``, not a new PTY implementation — see AGENTS.md). A durable
session's identity is a random id rig generates (never the cwd — the same cwd legitimately
hosts different sessions, see :mod:`riglib.omp_recovery`'s module docstring), recorded in
``RIG_OMP_SESSION`` for the child (the shared contract with tg-cli's ``tg-ctl omp-live status``
bridge) and recovered for OUR OWN inspection via abduco-argv ancestry (see
:mod:`riglib.omp_inspect`'s module docstring — NOT via reading the child's environment back,
which macOS restricts even for the owning user).

Every one-shot inspection subprocess this module runs uses ``stdin=subprocess.DEVNULL``. The
two exec paths (``run``/``attach``) deliberately REPLACE the current process (``os.execvpe``)
so the terminal/pty is handed straight to abduco — they are meant to be the last thing a pane
runs, not spawn a child.
"""

from __future__ import annotations

import os
import shutil
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Callable, NoReturn

from . import omp_inspect
from .omp_recovery import RestorePlan, SavedPane, plan_restore

DEFAULT_ABDUCO_BIN = "abduco"
DEFAULT_TMUX_BIN = "tmux"


def validate_durable_id(value: str) -> str:
    import re

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,60}", value):
        raise ValueError("invalid durable session identifier")
    return value


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


def generate_durable_id() -> str:
    return f"omp-{uuid.uuid4().hex[:12]}"


# ── the abduco exec wrappers (`rig omp run` / `rig omp attach`) ──────────────────────────────
def build_run_argv(
    omp_argv: list[str], session_id: str, *, abduco_bin: str = DEFAULT_ABDUCO_BIN
) -> list[str]:
    """``abduco -f -A <session_id> <omp_argv...>`` — connect-or-create, so a duplicate `rig omp
    run` with the SAME id (e.g. a restore racing a still-starting durable launch) reuses the
    existing process instead of erroring. -f only replaces an already terminated worker;
    an active session is still attached without launching another process."""
    if not omp_argv:
        raise ValueError(
            "omp_argv must be non-empty (the real omp binary + its arguments)"
        )
    return [abduco_bin, "-f", "-A", validate_durable_id(session_id), *omp_argv]


def build_attach_argv(
    session_id: str, *, abduco_bin: str = DEFAULT_ABDUCO_BIN
) -> list[str]:
    return [abduco_bin, "-a", validate_durable_id(session_id)]


def run_env(
    session_id: str, *, base_env: dict[str, str] | None = None
) -> dict[str, str]:
    env = dict(base_env if base_env is not None else os.environ)
    env["RIG_OMP_SESSION"] = validate_durable_id(session_id)
    env["ABDUCO_SOCKET_DIR"] = str(omp_inspect.abduco_socket_dir())
    return env


def run_session(
    omp_argv: list[str],
    *,
    session_id: str | None = None,
    abduco_bin: str = DEFAULT_ABDUCO_BIN,
    exec_fn: Callable[[str, list[str], dict[str, str]], NoReturn] = os.execvpe,
) -> str:
    """Launch ``omp_argv`` durably. Returns the session id used (for callers that print it
    before the exec replaces the process) — ``exec_fn`` never returns on success; tests inject
    a recording stand-in instead of actually replacing the test process."""
    sid = session_id or generate_durable_id()
    command = list(omp_argv)
    if command and command[0] == "omp":
        real = read_wrapper_real_bin(default_wrapper_config_path())
        if real:
            command[0] = real
    if command and _is_managed_wrapper(shutil.which(command[0]) or command[0]):
        raise RuntimeError(
            "unresolved OMP executable is the durable wrapper; repair its real-bin configuration"
        )
    socket_root = omp_inspect.abduco_socket_dir()
    socket_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if socket_root.is_symlink() or socket_root.stat().st_uid != os.getuid():
        raise ValueError("unsafe durable socket directory")
    socket_root.chmod(0o700)
    argv = build_run_argv(command, sid, abduco_bin=abduco_bin)
    env = run_env(sid)
    exec_fn(argv[0], argv, env)
    return sid


def attach_session(
    session_id: str,
    *,
    abduco_bin: str = DEFAULT_ABDUCO_BIN,
    exec_fn: Callable[[str, list[str], dict[str, str]], NoReturn] = os.execvpe,
) -> None:
    argv = build_attach_argv(session_id, abduco_bin=abduco_bin)
    env = run_env(session_id)
    exec_fn(argv[0], argv, env)


def list_sessions(
    abduco_bin: str = DEFAULT_ABDUCO_BIN, timeout: float = 5.0
) -> tuple[list[dict], bool]:
    """``([{name, attached, terminated, created}], inspection_ok)`` — richer than
    :func:`riglib.omp_inspect.list_abduco_sessions` (keeps the attached/terminated marker +
    timestamp for ``rig omp list``'s human/JSON output)."""
    try:
        proc = subprocess.run(
            [abduco_bin, "-l"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=omp_inspect.abduco_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], False
    if proc.returncode != 0:
        return [], False
    return omp_inspect.parse_abduco_listing(proc.stdout), True


# ── omp-save: cwd -> newest session file (ported from riglib.tmux's bash renderer) ──────────
def resolve_agent_root(
    env: dict[str, str] | None = None, home: Path | None = None
) -> Path:
    """Mirrors ``riglib.harness_skills.omp_agent_root()``'s override precedence, ported into
    Python so ``rig omp save-panes`` needs no bash at all — see ``render_omp_save``'s
    docstring for the exact contract (``PI_CODING_AGENT_DIR`` full override,
    ``PI_CONFIG_DIR`` renames the ``.omp`` dirname)."""
    env = env if env is not None else os.environ
    home = home if home is not None else Path.home()

    def _expand(value: str) -> Path:
        if value.startswith("/"):
            return Path(value)
        if value == "~":
            return home
        if value.startswith("~/"):
            return home / value[2:]
        return home / value

    if env.get("PI_CODING_AGENT_DIR"):
        return _expand(env["PI_CODING_AGENT_DIR"])
    if env.get("PI_CONFIG_DIR"):
        return _expand(env["PI_CONFIG_DIR"]) / "agent"
    return home / ".omp" / "agent"


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
    plan = plan_restore(
        saved,
        live,
        durable_sockets,
        inspection_ok=live_ok and durable_ok,
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
            f"cd {shell_quote(cwd)} && {rig_command} omp attach {shell_quote(plan.argv[-1])}",
            tmux_bin,
        ):
            return RestorePlan("blocked", "pane_delivery_failed")
    elif plan.action == "resume":
        import hashlib

        durable = (
            durable_session
            or "omp-recover-" + hashlib.sha256(session_id.encode()).hexdigest()[:20]
        )
        validate_durable_id(durable)
        command = f"cd {shell_quote(cwd)} && {rig_command} omp run --session-id {shell_quote(durable)} --resume {shell_quote(plan.argv[-1])}"
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


# ── the opt-in durable-wrapper install (coordinator activates it; rig only provides it) ──────
_WRAPPER_SCRIPT = """#!/usr/bin/env bash
# rig-managed: durable OMP wrapper — installed by `rig omp install-wrapper`.
# Interactive launches are wrapped in an abduco durable session; every other invocation
# (headless -p/--print, acp, models/config/... subcommands, --help/--version) passes straight
# through to the real binary, unwrapped.
exec rig omp exec -- "$@"
"""


def wrapper_script_text(config_path: Path | None = None) -> str:
    if config_path is None:
        return _WRAPPER_SCRIPT
    return _WRAPPER_SCRIPT.replace(
        "rig omp exec --",
        "rig omp exec --config " + shell_quote(str(config_path)) + " --",
    )


def install_wrapper(target: Path, real_omp_bin: str, config_path: Path) -> None:
    """Write the wrapper script to ``target`` and record ``real_omp_bin`` at ``config_path``
    (read back by ``rig omp exec``). Never overwrites ``real_omp_bin`` itself — the vendor
    binary/symlink stays exactly where it is; ``target`` must be a DIFFERENT path earlier in
    PATH for the wrapper to actually take effect (the caller's job to arrange, per AGENTS.md:
    rig prepares on-disk artifacts, it does not mutate the user's live PATH/shell)."""
    import json, tempfile

    real = Path(real_omp_bin)
    if (
        not real.is_absolute()
        or not real.exists()
        or target.absolute() == real.absolute()
    ):
        raise ValueError(
            "real OMP must be an existing absolute path different from wrapper"
        )
    if target.is_symlink():
        raise ValueError("refusing to overwrite an unmanaged OMP symlink")
    if target.exists() and not _is_managed_wrapper(str(target)):
        raise ValueError("refusing to overwrite an unmanaged OMP executable")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    for destination, text, mode in [
        (config_path, json.dumps({"real_bin": str(real.resolve())}) + "\n", 0o600),
        (target, wrapper_script_text(config_path), 0o755),
    ]:
        fd, name = tempfile.mkstemp(
            prefix=destination.name + ".tmp-", dir=destination.parent
        )
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, destination)
        finally:
            Path(name).unlink(missing_ok=True)


def read_wrapper_real_bin(config_path: Path) -> str | None:
    import json

    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    real_bin = data.get("real_bin")
    return real_bin if isinstance(real_bin, str) and real_bin else None


def default_wrapper_config_path() -> Path:
    return Path.home() / ".config" / "rig" / "omp-wrapper.json"


def _is_managed_wrapper(path: str) -> bool:
    try:
        with Path(path).open("rb") as handle:
            return b"# rig-managed: durable OMP wrapper" in handle.read(4096)
    except OSError:
        return False


def exec_or_wrap(
    argv: list[str],
    *,
    config_path: Path | None = None,
    exec_fn: Callable[[str, list[str], dict[str, str]], NoReturn] = os.execvpe,
) -> None:
    """``rig omp exec -- ARGS...``: the wrapper's actual body. Classifies ``argv`` with
    :func:`riglib.omp_recovery.is_interactive_omp`; wraps interactive launches durably,
    execs everything else straight through, unwrapped."""
    from .omp_recovery import is_interactive_omp

    config_path = config_path or default_wrapper_config_path()
    real_bin = read_wrapper_real_bin(config_path)
    if real_bin is None:
        raise RuntimeError(
            f"no real OMP binary recorded at {config_path} — run `rig omp install-wrapper` first"
        )
    if _is_managed_wrapper(real_bin):
        raise RuntimeError("real OMP executable points back to the managed wrapper")
    command = shlex.join([real_bin, *argv])
    if not is_interactive_omp(command):
        exec_fn(real_bin, [real_bin, *argv], dict(os.environ))
        return
    run_session([real_bin, *argv], exec_fn=exec_fn)
