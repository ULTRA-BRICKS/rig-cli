"""Area summary, per-layer drift and known-name renderers for `rig status`.

Context: pure renderers over the drift report; layer headings are shared with the config renderers.
"""
from __future__ import annotations

import sys
from itertools import chain

from .cli_output import _LAYER_HEADERS, _bold, _dim, _ok, _warn
from .provenance import KNOWN_NAMES_SHOWN as _KNOWN_NAMES_SHOWN


def _declaring_config(category: str, loaded) -> str:
    """Name the config FILE that declares a drift item's layer (or 'not declared in any layer').

    GLOBAL categories are declared in ``~/.config/rig/config.yaml``; REPO categories in the
    repo's ``./rig.yaml``. If that layer file isn't loaded (the category drifted from a default
    or an orphaned on-disk extra with no backing config), say so plainly instead of pointing at
    a file that doesn't declare it.
    """
    from .layers import GLOBAL, layer_for_category

    # `ship_env`/`gh_ship_alias` are GLOBAL machine artifacts with no key of their own — their
    # provenance is the layer that declares the delegator/alias combo. `source_for_key` tracks the
    # REAL declaring layer per key (repo OR global), so a ci-only combo declared purely in the global
    # cascade (`ship_delegator.enabled: false` + `ci.items.ship.gh_alias`) is attributed to the
    # global config even when an UNRELATED repo `rig.yaml` also exists — the case a bare repo-vs-
    # global lookup mislabels. Falls back to the primary config when the (default-on) delegator
    # declares no key, matching "would live in the repo rig.yaml".
    if category in ("ship_env", "gh_ship_alias"):
        return str(loaded.source_for_key("ship_delegator"))
    layer = layer_for_category(category)
    path = loaded.global_path if layer == GLOBAL else loaded.repo_path
    return str(path) if path is not None else "not declared in any layer"


def _area_notes_suffix(area, report) -> str:
    """The informational notes for ONE area, rendered dim after its state (rig-cli#375).

    A note is a fact the area's checker wants visible that is NOT drift — the Spotlight dependency
    dirs created since the last sweep, say: nothing is broken and the next sweep covers them, but
    dropping them silently is how a stalled sweep agent hides. Never folded into the drift counts,
    never part of the exit code.
    """
    from .areas import area_matches_drift

    notes = [n.detail for n in report.notes if area_matches_drift(area, n.category, n.item)]
    return _dim("  (" + "; ".join(notes) + ")") if notes else ""


def _area_state_line(area, plan, report, extra_configured_actions=(), held_back_actions=()) -> str:
    """The rendered state for ONE area, plus any informational notes for it."""
    return (_area_state_core(area, plan, report, extra_configured_actions, held_back_actions)
            + _area_notes_suffix(area, report))


def _area_counts(area, report) -> tuple[int, int, int]:
    """``(missing/modified, on-disk-not-declared, known)`` drift counts for ONE area.

    The reliable, unit-stable numbers are the DRIFT counts (config→disk + disk→config). We
    deliberately do NOT print an "in-sync item count": a plan action and a drift item are not 1:1
    across areas (the gitignore area is ONE action but TWO drift checks, and a skill is one copy
    action + one harness-link action), so ``declared − missing`` mixes units. A known item is by
    construction a disk-side extra (the scanners file it from the same walk that yields `extra`
    drift), so the drift matcher's `extra` routing applies to it verbatim (rig-cli#357).
    """
    from .areas import area_matches_drift

    missing = sum(
        1 for d in (report.by_direction("missing") + report.by_direction("modified"))
        if area_matches_drift(area, d.category, d.item, d.direction)
    )
    extra = sum(
        1 for d in report.by_direction("extra")
        if area_matches_drift(area, d.category, d.item, d.direction)
    )
    known = sum(1 for k in report.known if area_matches_drift(area, k.category, k.item, "extra"))
    return missing, extra, known


def _platform_unsupported_note(area_key: str) -> str | None:
    """The "unsupported" state of a CONFIGURED, drift-free area whose provisioner is a no-op here.

    ``tg_ctl`` / ``tgctl_reaper`` are macOS-only (launchd) and ``sysmon`` needs launchd or
    systemd --user: off those platforms detect() reports no drift even though nothing is
    installed, so a naive "no drift → in sync" would mislead.
    """
    if area_key in ("tg_ctl", "tgctl_reaper"):
        from .drift import _on_darwin

        if not _on_darwin():
            return _dim("unsupported (macOS-only; no-op off darwin)")
    elif area_key == "sysmon":
        if sys.platform != "darwin" and not sys.platform.startswith("linux"):
            return _dim(f"unsupported (no launchd/systemd on {sys.platform})")
    return None


