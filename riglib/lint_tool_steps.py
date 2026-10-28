"""Apply handler for the bootstrap steps that install tools OUTSIDE the repository's manifests.

Context: ``lint_bootstrap_step`` actions that carry a ``cache_dir`` key are routed here by
:mod:`riglib.actions.lint_policy_actions`. Several shapes, all written by :mod:`riglib.lint_bootstrap`
and none touching a file inside the repository (so they are allowed under CI, unlike manifest
edits):

- **argv** into the Rig tool cache: ``python -m venv``, ``pip install --only-binary :all:`` and
  ``npm install --prefix ... --ignore-scripts`` (``options.argv``);
- **copy_tree** / **write_content**: copy a catalog directory verbatim into the cache / write a
  rendered file (the cache-hosted Oxlint policy, :mod:`riglib.lint_policy_cache`);
- **release**: download a pinned asset, verify its sha256 and place the binary in the cache
  (``options.release = [url, sha256, kind]``, see :mod:`riglib.lint_release_tools`);
- **rustup components**: ``rustup component add clippy rustfmt`` for the toolchain that applies in
  the project directory (``options.component``). It never installs or switches a toolchain, so the
  project's Rust version is untouched; present components make it a no-op.

Trust boundary: the planner produces every argv/URL/path. The handler still refuses a ``cache_dir``
outside the Rig cache root, a rustup argv other than the one fixed command, and a project directory
that escapes the repo or crosses a symlink, because it is the place that spawns processes.
Stdlib-only at import time.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from .lint_release_tools import ReleaseError, fetch_verified_binary
from .lint_tool_cache import cache_root

STEP_TIMEOUT_SECONDS = 600
PROBE_TIMEOUT_SECONDS = 60
RUSTUP_ARGV = ("rustup", "component", "add", "clippy", "rustfmt")
_RUST_PROBES = (("cargo", "clippy", "--version"), ("cargo", "fmt", "--version"))
_STDERR_TAIL = 600


def _inside(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True


def _strings(raw: object) -> list[str] | None:
    if isinstance(raw, (list, tuple)) and raw and all(isinstance(a, str) and a for a in raw):
        return [str(a) for a in raw]
    return None


def _spawn(argv: list[str], cwd: Path, timeout: int, extra_env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """``(ok, detail)``; a missing binary, timeout or non-zero exit is a failure with a readable tail."""
    try:
        proc = subprocess.run(
            argv, cwd=cwd, shell=False, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout, check=False,
            env={**os.environ, **extra_env} if extra_env else None,
        )
    except FileNotFoundError:
        return False, f"{argv[0]} is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return False, f"{' '.join(argv)} timed out after {timeout}s"
    except OSError as exc:
        return False, f"cannot run {argv[0]}: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-_STDERR_TAIL:]
        return False, f"{' '.join(argv)} exited {proc.returncode}: {tail}"
    return True, ""


def _cache_paths(opts: Mapping[str, object]) -> tuple[Path, Path] | None:
    directory, creates = opts.get("cache_dir"), opts.get("creates")
    if not (isinstance(directory, str) and directory and isinstance(creates, str) and creates):
        return None
    return Path(directory), Path(creates)


def _install_release(opts: Mapping[str, object], directory: Path, creates: Path) -> tuple[str, str]:
    """``(status, detail)`` for a verified release-binary install."""
    release = _strings(opts.get("release"))
    if release is None or len(release) != 3:
        return "error", "malformed release (url, sha256, kind)"
    url, sha256, kind = release
    try:
        binary = fetch_verified_binary(url, sha256, kind, creates.name)
    except ReleaseError as exc:
        return "error", str(exc)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        staging = creates.with_name(creates.name + ".partial")
        staging.write_bytes(binary)
        staging.chmod(0o755)
        os.replace(staging, creates)
    except OSError as exc:
        return "error", f"cannot place {creates}: {exc}"
    return "created", f"installed {creates.name} from the pinned release {url}"


def _install_copy(opts: Mapping[str, object], directory: Path, creates: Path) -> tuple[str, str]:
    """Copy a catalog directory verbatim into the cache: regular files only, staged then renamed into place."""
    from .linter_carriers import _tree_snapshot

    pair = _strings(opts.get("copy_tree"))
    if pair is None or len(pair) != 2:
        return "error", "malformed copy_tree (source, destination)"
    source, destination = Path(pair[0]), Path(pair[1])
    if not _inside(destination, directory):
        return "error", f"{destination} is outside {directory}; refusing"
    try:
        files = _tree_snapshot(source)  # rejects symlinks and non-regular entries
        staging = destination.with_name(destination.name + ".partial")
        for rel, data in files.items():
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if destination.exists():
            return "error", f"{destination} exists without {creates.name}; remove it to repair the cache"
        os.replace(staging, destination)
    except (OSError, ValueError) as exc:
        return "error", f"cannot copy {source}: {exc}"
    return "created", f"copied {source.name} ({len(files)} files) into the tool cache"


def _install_content(opts: Mapping[str, object], directory: Path, creates: Path) -> tuple[str, str]:
    content = opts.get("write_content")
    if not isinstance(content, str) or not content:
        return "error", "malformed write_content"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        staging = creates.with_name(creates.name + ".partial")
        staging.write_text(content, encoding="utf-8")
        os.replace(staging, creates)
    except OSError as exc:
        return "error", f"cannot write {creates}: {exc}"
    return "created", f"wrote {creates.name}"


def _install_argv(opts: Mapping[str, object], directory: Path, creates: Path) -> tuple[str, str]:
    argv = _strings(opts.get("argv"))
    if argv is None:
        return "error", "malformed tool-cache action (argv)"
    run_dir = directory.parent
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return "error", f"cannot create {run_dir}: {exc}"
    ok, detail = _spawn(argv, run_dir, STEP_TIMEOUT_SECONDS)
    if not ok:
        return "error", detail
    if not creates.exists():
        return "error", f"{argv[0]} succeeded but {creates} was not created"
    return "created", f"ran {' '.join(argv)}"


def _tool_cache_step(opts: Mapping[str, object]) -> tuple[str, str]:
    paths = _cache_paths(opts)
    if paths is None:
        return "error", "malformed tool-cache action (cache_dir/creates)"
    directory, creates = paths
    if not _inside(directory, cache_root(os.environ)) or not _inside(creates, directory):
        return "error", f"{directory} is outside the Rig tool cache; refusing"
    if creates.exists():
        return "skipped", f"{creates.name} already cached"
    shapes = (("release", _install_release), ("copy_tree", _install_copy), ("write_content", _install_content))
    install: Callable[[Mapping[str, object], Path, Path], tuple[str, str]] = next(
        (fn for key, fn in shapes if key in opts), _install_argv
    )
    return install(opts, directory, creates)


def _project_dir(action, cwd_rel: object) -> Path | str:
    """The validated project directory, or an error string (escape / symlink / not a directory)."""
    from .actions.linter_config import _unsafe_path_component
    from .config import linter_path_escapes_repo

    if not isinstance(cwd_rel, str):
        return "malformed rustup action (cwd)"
    root = Path(action.target)
    if cwd_rel != "." and (linter_path_escapes_repo(cwd_rel) or _unsafe_path_component(root, cwd_rel) is not None):
        return f"cwd {cwd_rel!r} escapes the repo or crosses a symlink"
    cwd = root if cwd_rel == "." else root / cwd_rel
    return cwd if cwd.is_dir() else f"cwd {cwd} is not a directory"


_NO_AUTO_INSTALL = {"RUSTUP_AUTO_INSTALL": "0"}


def _components_present(cwd: Path) -> bool:
    # The probes run through rustup's `cargo` proxy too: without the same switch a repo-pinned,
    # not-installed toolchain would be downloaded by the mere probe.
    return all(_spawn(list(argv), cwd, PROBE_TIMEOUT_SECONDS, _NO_AUTO_INSTALL)[0] for argv in _RUST_PROBES)


def _rustup_step(action) -> tuple[str, str]:
    opts = action.options
    if _strings(opts.get("argv")) != list(RUSTUP_ARGV):
        return "error", "refused: only `rustup component add clippy rustfmt` is allowed"
    cwd = _project_dir(action, opts.get("cwd"))
    if isinstance(cwd, str):
        return "error", cwd
    if _components_present(cwd):
        return "skipped", "clippy and rustfmt are already installed for this toolchain"
    # A committed rust-toolchain.toml must never make rustup download a toolchain: only components of
    # the toolchain that is already installed are added.
    ok, detail = _spawn(list(RUSTUP_ARGV), cwd, STEP_TIMEOUT_SECONDS, _NO_AUTO_INSTALL)
    if not ok:
        if "not installed" in detail:
            detail += " -- Rig never installs a toolchain for you: run `rustup toolchain install` for the pinned channel yourself"
        return "error", detail
    if not _components_present(cwd):
        return "error", "rustup succeeded but `cargo clippy` / `cargo fmt` still do not run for this toolchain"
    return "updated", "added the clippy and rustfmt components to the current toolchain"


def run_git_exclude_step(action) -> tuple[str, str]:
    """Add the install directories to the Rig-owned block of ``.git/info/exclude`` (idempotent, append-only).

    The caller has already refused CI / a missing mutation grant. Every entry must be an anchored
    ``/<path>/node_modules/`` pattern; any other line of the exclude file is preserved verbatim.
    """
    from . import lint_ignore
    from .actions.git_exclude import reconcile_exclude_block, repo_info_exclude_path

    entries = _strings(action.options.get("git_exclude"))
    if entries is None or not all(lint_ignore.valid_entry(e) for e in entries):
        return "error", "malformed git_exclude (only anchored /<path>/node_modules/ entries are allowed)"
    path = repo_info_exclude_path(Path(action.target))
    if path is None:
        return "error", "not a git repository: nowhere to record the dependency ignore"
    try:
        content = path.read_text(encoding="utf-8") if path.is_file() else ""
    except (OSError, UnicodeDecodeError) as exc:
        return "error", f"cannot read {path}: {exc}"
    desired = lint_ignore.block_text(lint_ignore.merged_entries(content, entries))
    ok, outcome = reconcile_exclude_block(path, desired, lint_ignore.BEGIN_MARKER, lint_ignore.END_MARKER, "lint dependencies")
    if not ok:
        return "error", outcome
    return ("skipped", f"{', '.join(entries)} already ignored in {path}") if outcome == "already" else (
        "updated", f"ignored {', '.join(entries)} in {path} (local .git/info/exclude; not committed)"
    )


def run_tool_step(action):
    """Run one out-of-manifest bootstrap step (cache install, release download, rustup components); idempotent."""
    from .actions.runner import ActionResult

    opts = action.options
    step = str(opts.get("step") or action.item)
    if opts.get("allow_mutation") is not True:
        status, detail = "error", "refused: the action does not carry allow_mutation=true"
    elif "component" in opts:
        status, detail = _rustup_step(action)
    else:
        status, detail = _tool_cache_step(opts)
    return ActionResult(action, status, f"lint-bootstrap ({step}): {detail}")
