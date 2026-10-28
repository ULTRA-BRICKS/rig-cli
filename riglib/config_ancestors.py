"""Ancestor ``rig.yaml`` discovery and the ``task``-block inheritance overlay (rig-cli UB-23).

Why a separate module: a folder-level ``rig.yaml`` above a repo contributes ONLY its ``task``
routing block, under its own trust rules (ownership, no group/world write) and error wording. That
policy is independent of the global/repo layer cascade in ``config_load``, which merely asks for the
overlay and folds it back in.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from .config_errors import ConfigError
from .config_paths import repo_config_path
from .config_validate_task import validate_task
from .config_yaml import deep_merge, read_yaml_file
from .logging import get_logger


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def task_inheritance_ancestor_dirs(repo_root: Path) -> list[Path]:
    """Directories eligible for ``task``-block inheritance, OUTERMOST first (rig-cli UB-23).

    ``repo_root`` itself is excluded -- only its ancestors. Inside ``$HOME``, walks up to and
    including ``$HOME`` and never above it (the intended ``~/xp``/``~/work`` folder-default
    shape). Outside ``$HOME``, walks every ancestor except the filesystem root itself.
    """
    repo_root = repo_root.resolve()
    home = Path(os.path.expanduser("~")).resolve()
    if repo_root == home:
        return []
    nearest_first: list[Path] = []
    if is_within(repo_root, home):
        for parent in repo_root.parents:
            nearest_first.append(parent)
            if parent == home:
                break
    else:
        parents = list(repo_root.parents)
        nearest_first = parents[:-1] if len(parents) > 1 else []
    return list(reversed(nearest_first))


def trusted_ancestor_config(path: Path) -> bool:
    """Whether an ancestor ``rig.yaml`` may take part in the ``task`` cascade (UB-3 review).

    It must be owned by the current user and not group/world-writable, and so must its
    directory -- task-cli's ``_trusted_ancestor`` rule, so both tools read the same ancestors.
    Outside ``$HOME`` the walk passes shared directories such as ``/tmp``; a ``rig.yaml`` another
    local user planted there must not retarget this user's tickets (and the tracker token task-cli
    sends with them). An absent file is simply not trusted (nothing to load, nothing logged, so a
    shared ``/tmp`` does not warn on every load). A rejected existing file is logged at WARNING.
    Platforms without ``os.getuid`` (Windows) skip the check, as task-cli does.
    """
    if not hasattr(os, "getuid"):
        return True
    if not path.is_file():
        return False
    uid = os.getuid()
    for target in (path.parent, path):
        try:
            st = target.stat()
        except OSError as exc:
            get_logger().warning("rig.yaml ancestor skipped: cannot stat %s (%s)", target, exc.strerror or exc)
            return False
        if st.st_uid != uid or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            get_logger().warning("rig.yaml ancestor skipped: %s is not owned by you or is writable by others", target)
            return False
    return True


def read_ancestor_yaml(path: Path) -> dict[str, Any]:
    """:func:`read_yaml_file` for an ANCESTOR ``rig.yaml``, whose failure refuses a descendant load.

    Still fail-closed, but the error names the file as an inherited ancestor (the user is in a
    different repo and may not know it is read) and reports only the YAML error position: PyYAML's
    own message quotes the offending source line, which may carry a pasted credential. The new error
    is raised OUTSIDE the ``except`` block so it chains nothing: the original error would otherwise
    ride along as ``__context__`` and carry that excerpt into any traceback.
    """
    try:
        return read_yaml_file(path)
    except ConfigError as exc:
        mark = getattr(exc.__cause__, "problem_mark", None) or getattr(exc.__cause__, "context_mark", None)
        if mark is not None:
            why = f"YAML syntax error at line {mark.line + 1}, column {mark.column + 1} of {path}"
        elif exc.__cause__ is not None and not isinstance(exc.__cause__, OSError):
            why = f"{path} is not valid YAML"
        else:
            why = str(exc)
    raise ConfigError(
        f"cannot load ancestor config {path} (its `task:` block is inherited by repos below it)",
        why=why,
        fix=f"fix or remove {path}; ancestor rig.yaml files above a repo are read for its task routing",
        schema_path="task",
    )


def load_task_block_from_layer(path: Path, overrides: dict[Path, dict[str, Any]]) -> dict[str, Any] | None:
    """Read just the top-level ``task`` key from one config file (or its test override).

    Returns ``None`` when the file doesn't exist (and has no override) or has no ``task`` key
    at all. An explicit ``task: null`` is NOT treated as absent -- raises, same as the
    top-level-null convention every other ``task:`` consumer in this repo follows.
    """
    resolved = path.resolve()
    if resolved in overrides:
        data = overrides[resolved]
    elif path.is_file():
        data = read_ancestor_yaml(path)
    else:
        return None
    if "task" not in data:
        return None
    task_block = data["task"]
    if task_block is None:
        raise ConfigError(
            f"task must be a mapping (inherited from {path})",
            why=f"{path} declares `task: null`",
            fix=f"remove the `task:` key or give it a mapping in {path}",
            schema_path="task",
        )
    return task_block


def _validate_inherited_task(task_block: Any, path: Path) -> None:
    """Fail closed, naming the ancestor file, when its ``task`` block is not a valid mapping."""
    if not isinstance(task_block, dict):
        raise ConfigError(
            f"task must be a mapping (inherited from {path})",
            why=f"{path} declares a non-mapping `task:` block",
            fix=f"fix the `task:` block in {path}",
            schema_path="task",
        )
    try:
        validate_task(task_block)
    except ConfigError as exc:
        raise ConfigError(
            f"invalid task block inherited from {path}: {exc.what}",
            why=exc.why or f"{path}'s task block failed validation",
            fix=exc.fix or f"fix the `task:` block in {path}",
            schema_path=exc.schema_path or "task",
        ) from exc


def _ancestor_contribution(path: Path, overrides: dict[Path, dict[str, Any]]) -> dict[str, Any]:
    """One ancestor's validated routing contribution; ``{}`` when it has none.

    ``code_prefix`` is stripped: it is repo-unique ship metadata, not routing, and must never be
    inherited.
    """
    task_block = load_task_block_from_layer(path, overrides)
    if task_block is None:
        return {}
    _validate_inherited_task(task_block, path)
    contribution = dict(task_block)
    contribution.pop("code_prefix", None)
    return contribution


def ancestor_task_overlay(
    repo_root: Path, overrides: dict[Path, dict[str, Any]]
) -> tuple[dict[str, Any], list[Path]]:
    """Merge the ``task`` (routing-only) block from every eligible ancestor's ``rig.yaml``.

    OUTERMOST ancestor first, so the NEAREST ancestor wins among ancestors (rig-cli UB-23).
    Only the ``task`` key is ever read from an ancestor file -- no other section leaks into a
    descendant repo. ``code_prefix`` is stripped from every contributor: it is repo-unique ship
    metadata, not routing, and must never be inherited. Each contributor is validated on its own
    (:func:`validate_task`) so a malformed ancestor config fails closed, naming that ancestor's
    file. Returns ``({}, [])`` when nothing contributes -- callers must treat that as a no-op.

    The second return value is every CONTRIBUTING ancestor's path, outermost-to-nearest (the
    same cascade order the merge itself uses) -- an ancestor whose contribution is empty after
    stripping ``code_prefix`` is not a real contributor and is excluded, so ``load()`` reports
    only files that actually affected the result in ``LoadedConfig.layers``.
    """
    merged: dict[str, Any] = {}
    contributing: list[Path] = []
    for directory in task_inheritance_ancestor_dirs(repo_root):
        path = repo_config_path(directory)
        if path.resolve() not in overrides and not trusted_ancestor_config(path):
            continue
        contribution = _ancestor_contribution(path, overrides)
        if not contribution:
            continue
        merged = deep_merge(merged, contribution)
        contributing.append(path.resolve())
    return merged, contributing


def _layer_task(raw_task: Any) -> dict[str, Any]:
    """A layer's raw ``task`` value as a mapping; anything absent or malformed counts as empty."""
    task = raw_task or {}
    return task if isinstance(task, dict) else {}


