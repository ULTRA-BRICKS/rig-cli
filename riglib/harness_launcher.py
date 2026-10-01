"""``rig harness run|attach|list|exec|install-wrapper`` — host any harness in an abduco session.

A hosted session is ``abduco server → rig harness host <name> → harness`` (see
:mod:`riglib.harness_host`): the crash supervisor lives INSIDE the session so the pty, the socket
and any attached client survive a relaunch, and tg-ctl can type into the pty from outside tmux.
abduco is a small established session manager, not a new pty implementation (AGENTS.md). The
session name is ``<harness>-<12 hex>`` — never the cwd, because one cwd legitimately hosts several
sessions — and is stable across relaunches: it is the delivery address.

``run``/``attach`` REPLACE the current process (``os.execvpe``) so the terminal is handed straight
to abduco; they are meant to be the last thing a pane runs. ``--detach`` creates the session
without attaching (scripts, smoke tests).

The harness command line travels in ``RIG_HOST_SPEC`` — abduco's and the host's argv name only
``rig harness host <name>``. tg-ctl identifies agents by argv (``claude …``, ``…/codex``), so an
argv carrying the harness would make the abduco server look like the agent.

The opt-in wrapper scripts (:func:`install_wrapper`) shadow a harness on PATH: an interactive
launch is hosted (:func:`exec_or_wrap`), everything else — ``-p``, ``exec``, subcommands, ``--help``
— execs the real binary untouched. Where the real binary lives is recorded per harness in
``harness-real-bin.json`` because it is not always the first match on PATH (the omp fork checkout).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Callable, NoReturn

from . import omp_inspect
from .harness_host import ENV_SPEC, HostSpec, host_command, hosts_dir, rig_entry, validate_name
from .harness_kinds import KINDS, is_interactive, prepare_launch, validate_kind

DEFAULT_ABDUCO_BIN = "abduco"
WRAPPER_MARKER = "# rig-managed: harness wrapper"
#: the marker of the omp-only wrapper this launcher replaces. A script carrying it is rig's own
#: stale output: skipped when resolving the real binary and replaced by `install_wrapper`, so
#: the cutover never leaves a wrapper calling the deleted `rig omp exec`.
SUPERSEDED_WRAPPER_MARKERS = ("# rig-managed: durable OMP wrapper",)

ExecFn = Callable[[str, list[str], dict[str, str]], NoReturn]


def generate_host_id(kind: str) -> str:
    return f"{validate_kind(kind)}-{uuid.uuid4().hex[:12]}"


# ── the command abduco runs ──────────────────────────────────────────────────────────────────
def build_run_argv(name: str, *, abduco_bin: str = DEFAULT_ABDUCO_BIN, detach: bool = False) -> list[str]:
    """``abduco -f -A <name> <host>`` (connect-or-create) or ``abduco -f -n <name> <host>``
    (create only). ``-f`` replaces an already terminated session of that name; an active one is
    still attached, so a duplicate ``run`` with the SAME name (a restore racing a still-starting
    launch) reuses the existing process instead of starting a second writer."""
    return [abduco_bin, "-f", "-n" if detach else "-A", validate_name(name), *host_command(name)]


def build_attach_argv(name: str, *, abduco_bin: str = DEFAULT_ABDUCO_BIN) -> list[str]:
    return [abduco_bin, "-a", validate_name(name)]


def run_env(spec: HostSpec | None = None, *, base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base_env if base_env is not None else os.environ)
    env["ABDUCO_SOCKET_DIR"] = str(omp_inspect.abduco_socket_dir())
    if spec is not None:
        env[ENV_SPEC] = spec.to_env()
    return env


# ── which binary is "the real one" ───────────────────────────────────────────────────────────
def real_bin_config_path(env: dict[str, str] | None = None) -> Path:
    env = env if env is not None else os.environ
    root = Path(env["RIG_CONFIG_DIR"]) if env.get("RIG_CONFIG_DIR") else Path.home() / ".config" / "rig"
    return root / "harness-real-bin.json"


def read_real_bins(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in KINDS and isinstance(v, str) and v}


def is_managed_wrapper(path: str) -> bool:
    try:
        with Path(path).open("rb") as handle:
            head = handle.read(4096)
        return any(marker.encode() in head for marker in (WRAPPER_MARKER, *SUPERSEDED_WRAPPER_MARKERS))
    except OSError:
        return False


def resolve_real_bin(kind: str, env: dict[str, str] | None = None) -> str:
    """The recorded real binary for ``kind``, else the first executable of that name on PATH that
    is not one of our wrappers."""
    env = env if env is not None else os.environ
    validate_kind(kind)
    recorded = read_real_bins(real_bin_config_path(env)).get(kind)
    if recorded:
        if is_managed_wrapper(recorded):
            raise RuntimeError(f"the recorded real {kind} binary {recorded} is a rig wrapper; repair harness-real-bin.json")
        return recorded
    for directory in env.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory or ".", kind)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK) and not is_managed_wrapper(candidate):
            return candidate
    raise RuntimeError(f"no real {kind} executable found on PATH (wrappers are skipped)")


# ── run / attach / list ──────────────────────────────────────────────────────────────────────
def _ensure_socket_dir() -> None:
    socket_root = omp_inspect.abduco_socket_dir()
    socket_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if socket_root.is_symlink() or socket_root.stat().st_uid != os.getuid():
        raise ValueError("unsafe durable socket directory")
    socket_root.chmod(0o700)


def build_spec(kind: str, args: list[str], *, real_bin: str, label: str | None, cwd: str) -> HostSpec:
    argv, session = prepare_launch(kind, [real_bin, *args])
    return HostSpec(kind, argv, cwd, label, session)


def run_session(
    kind: str,
    args: list[str],
    *,
    label: str | None = None,
    host_id: str | None = None,
    detach: bool = False,
    real_bin: str | None = None,
    cwd: str | None = None,
    abduco_bin: str = DEFAULT_ABDUCO_BIN,
    exec_fn: ExecFn = os.execvpe,
    run_fn: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Host ``<kind> *args`` in abduco. Returns the session name; with the default ``exec_fn``
    an attaching run never returns (tests inject a recording stand-in)."""
    validate_kind(kind)
    name = validate_name(host_id) if host_id else generate_host_id(kind)
    if not name.startswith(f"{kind}-"):
        raise ValueError(f"host name {name!r} must start with '{kind}-' (pane recognition reads the harness from it)")
    real = real_bin or resolve_real_bin(kind)
    if is_managed_wrapper(shutil.which(real) or real):
        raise RuntimeError(f"the {kind} executable {real} is the rig wrapper; repair its real-bin configuration")
    spec = build_spec(kind, args, real_bin=real, label=label, cwd=cwd or os.getcwd())
    _ensure_socket_dir()
    argv = build_run_argv(name, abduco_bin=abduco_bin, detach=detach)
    env = run_env(spec)
    if detach:
        done = run_fn(argv, env=env, stdin=subprocess.DEVNULL, check=False)
        if done.returncode != 0:
            raise RuntimeError(f"abduco could not create session {name} (exit {done.returncode})")
    else:
        exec_fn(argv[0], argv, env)
    return name