def _unconfigured_state(area, held_back_actions) -> str:
    """An area with no plan action: "held back" when only tracked_files holds its actions back, else "not configured".

    A held-back area is configured, but its file lands on the default branch instead of this
    checkout. An area the config turns OFF stays "not configured" even when the machine-wide
    target holds known items — "in sync" would claim a reconciliation that never happens.
    """
    from .areas import area_matches_action

    if any(area_matches_action(area, a.category, a.options) for a in held_back_actions):
        return _dim("held back — lands on the default branch via tracked_files (below)")
    return _dim("not configured")


def _area_state_core(area, plan, report, extra_configured_actions=(), held_back_actions=()) -> str:
    """The rendered state for ONE area: "not configured" / "in sync" / "drift (…)".

    ``extra_configured_actions``: plan-dropped actions (repo-scoped, non-git cwd) that still mark
    their GLOBAL areas configured — see _render_area_summary. Configured-ness only, no counts.
    A configured area off its provisioner's platform is rendered "unsupported" (matching the
    dedicated detail lines), never a false "in sync" — but only when there really is no drift: a
    stale plist from a prior macOS run must not be masked.
    """
    from .areas import area_matches_action

    configured = any(
        area_matches_action(area, a.category, a.options)
        for a in chain(plan.actions, extra_configured_actions)
    )
    missing, extra, known = _area_counts(area, report)
    # on-disk items rig can place but does not manage: shown as a count next to the state, never
    # folded into the drift counts (they are informational, not drift).
    known_what = "your additions, kept" if area.key == "permissions" else "known, not rig-managed"
    known_note = _dim(f"  ({known} {known_what})") if known else ""

    if missing == 0 and extra == 0:
        if not configured:
            return _unconfigured_state(area, held_back_actions) + known_note
        return (_platform_unsupported_note(area.key) or _ok("in sync") + known_note)
    parts = []
    if missing:
        # "missing/modified" because the count folds both directions of config→disk drift: a
        # declared item absent from disk AND one present-but-changed. Naming only "missing" would
        # mislabel a modified-on-disk file as deleted (matches _render_drift_by_layer's wording).
        parts.append(f"{missing} declared-but-missing/modified")
    if extra:
        parts.append(f"{extra} on-disk-not-declared")
    return _warn(f"drift ({', '.join(parts)})") + known_note


def _render_area_summary(plan, report, env, extra_configured_actions=(), held_back_actions=()) -> None:
    """Print the AREA SUMMARY: every reconciled area, grouped by layer, with in-sync vs drift.

    The pre-summary status rendered ONLY drifting items as a flat dump dominated by skill rows
    (one per skill + a harness-link row each), so it read as "mostly skills" and every other
    area was buried or — when in sync — invisible. This summary enumerates the full area
    registry (:mod:`riglib.areas`) so the user sees, at a glance, the WHOLE set of things rig
    manages and where each stands: configured-and-in-sync, drifted (with a count), or not
    configured. The detailed per-item drift dump still follows below for the actual problems.

    REPO-layer areas are gated to git repos (a non-git dir has no repo layer); in a non-git dir
    only the GLOBAL areas show.

    ``extra_configured_actions`` are actions DROPPED from the plan (repo-scoped actions outside
    git) that must still mark their GLOBAL areas configured: the ship_delegator action carries
    the machine-wide ship_env artifact, and with it dropped a clean env file would render
    ``not configured`` even though apply manages the file from this cwd too. They feed only the
    configured-ness check, never drift counts (their repo-side drift stays out of a non-git run).
    ``held_back_actions`` are the actions ``tracked_files`` holds back (``plan.diverted``): an area
    carried only by them reads "held back", not "not configured".
    """
    from .areas import areas_for_layer
    from .layers import GLOBAL, REPO

    print()
    print(_bold("  areas rig manages:"))
    # only render (and only count) the layers that will actually show — a non-git dir has no
    # REPO layer, so its REPO areas are neither computed nor printed.
    layers = (GLOBAL,) if not env.is_git_repo else (GLOBAL, REPO)
    for layer in layers:
        print(_dim(f"    {_LAYER_HEADERS[layer]}"))
        for area in areas_for_layer(layer):
            line = _area_state_line(area, plan, report, extra_configured_actions, held_back_actions)
            print(f"      {area.label}: {line}")


def _bucket_drift_by_layer(report) -> dict[str, dict[str, list]]:
    """Bucket every drift item by its owning layer, preserving missing-before-extra ordering."""
    from .layers import GLOBAL, REPO, layer_for_category

    buckets: dict[str, dict[str, list]] = {
        GLOBAL: {"missing": [], "extra": []},
        REPO: {"missing": [], "extra": []},
    }
    for d in report.by_direction("missing") + report.by_direction("modified"):
        buckets[layer_for_category(d.category)]["missing"].append(d)
    for d in report.by_direction("extra"):
        buckets[layer_for_category(d.category)]["extra"].append(d)
    return buckets


