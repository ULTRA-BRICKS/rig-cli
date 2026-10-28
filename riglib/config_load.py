"""Config cascade loader: :class:`LoadedConfig` and :func:`load` (global < ancestors < repo).

Why a separate module: reading layers, guarding their placement and merging them with provenance is
the loader's whole job; the dot-path helpers, the YAML reader, the per-layer guards and the ancestor
``task`` overlay it composes live in their own modules, and the schema validators it calls
(:func:`riglib.config_validate.validate`) never depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config_lint_blocks
from .config_ancestors import ancestor_task_overlay, recombine_task_layers
from .config_errors import ConfigError
from .config_load_guards import reject_misplaced_blocks, reject_repo_only_in_global
from .config_paths import global_config_path, repo_config_path
from .config_validate import validate
from .config_yaml import deep_merge, read_yaml_file


@dataclass
class LoadedConfig:
    """A cascaded, validated config plus provenance of where each layer came from."""

    data: dict[str, Any]
    repo_root: Path
    global_path: Path | None = None
    repo_path: Path | None = None
    layers: list[str] = field(default_factory=list)
    # Provenance: which config FILE last declared each top-level key. Built during the cascade
    # (global first, then repo/explicit overwrites), so a key present only in the global config
    # maps to the global path even when a repo rig.yaml is also loaded. Used to name the CORRECT
    # source file in an unknown-item error instead of always blaming the repo file.
    key_sources: dict[str, Path] = field(default_factory=dict)
    # The GLOBAL layer's own data, before a repo layer merged over it — for the machine-wide
    # settings a repo override must never reach (FJ_FALLBACK_HOST is the global fj.host).
    global_data: dict[str, Any] = field(default_factory=dict)

    @property
    def has_declared_config(self) -> bool:
        """True when a global, repo or ``--config`` layer was loaded.

        An ``ancestor:`` layer does not count: an ancestor ``rig.yaml`` contributes only its
        ``task`` routing block, which apply does not consume, so it is no source of truth for a
        repo's apply surface. The "no rig.yaml found" guards (``rig apply``, config-web) use this
        so an ancestor-only directory still refuses to apply built-in defaults.
        """
        return any(not layer.startswith("ancestor:") for layer in self.layers)

    @property
    def agent_tools_source(self) -> str | None:
        v = self.data.get("agent_tools_source")
        return str(v) if v else None

    @property
    def stack(self) -> str | None:
        """The declared stack preset (``l1/lang[/framework]``), or ``None`` when unset.

        This is the STACK PRESET (the by-stack curation axis), distinct from the build-toolchain
        ``detect.Environment.stack``. Cascaded like every value: a repo ``stack`` overrides the
        global default. Shape is validated in :func:`validate`; here we only surface the raw value.
        """
        v = self.data.get("stack")
        return str(v).strip() if isinstance(v, str) and v.strip() else None

    def category(self, name: str) -> dict[str, Any]:
        cat = self.data.get(name)
        return cat if isinstance(cat, dict) else {}

    @property
    def defaults(self) -> dict[str, Any]:
        d = self.data.get("defaults")
        return d if isinstance(d, dict) else {}

    @property
    def primary_config_path(self) -> Path:
        """The most-specific config file backing this config, for naming in error messages.

        The repo ``rig.yaml`` OVERRIDES the global config, so an invalid key is most likely
        the repo one — name it first; fall back to the global path, then to a notional repo
        path so an error always points somewhere concrete to edit.
        """
        if self.repo_path is not None:
            return self.repo_path
        if self.global_path is not None:
            return self.global_path
        return repo_config_path(self.repo_root)

    def source_for_key(self, dotted_key: str) -> Path:
        """The config FILE that declared ``dotted_key`` (e.g. ``mcp.items.review``).

        Resolves provenance by the TOP-LEVEL key (``mcp``): a key that came solely from the
        global ``~/.config/rig/config.yaml`` names the global file, while a key the repo
        ``rig.yaml`` set (or overrode) names the repo file. This is what lets an unknown-item
        error point at the file actually carrying the stale entry — e.g. a removed MCP slot
        left in the global config is reported against the global file, not the repo's.

        Falls back to :attr:`primary_config_path` when the key has no tracked provenance (a
        config built directly in tests, or a key that is not top-level).
        """
        top = dotted_key.split(".", 1)[0]
        src = self.key_sources.get(top)
        return src if src is not None else self.primary_config_path


@dataclass
class _Cascade:
    """The accumulator the layers merge into, so no layer can forget part of the bookkeeping."""

    overrides: dict[Path, dict[str, Any]]
    merged: dict[str, Any] = field(default_factory=dict)
    layers: list[str] = field(default_factory=list)
    # Track which file last declared each top-level key, in cascade order (global, then
    # repo/explicit). A key the repo layer sets overwrites the global provenance — so the final
    # mapping names the file the user must edit to remove the offending key.
    key_sources: dict[str, Path] = field(default_factory=dict)
    # Each layer's OWN (pre-merge, raw) `task` value, keyed by label -- used to recombine the
    # `task` block around the ancestor overlay after the normal cascade validates (UB-23).
    raw_task_by_label: dict[str, Any] = field(default_factory=dict)
    global_data: dict[str, Any] = field(default_factory=dict)
    # raw per-layer blocks, to name layers in merged errors
    lint_checks_layers: dict[str, Any] = field(default_factory=dict)

    def merge_layer(self, path: Path, label: str) -> None:
        """Merge one config file into the accumulator and record its key provenance + layer.

        Single seam so a new layer can't forget to update ``key_sources`` (the bug the
        provenance feature exists to avoid). ``merged`` is rebound because ``deep_merge``
        returns a fresh dict rather than mutating in place.
        """
        resolved_path = path.resolve()
        data = dict(self.overrides[resolved_path]) if resolved_path in self.overrides else read_yaml_file(path)
        if "task" in data:
            self.raw_task_by_label[label] = data["task"]
        reject_misplaced_blocks(data, path, label)
        config_lint_blocks.validate_layer(data, label, self.lint_checks_layers)
        reject_repo_only_in_global(data, path, label)
        self.merged = deep_merge(self.merged, data)
        if label == "global":
            self.global_data.update(data)
        for k in data:
            self.key_sources[k] = path
        self.layers.append(f"{label}:{path}")

    def has_layer_file(self, path: Path) -> bool:
        return path.is_file() or path.resolve() in self.overrides


def _explicit_layer_label(rpath: Path, repo_root: Path) -> str:
    """Which layer an explicit ``--config`` file counts as: it may BE the repo or global file."""
    if rpath == repo_config_path(repo_root).resolve():
        return "repo"
    if rpath == global_config_path().resolve():
        # the global file passed explicitly (`config set --global`/`env set` validate it in
        # isolation this way) is still the GLOBAL layer — its global-only blocks are legal.
        return "global"
    return "config"


def _merge_repo_scope(cascade: _Cascade, repo_root: Path, explicit_config: Path | None) -> Path:
    """Merge the repo (or ``--config``) layer; returns the path it was looked up at."""
    if explicit_config is None:
        rpath = repo_config_path(repo_root)
        if cascade.has_layer_file(rpath):
            cascade.merge_layer(rpath, "repo")
        return rpath
    rpath = explicit_config.resolve()
    if not rpath.is_file() and rpath not in cascade.overrides:
        raise ConfigError(f"--config file not found: {rpath}")
    cascade.merge_layer(rpath, _explicit_layer_label(rpath, repo_root))
    return rpath


def load(
    repo_root: Path,
    *,
    explicit_config: Path | None = None,
    include_global: bool = True,
    include_repo: bool = True,
    include_ancestors: bool = True,
    layer_overrides: dict[Path, dict[str, Any]] | None = None,
) -> LoadedConfig:
    """Cascade-load config for ``repo_root``.

    - ``explicit_config`` (from ``--config P``) replaces the per-repo layer with ``P``.
    - The global layer is always the base unless ``include_global=False``.
    - ``include_repo=False`` skips the repo/config layer entirely, including ``explicit_config``
      AND the ``task``-block ancestor overlay (UB-23): ancestors are repo-scoped folder
      defaults, not global config, so a caller asking for the GLOBAL layer only must not see
      them leak in either -- the result stays GLOBAL-ONLY.
    - ``include_ancestors=False`` skips the ``task``-block ancestor overlay (UB-23; see the
      module docstring) — the global/repo layers still load normally.
    - The result is validated (fail-closed) before return.
    """
    repo_root = repo_root.resolve()
    cascade = _Cascade(overrides={Path(path).resolve(): dict(data) for path, data in (layer_overrides or {}).items()})
    gpath: Path | None = None
    rpath: Path | None = None

    # Ancestors are repo-scoped folder defaults, not global config -- gated by BOTH flags so a
    # caller that excludes the repo scope (`include_repo=False`, e.g. a GLOBAL-only read) never
    # sees them leak in (the gap this fix closes; ancestors used to be gated by
    # `include_ancestors` alone, independent of `include_repo`).
    ancestor_task, ancestor_task_sources = (
        ancestor_task_overlay(repo_root, cascade.overrides) if (include_ancestors and include_repo) else ({}, [])
    )

    if include_global:
        gpath = global_config_path()
        if cascade.has_layer_file(gpath):
            cascade.merge_layer(gpath, "global")

    # Report every contributing ancestor in `LoadedConfig.layers`, in cascade order (outermost
    # first, matching the merge itself) -- inserted between the global and repo entries so the
    # list reads in the same `global < ancestors < repo` order the precedence follows.
    cascade.layers.extend(f"ancestor:{ancestor_path}" for ancestor_path in ancestor_task_sources)

    if include_repo:
        rpath = _merge_repo_scope(cascade, repo_root, explicit_config)

    config_lint_blocks.resolve_merged(cascade.merged, cascade.lint_checks_layers)
    validate(cascade.merged)
    ancestor_task_source = ancestor_task_sources[-1] if ancestor_task_sources else None
    recombine_task_layers(
        cascade.merged, cascade.key_sources, cascade.raw_task_by_label, ancestor_task, ancestor_task_source
    )
    cascade.merged.pop("scope", None)  # `scope` is a removed legacy key — drop it so it never
    # lingers in loaded.data, gets re-serialized, or is mistaken for a live setting.
    cascade.key_sources.pop("scope", None)
    return LoadedConfig(
        data=cascade.merged,
        repo_root=repo_root,
        global_path=gpath if gpath and gpath.is_file() else None,
        repo_path=rpath if rpath and rpath.is_file() else None,
        layers=cascade.layers,
        key_sources=cascade.key_sources,
        global_data=cascade.global_data,
    )


def resolve_init_stack(repo_root: Path, *, explicit: str | None = None, global_stack: str | None = None) -> str | None:
    """Resolve the stack preset a fresh ``rig init`` writes into the committed rig.yaml.

    Cascade (mirrors the headless init resolver so the interactive wizard and the headless
    path agree): an explicit ``--stack`` wins, else the global-config default, else a
    best-guess from the repo files, else ``None`` (unset → :func:`stack_requirement_warning`).
    Both the headless `_resolve_init_plan` and the interactive TUI go through here so the
    two front-ends can never seed a different stack."""
    from .detect import detect_stack_preset

    return explicit or global_stack or detect_stack_preset(repo_root)


def stack_requirement_warning(config: "LoadedConfig") -> str | None:
    """A soft-require warning when a repo config carries no ``stack``, else ``None``.

    Per-repo ``stack`` is mandatory by POLICY but enforced softly during the migration phase: a
    missing value warns (and points at the detected guess) rather than failing. Callers (init /
    apply / status) print the returned line. A present stack → ``None`` (no warning)."""
    if config.stack:
        return None
    from .detect import detect_stack_preset

    guess = detect_stack_preset(config.repo_root)
    hint = f"; rig detected '{guess}'" if guess else ""
    return (
        "stack: not set — declare it in rig.yaml as l1/lang[/framework] "
        f"(e.g. mobile/swift/swiftui, frontend/ts/react, backend/python){hint}. "
        "Run `rig init` to set it, or `rig config set stack <value>`."
    )