def recombine_task_layers(
    merged: dict[str, Any],
    key_sources: dict[str, Path],
    raw_task_by_label: dict[str, Any],
    ancestor_task: dict[str, Any],
    ancestor_task_source: Path | None,
) -> None:
    """Fold the ancestor ``task`` overlay into ``merged``/``key_sources`` in place (UB-23).

    Final precedence per KEY (not whole-block replacement): ``global < ancestors < repo``. A
    no-op when no ancestor contributed anything, so the zero-ancestor case stays byte-for-byte
    identical to pre-UB-23 behavior. The ``isinstance`` guards below are defensive, not
    load-bearing: a layer whose raw ``task`` value was itself malformed would already have
    raised inside ``validate(merged)`` before this runs (``raw_task_by_label`` is captured
    pre-validation, so it can't be assumed well-typed on its own).
    """
    if not ancestor_task:
        return
    global_layer_task = _layer_task(raw_task_by_label.get("global"))
    repo_layer_task = _layer_task(raw_task_by_label.get("repo") or raw_task_by_label.get("config"))
    final_task = deep_merge(deep_merge(dict(global_layer_task), ancestor_task), dict(repo_layer_task))
    validate_task(final_task)
    merged["task"] = final_task
    has_repo_layer = "repo" in raw_task_by_label or "config" in raw_task_by_label
    if not has_repo_layer and ancestor_task_source is not None:
        key_sources["task"] = ancestor_task_source