def attach_session(name: str, *, abduco_bin: str = DEFAULT_ABDUCO_BIN, exec_fn: ExecFn = os.execvpe) -> None:
    argv = build_attach_argv(name, abduco_bin=abduco_bin)
    exec_fn(argv[0], argv, run_env())


def list_sessions(abduco_bin: str = DEFAULT_ABDUCO_BIN, timeout: float = 5.0) -> tuple[list[dict], bool]:
    """``([{name, attached, terminated, created, harness?, label?, cwd?}], inspection_ok)`` —
    abduco's sessions joined with their tg-ctl registration records."""
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
    records = hosts_dir(dict(os.environ))
    sessions = omp_inspect.parse_abduco_listing(proc.stdout)
    for session in sessions:
        try:
            record = json.loads((records / f"{session['name']}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for key in ("harness", "label", "cwd"):
            if isinstance(record, dict) and record.get(key):
                session[key] = record[key]
    return sessions, True


# ── the opt-in wrapper on PATH ───────────────────────────────────────────────────────────────
def wrapper_text(kind: str) -> str:
    validate_kind(kind)
    body = shlex.join([*rig_entry(), "harness", "exec", kind, "--"])
    return (
        "#!/usr/bin/env bash\n"
        f"{WRAPPER_MARKER} ({kind}) — written by `rig apply` / `rig harness install-wrapper`.\n"
        "# Interactive launches run in an abduco session under the rig crash supervisor and are\n"
        "# registered with tg-ctl; every other invocation (-p/--print, exec, subcommands, --help)\n"
        f"# execs the real {kind} binary untouched.\n"
        "# Fallbacks only (python, bun, abduco) are appended: the caller's PATH order still decides\n"
        "# which real binary runs.\n"
        'export PATH="${PATH:-}:$HOME/.bun/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin"\n'
        f'exec {body} "$@"\n'
    )


def wrapper_state(kind: str, target: Path) -> str:
    """``missing`` | ``current`` | ``stale`` (ours, different text) | ``foreign`` (not ours)."""
    if not target.exists() and not target.is_symlink():
        return "missing"
    if target.is_symlink() or not is_managed_wrapper(str(target)):
        return "foreign"
    try:
        return "current" if target.read_text(encoding="utf-8") == wrapper_text(kind) else "stale"
    except (OSError, UnicodeError):
        return "stale"


def install_wrapper(kind: str, target: Path, *, real_bin: str | None = None, env: dict[str, str] | None = None) -> bool:
    """Write the wrapper for ``kind`` to ``target`` (and record ``real_bin``); ``True`` when
    anything changed. Never overwrites an unmanaged file or symlink, never the real binary."""
    validate_kind(kind)
    if real_bin is not None:
        real = Path(real_bin)
        if not real.is_absolute() or not real.exists() or real.absolute() == target.absolute():
            raise ValueError("real binary must be an existing absolute path different from the wrapper")
        if is_managed_wrapper(str(real)):
            raise ValueError("real binary must not be a rig wrapper")
    state = wrapper_state(kind, target)
    if state == "foreign":
        raise ValueError(f"refusing to overwrite an unmanaged {kind} executable or symlink at {target}")
    changed = False
    if real_bin is not None:
        config = real_bin_config_path(env)
        bins = read_real_bins(config)
        if bins.get(kind) != str(Path(real_bin).resolve()):
            bins[kind] = str(Path(real_bin).resolve())
            _atomic_write(config, json.dumps(bins, indent=2, sort_keys=True) + "\n", 0o600)
            changed = True
    if state != "current":
        _atomic_write(target, wrapper_text(kind), 0o755)
        changed = True
    return changed


def _atomic_write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".tmp-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def exec_or_wrap(
    kind: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    interactive_tty: bool | None = None,
    exec_fn: ExecFn = os.execvpe,
) -> None:
    """``rig harness exec <kind> -- ARGS...``: the installed wrapper's body. Hosts an interactive
    launch on a terminal; execs everything else (headless, subcommands, no tty) straight through."""
    env = env if env is not None else dict(os.environ)
    real = resolve_real_bin(kind, env)
    if interactive_tty is None:
        interactive_tty = sys.stdin.isatty() and sys.stdout.isatty()
    if not (interactive_tty and is_interactive(kind, args)):
        exec_fn(real, [real, *args], env)
        return
    run_session(kind, args, real_bin=real, exec_fn=exec_fn)
