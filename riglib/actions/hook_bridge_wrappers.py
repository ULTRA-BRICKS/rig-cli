"""Wrapper/symlink helpers for the opencode and omp hook-bridge plugins."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from ..config import (
    OPENCODE_HOOK_BRIDGE_EXCLUDE_BEGIN_MARKER,
    OPENCODE_HOOK_BRIDGE_EXCLUDE_COMMENT,
    OPENCODE_HOOK_BRIDGE_EXCLUDE_END_MARKER,
)
from ..paths import expand_user_path
from ..plan import Action
from .git_exclude import _atomic_write_exclude, _find_marker_lines, repo_info_exclude_path
from .hook_bridge import (
    _LEGACY_OPENCODE_PLUGIN_NAME,
    hook_bridge_module,
    hook_bridge_settings_file,
    opencode_hook_bridge_plugin_target,
)
from .skills import _same_link_dest


def omp_hook_bridge_extension_target(action: Action) -> tuple[Path, Path]:
    """Return the omp extension symlink path and the bridge extension it should target."""
    extension_path = hook_bridge_settings_file(action)
    module = hook_bridge_module(action)
    dest = Path(str(action.options["lib_dir"])) / module / "extension.ts"
    return extension_path, dest


def omp_hook_bridge_uses_wrapper(action: Action) -> bool:
    """True when rig must write an extension wrapper instead of a plain symlink.

    Mirrors :func:`opencode_hook_bridge_uses_wrapper` for a custom descriptor dir: omp's
    dispatcher (like opencode's) reads its descriptor dir from a process-level env var
    (``OMP_HOOKS_DIR``) rather than a CLI flag, so a non-default target needs a tiny wrapper
    module that sets the env var before delegating to the real extension — a plain symlink
    cannot carry that. ALSO true when ``hook_bridge.python`` names a custom interpreter: the
    extension itself reads ``OMP_HOOK_BRIDGE_PYTHON`` (falling back to a bare ``python3``) to
    decide which interpreter runs the dispatcher subprocess — with a plain symlink that option
    would be silently ignored (rig would accept the config key but never wire it anywhere).
    """
    return bool(action.options.get("hooks_dir")) or bool(action.options.get("python"))


_OMP_WRAPPER_HEADER = "// rig-managed omp hook bridge wrapper. Do not edit.\n"


def omp_hook_bridge_wrapper_text(action: Action) -> str:
    """The managed omp wrapper used when descriptors live outside the default hook dir and/or
    a custom interpreter is configured — either alone, or both together."""
    _extension_path, dest = omp_hook_bridge_extension_target(action)
    hooks_dir = action.options.get("hooks_dir")
    python = action.options.get("python")
    if not hooks_dir and not python:
        raise AssertionError("omp wrapper requires hooks_dir and/or python; guard with uses_wrapper")
    env_lines = []
    if hooks_dir:
        env_lines.append(f"process.env.OMP_HOOKS_DIR = {json.dumps(str(hooks_dir))};\n")
    if python:
        env_lines.append(f"process.env.OMP_HOOK_BRIDGE_PYTHON = {json.dumps(str(python))};\n")
    return (
        _OMP_WRAPPER_HEADER
        + "".join(env_lines)
        + f"const bridgeModule = await import({json.dumps(dest.resolve().as_uri())});\n"
        "export default bridgeModule.default;\n"
    )


_OMP_WRAPPER_FOOTER = "export default bridgeModule.default;\n"


def _omp_wrapper_text_is_rig_managed(text: str) -> bool:
    """True when ``text`` has the exact shape a rig-generated omp wrapper has: both the header
    AND the trailing default-export line, not merely a first line that happens to match the
    (documented, hence copyable) header comment. A foreign hand-written extension could
    plausibly start with that one comment line — requiring the WHOLE template shape (both
    anchors) before treating a file as "ours to replace unconditionally, bypassing
    on_conflict" makes misidentification substantially harder than a one-line substring match
    (review-cli finding, rig-cli#342). Still not cryptographic provenance — a foreign file could
    deliberately clone both lines — but this repo's other generated-file provenance checks
    (e.g. the omp permissions guard's version marker) are similarly heuristic, not signed."""
    return text.startswith(_OMP_WRAPPER_HEADER) and text.endswith(_OMP_WRAPPER_FOOTER)


def _omp_wrapper_file_is_rig_managed(path: Path) -> bool:
    """True when ``path`` is a REGULAR file (not a symlink) whose content matches the shape a
    rig-generated omp wrapper has — i.e. an earlier ``rig apply`` wrote it, so it is safe to
    replace unconditionally regardless of ``on_conflict``, exactly like a rig-owned symlink.
    Read/decode failures are NOT treated as rig-managed — fail safe, never clobber content we
    could not actually verify is ours. The file is always small (a handful of lines), so a full
    read is cheap; see :func:`_omp_wrapper_text_is_rig_managed` for the shape it checks."""
    if path.is_symlink() or not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return _omp_wrapper_text_is_rig_managed(text)


def _omp_symlink_is_rig_managed(link_path: Path, dest: Path) -> bool:
    """True when ``link_path`` is a symlink that DEMONSTRABLY targets the omp bridge — either the
    current ``dest`` (``extension.ts`` in this checkout) or a prior agent-tools checkout's
    ``lib/omp_hook_bridge/extension.ts`` (the checkout moved; the link is stale but ours).

    Only such a link may bypass ``on_conflict``: a symlink a USER created at the managed path
    pointing at some other extension of their own is foreign configuration, and replacing it
    without honoring ``skip``/``backup`` would silently destroy it with no restore path —
    exactly what AGENTS.md's "anything replaced is backed up per on_conflict" rule forbids
    (PR #352 review thread). Mirrors :func:`_looks_like_agent_tools_opencode_bridge`. A
    dangling link is judged by its stored target text (no resolve needed), so a link into a
    deleted old checkout still reads as ours. Read failures → not ours (fail safe).
    """
    if not link_path.is_symlink():
        return False
    try:
        current = link_path.readlink()
    except OSError:
        return False
    if _same_link_dest(link_path, current, dest):
        return True
    parts = _link_target_path(link_path, current).parts
    return len(parts) >= 3 and parts[-3:] == ("lib", "omp_hook_bridge", "extension.ts")


def opencode_hook_bridge_uses_wrapper(action: Action) -> bool:
    """True when rig must write a plugin wrapper to pass a custom descriptor dir."""
    return bool(action.options.get("hooks_dir"))


def opencode_hook_bridge_wrapper_text(action: Action) -> str:
    """The managed opencode wrapper used when descriptors live outside the default hook dir."""
    _plugin_path, dest = opencode_hook_bridge_plugin_target(action)
    hooks_dir = action.options.get("hooks_dir")
    if not hooks_dir:
        raise AssertionError("opencode wrapper requires hooks_dir; guard with uses_wrapper")
    return (
        "// rig-managed opencode hook bridge wrapper. Do not edit.\n"
        f"process.env.OPENCODE_HOOKS_DIR = {json.dumps(str(hooks_dir))};\n"
        f"const bridgeModule = await import({json.dumps(dest.resolve().as_uri())});\n"
        "export const AgentToolsHookBridge = bridgeModule.AgentToolsHookBridge;\n"
    )


def opencode_hook_bridge_exclude_block_text(rel_path: str) -> str:
    """The marker-delimited block rig owns for the repo-local opencode plugin symlink."""
    rel_path = rel_path.lstrip("/")
    return "\n".join(
        [
            OPENCODE_HOOK_BRIDGE_EXCLUDE_BEGIN_MARKER,
            OPENCODE_HOOK_BRIDGE_EXCLUDE_COMMENT,
            f"/{rel_path}",
            f"/{rel_path}.rig-bak-*",
            OPENCODE_HOOK_BRIDGE_EXCLUDE_END_MARKER,
        ]
    )


def _path_for_git_probe(path: Path) -> Path:
    probe = path if path.is_dir() else path.parent
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return probe


def _repo_root_for_path(path: Path) -> Path | None:
    probe = _path_for_git_probe(path)
    try:
        res = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    root = res.stdout.strip()
    return Path(root) if root else None


def _opencode_bridge_exclude_context(plugin_path: Path) -> tuple[Path, str] | None:
    repo_root = _repo_root_for_path(plugin_path)
    if repo_root is None:
        return None
    try:
        abs_plugin_path = Path(os.path.abspath(plugin_path))
        normalized_plugin_path = abs_plugin_path.parent.resolve(strict=False) / abs_plugin_path.name
        rel_path = str(normalized_plugin_path.relative_to(repo_root.resolve(strict=False)))
    except ValueError:
        return None
    exclude_path = repo_info_exclude_path(repo_root)
    if exclude_path is None:
        return None
    return exclude_path, rel_path


def _opencode_exclude_has_entry(exclude_path: Path | None, rel_path: str) -> bool:
    if exclude_path is None:
        return True
    if not exclude_path.is_file():
        return False
    try:
        with exclude_path.open(encoding="utf-8", newline="") as fh:
            content = fh.read()
    except OSError:
        return False
    begins = _find_marker_lines(content, OPENCODE_HOOK_BRIDGE_EXCLUDE_BEGIN_MARKER)
    ends = _find_marker_lines(content, OPENCODE_HOOK_BRIDGE_EXCLUDE_END_MARKER)
    if len(begins) != 1 or len(ends) != 1:
        return False
    b_start, _b_end = begins[0]
    e_start, e_end = ends[0]
    if e_start < b_start:
        return False
    return content[b_start:e_end] == opencode_hook_bridge_exclude_block_text(rel_path)


_ExcludeOutcome = tuple[bool, bool, str]


def _append_exclude_block(content: str, desired: str) -> str:
    if not content:
        return f"{desired}\n"
    lead = content if content.endswith("\n") else content + "\n"
    return f"{lead}{desired}\n"


def _exclude_marker_pairs(
    begins: list[tuple[int, int]], ends: list[tuple[int, int]]
) -> list[tuple[int, int]] | None:
    """(begin start, end line end) per managed block; None when markers are not strictly
    begin/end alternating."""
    markers = sorted(
        [(b[0], b[1], "begin") for b in begins] + [(e[0], e[1], "end") for e in ends]
    )
    pairs: list[tuple[int, int]] = []
    expect = "begin"
    pending = -1
    for start, line_end, kind in markers:
        if kind != expect:
            return None
        if kind == "begin":
            pending = start
            expect = "end"
        else:
            pairs.append((pending, line_end))
            expect = "begin"
    return pairs


def _rewrite_exclude_blocks(content: str, pairs: list[tuple[int, int]], desired: str) -> str:
    """Replace the first managed block with ``desired`` and drop any duplicate blocks."""
    out: list[str] = []
    cursor = 0
    for idx, (r_start, r_end) in enumerate(pairs):
        out.append(content[cursor:r_start])
        if idx == 0:
            out.append(desired)
        elif content[r_end : r_end + 1] == "\n":
            r_end += 1
        cursor = r_end
    out.append(content[cursor:])
    return "".join(out)


def _plan_exclude_content(
    content: str, desired: str, exclude_path: Path
) -> tuple[str | None, _ExcludeOutcome | None]:
    """(new exclude-file content, None) to write, or (None, outcome) when nothing is to be written."""
    begins = _find_marker_lines(content, OPENCODE_HOOK_BRIDGE_EXCLUDE_BEGIN_MARKER)
    ends = _find_marker_lines(content, OPENCODE_HOOK_BRIDGE_EXCLUDE_END_MARKER)
    if len(begins) != len(ends):
        return None, (False, False, f"{exclude_path} has unbalanced rig opencode hook bridge markers — reconcile by hand")
    if not begins:
        return _append_exclude_block(content, desired), None
    pairs = _exclude_marker_pairs(begins, ends)
    if pairs is None:
        return None, (False, False, f"{exclude_path} has misordered rig opencode hook bridge markers — reconcile by hand")
    if len(pairs) == 1 and content[pairs[0][0] : pairs[0][1]] == desired:
        return None, (True, False, f"already ignored in {exclude_path}")
    return _rewrite_exclude_blocks(content, pairs, desired), None


def _reconcile_opencode_bridge_exclude(plugin_path: Path) -> tuple[bool, bool, str]:
    ctx = _opencode_bridge_exclude_context(plugin_path)
    if ctx is None:
        return True, False, ""
    exclude_path, rel_path = ctx
    desired = opencode_hook_bridge_exclude_block_text(rel_path)
    if not exclude_path.exists():
        try:
            exclude_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_exclude(exclude_path, desired + "\n")
        except OSError as exc:
            return False, False, f"could not write {exclude_path}: {exc}"
        return True, True, f"ignored in {exclude_path}"
    try:
        with exclude_path.open(encoding="utf-8", newline="") as fh:
            content = fh.read()
    except OSError as exc:
        return False, False, f"could not read {exclude_path}: {exc}"
    new_content, outcome = _plan_exclude_content(content, desired, exclude_path)
    if outcome is not None:
        return outcome
    try:
        _atomic_write_exclude(exclude_path, new_content)
    except OSError as exc:
        return False, False, f"could not write {exclude_path}: {exc}"
    return True, True, f"ignored in {exclude_path}"


def _normalized_path_without_following_leaf(path: Path) -> Path:
    abs_path = Path(os.path.abspath(path))
    return abs_path.parent.resolve(strict=False) / abs_path.name


def legacy_opencode_bridge_symlink_path() -> Path:
    return expand_user_path(f"~/.config/opencode/plugins/{_LEGACY_OPENCODE_PLUGIN_NAME}")


def _link_target_path(link_path: Path, target: Path) -> Path:
    return target if target.is_absolute() else link_path.parent / target


def _looks_like_agent_tools_opencode_bridge(link_path: Path, target: Path) -> bool:
    parts = _link_target_path(link_path, target).parts
    return len(parts) >= 3 and parts[-3:] == ("lib", "opencode_hook_bridge", "plugin.js")


def legacy_opencode_bridge_needs_cleanup(plugin_path: Path, dest: Path) -> bool:
    legacy = legacy_opencode_bridge_symlink_path()
    if _normalized_path_without_following_leaf(legacy) == _normalized_path_without_following_leaf(plugin_path):
        return False
    if not legacy.is_symlink():
        return False
    try:
        current = legacy.readlink()
    except OSError:
        return False
    return _same_link_dest(legacy, current, dest) or _looks_like_agent_tools_opencode_bridge(legacy, current)


def _remove_legacy_opencode_bridge_symlink(plugin_path: Path, dest: Path) -> tuple[bool, str]:
    legacy = legacy_opencode_bridge_symlink_path()
    if not legacy_opencode_bridge_needs_cleanup(plugin_path, dest):
        return False, ""
    try:
        legacy.unlink()
    except OSError as exc:
        return False, f"legacy global opencode plugin still present (could not remove {legacy}: {exc})"
    return True, f"removed legacy global opencode plugin {legacy}"
