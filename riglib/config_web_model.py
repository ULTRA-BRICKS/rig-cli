"""config-web's page view model: the reconciled registry areas projected with live values.

Split out of :mod:`riglib.config_web`. Read-only — it loads the cascade and the registry
(:mod:`riglib.schema`) and never writes; the write path stays in ``config_web.apply_edit``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config as cfg
from . import schema
from .layers import GLOBAL

# ── view model ────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FieldView:
    """One editable option as the page renders it: the live value + where an edit would land."""

    key: str
    kind: str
    value: Any
    default: Any
    hint: str
    choices: tuple[str, ...]
    layer: str  # GLOBAL / REPO — the file an edit to THIS field is written to
    # The repo file that ALSO sets this GLOBAL option's key on a repo tab (``None`` otherwise).
    # A hand-committed repo-level list REPLACES the global one for that repo, so the field shows
    # the repo value while an edit lands in the global file — the page says so instead of letting
    # the edit look like a silent no-op (rig never deletes the repo entry itself).
    shadowed_by: str | None = None

    @property
    def layer_file(self) -> str:
        """A human label for the owning file (matches the wizard's layer routing)."""
        return "~/.config/rig/config.yaml" if self.layer == GLOBAL else "./rig.yaml"


@dataclass(frozen=True)
class AreaView:
    """One reconciled area (a ``rig status`` row) with its editable fields."""

    category: str
    title: str
    blurb: str
    layer: str
    fields: tuple[FieldView, ...]


@dataclass(frozen=True)
class ConfigModel:
    """The whole page model: the cascaded config's areas + provenance of the two layer files."""

    areas: tuple[AreaView, ...]
    repo_root: Path
    global_path: Path
    repo_path: Path
    global_present: bool
    repo_present: bool
    global_only: bool = False


def build_model(repo_root: Path, *, global_only: bool = False) -> ConfigModel:
    """Read the cascaded config for ``repo_root`` and project it into the page :class:`ConfigModel`.

    Uses the SAME cascade loader the rest of rig uses (global then repo), and the SAME registry
    (:mod:`riglib.schema`) the wizard reads — so every reconciled area + option shows here with
    its live effective value, no parallel field list. ``effective_value`` applies the
    block-presence gating (a feature ``rig apply`` would skip reads as OFF, not its default), so
    the page never advertises a setting the reconciler ignores.

    ``global_only=True`` is the Global scope tab (rig-cli#310): loads ``~/.config/rig/config.yaml``
    ALONE via ``cfg.load(repo_root, include_repo=False)`` — no repo overlay — and renders only the
    OPTIONS whose WRITABLE layer is GLOBAL (:attr:`~riglib.schema.Option.layer`): the
    machine-wide-only categories the scaffold never writes into a committed repo file
    (gitignore/spotlight/tg_ctl/tmux/mode) plus the per-option GLOBAL overrides inside
    repo-writable areas (``skills.known`` / ``agent_hooks.known`` / ``mcp.known``, rig-cli#372 —
    see :func:`_area_view`). ``repo_root`` is still required as the loader's anchor even though
    its repo layer is skipped. NOTE: the Global tab's PLAN and DRIFT panel stay restricted to the
    global-only CATEGORIES (:func:`riglib.config_web_plan.build_scope_plan` /
    ``compute_scope_drift``) — a known-list edit changes what is *known*, not what is applied,
    and widening the drift scan to the skills/hooks/MCP dirs would flood that tab with every
    REPO-tab-installed item (the very reason the restriction exists). The provenance effect of
    the edit is visible on each REPO tab's drift panel, where those dirs are scanned.

    The Global tab's VIEW, PLAN and DRIFT all use the WRITABLE-layer classification (never the
    broader STATUS layer of :func:`riglib.layers.layer_for_category`): the view filters by
    ``Option.layer``, the plan/drift by :func:`riglib.schema.global_only_categories` — see
    :func:`riglib.config_web_plan.build_scope_plan` for why plan == view is load-bearing there.
    """
    loaded = cfg.load(repo_root, include_repo=not global_only)
    repo_path = cfg.repo_config_path(repo_root)
    # the repo layer ALONE (already parsed successfully by cfg.load above), to flag a GLOBAL
    # option whose key the committed repo file also sets — see FieldView.shadowed_by.
    repo_layer = cfg.read_yaml_file(repo_path) if not global_only and repo_path.is_file() else {}
    areas = [
        view
        for area in schema.AREAS
        if (
            view := _area_view(
                area, loaded.data, global_only=global_only, repo_layer=repo_layer, repo_path=repo_path
            )
        )
        is not None
    ]
    return ConfigModel(
        areas=tuple(areas),
        repo_root=repo_root,
        global_path=cfg.global_config_path(),
        repo_path=cfg.repo_config_path(repo_root),
        global_present=cfg.global_config_path().is_file(),
        repo_present=cfg.repo_config_path(repo_root).is_file(),
        global_only=global_only,
    )


def _area_view(
    area: schema.Area,
    merged: dict[str, Any],
    *,
    global_only: bool,
    repo_layer: dict[str, Any],
    repo_path: Path,
) -> AreaView | None:
    """Project one registry area for the page; ``None`` when it has nothing to show on this tab.

    Filters by each OPTION's writable layer, not the area's (an area can be MIXED — see
    :meth:`riglib.schema.Area.options_for_layer`). On the Global tab only the GLOBAL options
    render and the section badge says GLOBAL (matching the fields under it); a repo tab renders
    every option, each field carrying its own layer badge.
    """
    options = area.options_for_layer(GLOBAL) if global_only else area.options
    if not options:
        return None
    fields = tuple(
        FieldView(
            key=opt.key,
            kind=opt.kind,
            value=schema.effective_value(opt, merged),
            default=opt.default,
            hint=opt.hint,
            choices=opt.choices,
            layer=opt.layer,
            shadowed_by=_shadowing_repo_file(opt, repo_layer, repo_path),
        )
        for opt in options
    )
    return AreaView(
        category=area.category,
        title=area.title,
        blurb=area.blurb,
        layer=GLOBAL if global_only else area.layer,
        fields=fields,
    )


def _shadowing_repo_file(
    option: schema.Option, repo_layer: dict[str, Any], repo_path: Path
) -> str | None:
    """``repo_path`` when the committed repo file ALSO sets a GLOBAL option's key (else ``None``)."""
    if option.layer != GLOBAL or not repo_layer:
        return None
    try:
        schema.get_path(repo_layer, option.key)
    except schema.KeyError_:
        return None
    return str(repo_path)