def _print_layer_drift(layer: str, miss: list, ext: list, loaded) -> None:
    print()
    print(_bold(f"  {_LAYER_HEADERS[layer]}"))
    if miss:
        print(_warn(f"    config→disk drift ({len(miss)}) — declared but missing/modified:"))
        for d in miss:
            src = _declaring_config(d.category, loaded)
            print(f"      {_warn('▸')} {d.category}/{d.item}: {d.detail}  {_dim('[declared in ' + src + ']')}")
    if ext:
        print(_warn(f"    disk→config drift ({len(ext)}) — on disk, not declared:"))
        for d in ext:
            src = _declaring_config(d.category, loaded)
            print(f"      {_warn('▸')} {d.category}/{d.item}: {d.detail}  {_dim('[' + str(d.target) + '; ' + src + ']')}")


def _render_drift_by_layer(report, loaded, env) -> None:
    """Render drift GROUPED by GLOBAL vs REPO, each item naming its declaring config file.

    The pre-v2 status flattened machine-wide GLOBAL drift (skills/hooks/harness/mcp) together
    with this repo's REPO drift (CI/symlinks) into one undifferentiated dump — so a global
    skills drift looked like the repo's problem. Grouping by layer + naming the source file
    makes each item's ownership unambiguous.
    """
    from .layers import GLOBAL, REPO

    buckets = _bucket_drift_by_layer(report)
    for layer in (GLOBAL, REPO):
        miss = buckets[layer]["missing"]
        ext = buckets[layer]["extra"]
        # the REPO layer is meaningless outside a git repo — never print it there.
        if (not miss and not ext) or (layer == REPO and not env.is_git_repo):
            continue
        _print_layer_drift(layer, miss, ext, loaded)


def _render_known_by_layer(report, loaded, env) -> None:
    """Render the KNOWN items (rig-cli#357) grouped by layer, then by origin — one line per group.

    Two headings per layer: "known, not managed by rig" (skills / hooks / workflows / MCP entries
    whose origin rig can name) and "your additions, kept" (permission entries beyond the rig
    baseline). Both are informational — printed dim, never counted as drift, never part of the
    exit code — so a clean machine with 40 tool-installed skills reads as clean, not as 40 drift.
    """
    from .layers import GLOBAL, REPO, layer_for_category

    buckets: dict[str, dict[str, list]] = {GLOBAL: {"placed": [], "kept": []}, REPO: {"placed": [], "kept": []}}
    for k in report.known_placed:
        buckets[layer_for_category(k.category)]["placed"].append(k)
    for k in report.known_kept:
        buckets[layer_for_category(k.category)]["kept"].append(k)
    for layer in (GLOBAL, REPO):
        placed, kept = buckets[layer]["placed"], buckets[layer]["kept"]
        if not (placed or kept) or (layer == REPO and not env.is_git_repo):
            continue
        print()
        print(_bold(f"  {_LAYER_HEADERS[layer]}"))
        if placed:
            print(_dim(f"    known, not managed by rig ({len(placed)}) — informational, not drift:"))
            _print_known_groups(placed, loaded)
        if kept:
            print(_dim(f"    your additions, kept ({len(kept)}) — beyond the rig baseline; rig never removes them:"))
            _print_known_groups(kept, loaded)


def _print_known_groups(items: list, loaded) -> None:
    """One line per (category, origin kind[, permission container]) with the member names joined."""
    from .provenance import KIND_CONFIG_KNOWN, KIND_LABELS

    groups: dict[tuple[str, str, str], list] = {}
    for k in items:
        # permission entries carry the list they sit in (`claude-code:permissions.deny`, …) and
        # print under it — the other categories group per origin only
        groups.setdefault((k.category, k.kind, k.container), []).append(k)
    for (category, kind, container), members in groups.items():
        names = [_known_name(k) for k in members]
        shown = ", ".join(names[:_KNOWN_NAMES_SHOWN])
        if len(names) > _KNOWN_NAMES_SHOWN:
            shown += f" … and {len(names) - _KNOWN_NAMES_SHOWN} more"
        where = container or category
        label = KIND_LABELS[kind]
        if kind == KIND_CONFIG_KNOWN:
            label += f" ({category}.known in {_declaring_config(category, loaded)})"
        print(f"      {_dim('▸')} {where} ({len(members)}) {label}: {shown}")


def _known_summary_note(report) -> str:
    """The parenthetical after "in sync": known-not-managed and kept-additions counted apart."""
    kept = len(report.known_kept)
    placed = len(report.known_placed)
    parts = []
    if placed:
        parts.append(f"{placed} known item{'s' if placed != 1 else ''} not managed by rig")
    if kept:
        parts.append(f"{kept} permission addition{'s' if kept != 1 else ''} kept")
    return f" ({', '.join(parts)})" if parts else ""


def _known_name(k) -> str:
    """The display name of a known item; names the installer when it differs from the item."""
    return f"{k.name} (by {k.by})" if k.by and k.by != k.name else k.name
