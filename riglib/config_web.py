"""config-web — a local, machine-wide web console to VIEW, EDIT, and APPLY the reconciled rig
config (rig-cli#310).

What this is
------------
A self-contained, dependency-free web front-end over the SAME config + apply engine that ``rig
setup`` (the wizard), ``rig config get|set``, and ``rig apply`` itself drive: :mod:`riglib.schema`
(the option REGISTRY), :mod:`riglib.config` (the cascade loader, the dot-path set, the layer
files), and :mod:`riglib.plan` / :mod:`riglib.actions.runner` (``plan.build`` + ``run_plan``, the
SAME plan/execute engine ``rig apply``/``rig init`` use — never a forked executor). One browser
tab per rig-managed repo (discovered via :mod:`riglib.config_web_scopes`, read-only over the
existing machine-local repository registry) plus a dedicated GLOBAL tab
(``~/.config/rig/config.yaml`` alone, no repo overlay). Each tab shows every reconciled AREA with
its live effective value, a DRIFT panel (:mod:`riglib.config_web_plan`'s ``compute_scope_drift`` —
the same two-way ``rig status`` engine), and lets the user change a value in the browser — routing
the write to the owning layer's file exactly like the wizard, then re-validating fail-closed.

An edit no longer silently stops at the config file. It surfaces a PLAN PREVIEW (mirroring ``rig
apply info``, each planned action carrying a visual tag from :mod:`riglib.action_tags` naming what
kind of change it is, exactly where on disk/remotely it lands, and whether it affects a running
agent or the human's own workflow), lets the user confirm the whole plan or skip individual
actions, then applies the SELECTED actions with live per-phase progress
(:class:`~riglib.config_web_plan.ApplyJobStore`, reusing ``run_plan``'s existing
``on_start``/``progress`` callbacks — the same mechanism PR #306 added for the ``rig init`` TUI's
live Apply screen). Exactly one apply job may run process-wide at a time, and a stale preview
(config changed since the browser fetched it) is refused rather than silently applied.

How it is reached at runtime
----------------------------
``riglib.cli`` registers a ``config-web`` subcommand whose lifecycle (run/start/stop/status/
enable/disable) is wired through the shared ``agenttools_service`` library (the one service
manager every long-running server in the ecosystem shares — review dashboard, tg-ctl, the
daemon-supervisor). ``run`` serves :class:`ConfigWebApp` in the foreground; ``start`` daemonizes
it; ``enable`` installs an OS autostart (launchd / systemd --user) AND starts it now. The bare
``rig config-web`` (no subcommand) prints HELP and NEVER launches a server. The HTTP layer is
stdlib ``http.server`` (mirroring ``riglib.stats.render.web``): no CDN, no JS framework, inline
CSS, vanilla-JS ``fetch`` calls against the small JSON API below.

Invariants
----------
- **One config + apply engine, several front-ends.** The view model and the edit write go through
  :mod:`riglib.schema` (``AREAS`` / ``effective_value`` / ``coerce`` / ``writable_layer_for_category``)
  and :mod:`riglib.config` (``read_yaml_file`` / ``set_path`` / ``validate``); the plan preview and
  apply go through :mod:`riglib.plan` (``build``) and :mod:`riglib.actions.runner` (``run_plan``) —
  config-web adds NO parallel plan builder or executor. Filtering the SELECTED actions into a new
  ``InstallPlan`` before calling ``run_plan`` is not a fork of the executor — it is the same call,
  over a subset of the same plan.
- **Every multi-repo endpoint goes through the scope ALLOWLIST.** ``/edit``, ``/api/scope``,
  ``/api/drift``, ``/api/plan``, ``/api/apply`` all resolve a request's ``scope`` id against
  :func:`~riglib.config_web_scopes.discover_scopes`'s result for THIS server instance
  (:meth:`ConfigWebApp._resolve_scope`) — an unrecognized id is refused (404/400), never treated
  as an arbitrary filesystem path.
- **A stale plan is refused, never silently applied.** ``/api/plan`` returns a fingerprint (a hash
  of the ordered action list — kind/category/item/target/source/options, plus ``on_conflict``, so
  it changes even when the action IDENTITIES stay the same but their content/behavior would
  differ); ``/api/apply`` rebuilds the plan fresh and 409s if the fingerprint no longer matches —
  the config changed since the browser previewed it.
- **Edits route to the OWNING layer.** A REPO option writes that scope's ``./rig.yaml``; a
  GLOBAL-only option (``gitignore`` / ``tg_ctl`` / ``tmux`` / ``mode`` / ``spotlight``) writes
  ``~/.config/rig/config.yaml`` — the same routing ``writable_layer_for_category`` enforces for
  the wizard, keeping a machine-wide block out of a committed repo file.
- **Fail-closed on write.** A value is coerced per the option's kind, written to a COPY of the
  target file's dict, the merged result is re-:func:`~riglib.config.validate`'d, and only then is
  the file rewritten. A bad value leaves the file untouched and returns an error to the browser.
- **Bind localhost only — but that is NOT a browser security boundary.** The server binds
  ``127.0.0.1`` (never ``0.0.0.0``); still, ANY web page the user visits can ``fetch`` a
  loopback URL, so every mutating/compute-triggering POST (``/edit``, ``/api/plan``,
  ``/api/apply``) is gated by the SAME same-origin/CSRF check (:func:`is_cross_site_write`, plus
  an ``application/json`` content-type requirement for the endpoints that carry a body) — a
  ``cross-site`` ``Sec-Fetch-Site`` or a foreign ``Origin`` is refused 403. A non-browser CLI
  client (curl, the tests) sends neither header and is allowed; the threat model is a hostile web
  page, not local tooling.
- **A handler never leaks a traceback.** A malformed config on GET → 500 with a readable message
  (not a severed socket / blank page); a late write ``OSError`` on POST → 500 JSON; a busy bind
  port → a clean actionable ``OSError`` from :meth:`ConfigWebApp.serve`.

Stdlib-only at import time (the repo rule): ``yaml`` and ``http.server`` are imported lazily
inside the functions that need them, so importing this module stays dependency-light.

Module layout
-------------
This module holds the edit write (:func:`apply_edit`) and the HTTP application
(:class:`ConfigWebApp`); the rest lives in focused siblings and is re-exported here so the public
names stay importable from ``riglib.config_web``: :mod:`riglib.config_web_guard` (host/port
constants + the CSRF / DNS-rebinding guards), :mod:`riglib.config_web_model` (the view model),
:mod:`riglib.config_web_render` (HTML rendering). ``apply_edit`` stays here — next to the
``_write_layer`` seam the tests patch — so a patched name is looked up where its consumer lives.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from . import config as cfg
from . import schema
from .config_web_guard import (  # noqa: F401 — re-exported: tests + config_web_service import these here
    DEFAULT_PORT,
    HOST,
    MAX_EDIT_BODY_BYTES,
    is_allowed_host,
    is_cross_site_write,
)
from .config_web_model import (  # noqa: F401 — re-exported public view-model names
    AreaView,
    ConfigModel,
    FieldView,
    build_model,
)
from .config_web_plan import ApplyJobStore
from .config_web_render import (  # noqa: F401 — re-exported public render names
    PAGE_TITLE,
    _control_value,
    _field_control,
    _field_row,
    _fmt_value,
    areas_fragment,
    build_html,
)
from .layers import GLOBAL, REPO

# ── the edit write (routed to the owning layer, fail-closed) ────────────────────────────────
class EditError(ValueError):
    """A rejected edit (unknown key, bad value, validation failure) — surfaced to the browser."""


def _target_path(repo_root: Path, option: schema.Option) -> Path:
    """The file an edit to ``option`` is written to — the owning layer, like the wizard's routing."""
    if option.layer == GLOBAL:
        return cfg.global_config_path()
    return cfg.repo_config_path(repo_root)


def apply_edit(
    repo_root: Path, key: str, raw_value: str, *, include_repo: bool = True
) -> dict[str, Any]:
    """Coerce + write ONE option's value to its owning layer file, fail-closed. Returns a summary.

    ``include_repo=False`` (config-web's Global scope) makes GATE 2 validate ``repo_root``'s
    GLOBAL layer ALONE, with no repo overlay — matching
    :func:`riglib.config_web_plan.build_scope_plan`'s Global branch. Anchoring a Global edit's
    gate at the default ``include_repo=True`` cascade would let an UNRELATED repo's ``rig.yaml``
    at ``repo_root`` (e.g. a stray ``rig.yaml`` at ``$HOME`` for a dotfiles user) reject the edit
    for a reason invisible on the Global tab, which never renders or intends to touch that file
    (caught in review).

    Mirrors ``rig config set`` / the wizard's write path, scoped to a single layer file, with the
    SAME two gates ``_cmd_config_set`` uses so the web UI cannot persist a config the CLI would
    reject:

    1. Resolve the option in the registry (unknown key → :class:`EditError`).
    2. Coerce the raw string to the option's typed value (bad value → :class:`EditError`).
    3. Refuse a REPO edit when ``./rig.yaml`` does not exist yet (same guard as ``config set``:
       editing from ``{}`` would let built-in defaults mutate disk with no committed source of
       truth — ``rig init`` must create the file first). A GLOBAL edit MAY create the machine-wide
       file, so it is not guarded.
    4. GATE 1 — schema validation of the whole edited tree (enum/type checks).
    5. Write the file, then GATE 2 — build the plan from the on-disk cascade (catalog-backed:
       a bad ``agent_tools_source`` / unknown CI item lives here, not in :func:`config.validate`).
       Any failure ROLLS the file back to its exact prior bytes and raises :class:`EditError`, so
       the web UI never leaves a written-but-unreconcilable config behind — identical to the CLI.

    This function itself only edits the DECLARED config; it does not run ``rig apply`` (the same
    separation ``config set --no-apply`` offers). Reconciling to disk is a SEPARATE, explicit step
    the browser offers right after a successful edit — the plan-preview → confirm/skip → live-
    progress apply flow (:mod:`riglib.config_web_plan`), never triggered automatically by a write.
    """
    option = schema.option_for_key(key)
    if option is None:
        raise EditError(f"unknown config option {key!r}")
    try:
        value = schema.coerce(option, raw_value)
    except ValueError as exc:
        raise EditError(str(exc)) from exc

    target = _target_path(repo_root, option)
    # Refuse a repo-local edit when ./rig.yaml is absent — the same guard `config set` enforces:
    # reconciling from {} would let defaults mutate disk with no committed source of truth. (A
    # GLOBAL edit may legitimately create the machine-wide file, so it is exempt.)
    if option.layer == REPO and not target.is_file():
        raise EditError(
            f"no {target} — run `rig init` (or `rig export -o rig.yaml`) first; "
            "config-web edits an existing committed config, it does not bootstrap one."
        )

    # Read the single owning-layer file (an absent GLOBAL file starts from an empty mapping — the
    # wizard creates the global config the same way). NOT the cascade: an edit lands in exactly one
    # file, the layer that owns the option. A MALFORMED existing file raises ConfigError — surface
    # it as a clean EditError (a stale browser tab editing a since-broken file must get JSON, not a
    # severed socket).
    try:
        data = cfg.read_yaml_file(target) if target.is_file() else {}
    except cfg.ConfigError as exc:
        raise EditError(f"cannot read {target}: {exc}") from exc
    # Drop the removed legacy `scope` key (mirrors `_cmd_config_set` + config.load): we re-serialize
    # the WHOLE file, so leaving it would re-emit a setting the schema no longer recognizes — a
    # browser edit must never (re)introduce dead config.
    data.pop("scope", None)
    try:
        cfg.set_path(data, key, value)
    except cfg.ConfigError as exc:  # an existing non-mapping intermediate (e.g. `harness: "a string"`)
        raise EditError(str(exc)) from exc

    # GATE 1 — fail-closed schema validation of the whole edited tree before touching disk. A bad
    # combination (an out-of-range enum coercion let through, a type the validator rejects) aborts
    # here, leaving the file exactly as it was.
    try:
        cfg.validate(data)
    except cfg.ConfigError as exc:
        raise EditError(f"rejected by config validation: {exc}") from exc

    # Capture prior bytes BEFORE writing so the write itself OR GATE 2 can fully ROLL BACK, exactly
    # like `_cmd_config_set`. The file must be byte-identical on ANY failure — so the write is
    # INSIDE the try (a partial/truncated write_text on a full disk is rolled back too, not left).
    original = target.read_text(encoding="utf-8") if target.is_file() else None

    def _rollback() -> None:
        if original is None:
            target.unlink(missing_ok=True)  # we created the file; remove our partial write
        else:
            target.write_text(original, encoding="utf-8")  # restore prior contents

    try:
        _write_layer(target, data, option.layer)
        # For a GLOBAL edit, validate the written global file IN ISOLATION first (mirroring
        # `config set --global`'s `_validate_layer_in_isolation`): the cascade plan below merges a
        # repo overlay over the global layer, which can MASK a catalog-backed error in the global
        # file (a repo rig.yaml overriding the just-broken key). Check it alone so a globally-broken
        # config never persists just because THIS repo happens to override it.
        if option.layer == GLOBAL:
            _validate_layer_in_isolation(target)
        # GATE 2 — build the plan from the on-disk cascade (catalog-backed validation the schema
        # check can't do: a bad agent_tools_source, an unknown CI item). A failure means the edit
        # is unreconcilable → roll the file back and reject.
        _build_plan_gate(repo_root, include_repo=include_repo)
    except OSError:
        # an IO failure (a partial/truncated write_text on a full disk, a permissions error) —
        # roll back the file, then re-raise the OSError so handle_edit maps it to a 500 (a
        # server-side problem, distinct from a user-rejected config which is a 400 EditError).
        _rollback()
        raise
    except Exception as exc:  # noqa: BLE001 — any plan/catalog validation failure: roll back + reject
        _rollback()
        raise EditError(f"rejected by config reconcile check: {exc}") from exc

    return {
        "key": key,
        "value": value,
        "layer": option.layer,
        "file": str(target),
    }


def _write_layer(path: Path, data: dict[str, Any], layer: str) -> None:
    """Serialize ``data`` to its owning-layer file, BYTE-IDENTICAL to what ``rig config set`` writes.

    Both front-ends must round-trip a layer file the same way, so this reuses the SAME
    :class:`~riglib.state.SetupState` serializer ``_cmd_config_set`` uses — not a parallel
    ``yaml.safe_dump`` that would drift:

    - REPO  → :meth:`SetupState.write` — the committed-source-of-truth header + the YAML body.
    - GLOBAL → :meth:`SetupState.to_yaml` — a plain machine-wide dump, no repo header (the global
      ``~/.config/rig/config.yaml`` is not a committed file).

    Like ``config set`` (and the underlying ``yaml.safe_dump``), this does NOT preserve a hand-
    authored file's inline-flow style or comments — the canonical block form is re-emitted. That
    is the existing CLI behaviour, shared here on purpose rather than reinvented. Lazy import of
    state keeps this module import-light.
    """
    from .state import SetupState  # lazy: keeps module import light, mirrors the repo rule

    path.parent.mkdir(parents=True, exist_ok=True)
    state = SetupState.from_dict(data)
    if layer == GLOBAL:
        path.write_text(state.to_yaml(), encoding="utf-8")
    else:
        state.write(path)


def _build_plan_gate(repo_root: Path, *, include_repo: bool = True) -> None:
    """Build the reconcile plan from the on-disk cascade — the second, catalog-backed gate.

    This is GATE 2 in :func:`apply_edit`, mirroring ``_cmd_config_set``: it loads the cascaded
    config (global, plus repo IF ``include_repo`` — INCLUDING the just-written edit), scans the
    agent-tools catalog, and builds the plan. It does NOT execute anything (config-web never runs
    ``rig apply``) — it only proves the edited config can be reconciled. Catalog-backed errors a
    pure-schema :func:`config.validate` cannot catch (a bad ``agent_tools_source``, an unknown
    CI/MCP item) surface here; the caller rolls the file back on any exception. ``include_repo=
    False`` (config-web's Global scope) validates the GLOBAL layer alone, exactly like
    :func:`riglib.config_web_plan.build_scope_plan`'s Global branch — see :func:`apply_edit`'s
    docstring for why this must NOT default to the full cascade there. Heavy modules are imported
    lazily so this module stays import-light.
    """
    from .catalog import Catalog
    from .detect import detect_environment
    from .plan import build

    env = detect_environment(repo_root.resolve())
    loaded = cfg.load(env.repo_root, include_repo=include_repo)
    catalog = Catalog.scan(loaded.agent_tools_source)
    build(loaded, catalog, project_type=env.project_type, stack_preset=env.stack_preset)


def _validate_layer_in_isolation(layer_path: Path) -> None:
    """Validate ONE config file alone (no cascade) — reused from the CLI's `config set --global`.

    A GLOBAL edit is otherwise only checked by the merged cascade (:func:`_build_plan_gate`), where
    a repo ``rig.yaml`` overriding the just-broken global key masks the breakage so a globally-
    broken config still persists. The CLI solves this with ``cli_plan_print._validate_layer_in_isolation``;
    config-web reuses that SAME function (lazy import to avoid an import cycle) rather
    than duplicate the logic, so the two surfaces validate a global edit identically.
    """
    from .cli_plan_print import _validate_layer_in_isolation as _cli_isolated  # lazy: breaks the import cycle

    _cli_isolated(layer_path)


# ── the HTTP application (stdlib http.server) ───────────────────────────────────────────────
@dataclass
class ConfigWebApp:
    """The config-web HTTP application — a MACHINE-WIDE console (rig-cli#310): one browser tab per
    rig-managed repo (discovered via :mod:`riglib.config_web_scopes`) plus the Global scope, a
    drift panel per active tab, and an interactive plan-preview → confirm/skip → live-progress
    apply flow that runs through the SAME shared engine ``rig apply``/``rig init`` use
    (:mod:`riglib.config_web_plan`, never a forked executor).

    ``serve`` binds a localhost ``http.server`` and blocks until interrupted. Every view is
    rebuilt PER REQUEST so a config change (from the browser, the CLI, or a hand-edit) is
    reflected on refresh/poll without a restart. ``repo_root`` is the HOME scope — the repo
    config-web was started against; it is always the first tab (see
    :func:`~riglib.config_web_scopes.discover_scopes`).
    """

    repo_root: Path
    job_store: ApplyJobStore = field(default_factory=ApplyJobStore)

    # ── scope resolution (the allowlist EVERY multi-repo endpoint must go through) ────────────
    def scopes(self) -> list[Any]:
        from .config_web_scopes import discover_scopes

        return discover_scopes(self.repo_root)

    def _resolve_scope(
        self, scope_id: str | None, *, default_on_missing: bool = False, scopes: list[Any] | None = None
    ) -> Any | None:
        """Resolve a request's scope id against the discovered allowlist.

        Returns ``None`` for an id that was given but does not resolve (the caller must refuse
        the request — never fall back silently for an EXPLICIT bad id). When ``scope_id`` is
        absent/empty and ``default_on_missing`` is set, returns the home scope instead (keeps
        ``/edit`` back-compatible with callers that never sent a ``scope`` field). Pass an
        already-fetched ``scopes`` list (e.g. from :meth:`render_page`) to avoid re-reading the
        repository registry twice per request.
        """
        from .config_web_scopes import default_scope, resolve_scope

        if scopes is None:
            scopes = self.scopes()
        if not scope_id:
            return default_scope(scopes) if default_on_missing else None
        return resolve_scope(scopes, scope_id)

    # ── page / fragment rendering ───────────────────────────────────────────────────────────────
    def render_page(self, scope_id: str | None = None) -> bytes:
        from .config_web_scopes import default_scope

        scopes = self.scopes()
        # An unresolvable ?scope= (unlike /edit's explicit-bad-id 400) degrades to the home tab —
        # a GET must always render SOMETHING, never error on a stale/copy-pasted URL. This is
        # NOT the same "default_on_missing" case _resolve_scope handles for a MISSING id (that
        # flag doesn't cover a present-but-invalid one), so the two-step fallback stays explicit
        # here rather than folding into one _resolve_scope call.
        scope = self._resolve_scope(scope_id, scopes=scopes) or default_scope(scopes)
        model = build_model(scope.repo_root or self.repo_root, global_only=scope.is_global)
        return build_html(model, scopes, scope).encode("utf-8")

    def handle_scope_fragment(self, scope_id: str | None) -> tuple[int, dict[str, Any]]:
        """``/api/scope`` — the area-sections HTML for a JS-driven, no-page-reload tab switch."""
        scope = self._resolve_scope(scope_id)
        if scope is None:
            return 404, {"ok": False, "error": f"unknown scope {scope_id!r}"}
        try:
            model = build_model(scope.repo_root or self.repo_root, global_only=scope.is_global)
        except Exception as exc:  # noqa: BLE001 — malformed config on disk, never a raw 500 traceback
            return 500, {"ok": False, "error": f"could not load scope: {exc}"}
        return 200, {"ok": True, "html": areas_fragment(model), "label": scope.label}

    # ── the edit write (routed to the owning layer, fail-closed) ───────────────────────────────
    def handle_edit(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Apply one edit from a POST body. Returns ``(http_status, json_body)``.

        An optional ``scope`` field (rig-cli#310) selects WHICH repo's ``rig.yaml`` a REPO-layer
        edit is validated/written against; absent (older callers, or a GLOBAL-only edit on the
        home scope) defaults to the home scope. A PRESENT but non-string ``scope`` (a malformed
        client payload) is a 400, not a silent fall-through to the default (caught in review).

        The GLOBAL scope may edit ONLY global-layer options: ``apply_edit`` itself routes a
        GLOBAL option to the global file regardless of anchor, but nothing previously stopped a
        Global-tab request from naming a REPO-layer key (``harness.auto_mode`` etc.), which
        ``apply_edit`` would then happily write into ``$HOME/rig.yaml`` if one happened to exist
        — an unrelated, hidden repo config the Global tab never renders and promises no overlay
        of (caught in review). Rejected here, before ``apply_edit`` ever runs.

        The ANCHOR also matters independent of the above: it decides which repo's cascade GATE 2
        (``_build_plan_gate``) validates against. A Global-tab edit anchors at ``$HOME`` WITH
        ``include_repo=False`` (matching :func:`riglib.config_web_plan.build_scope_plan`'s Global
        scope) — anchoring at the full cascade would let a broken/malformed ``$HOME/rig.yaml``
        (or the server's home repo's, before this fix) reject every Global-tab edit for a reason
        invisible on that tab (caught in review, two passes).
        """
        key = payload.get("key")
        raw = payload.get("value")
        scope_id = payload.get("scope")
        if not isinstance(key, str) or not isinstance(raw, str):
            return 400, {"ok": False, "error": "edit requires string 'key' and 'value'"}
        if scope_id is not None and not isinstance(scope_id, str):
            return 400, {"ok": False, "error": "'scope' must be a string"}
        scope = self._resolve_scope(scope_id, default_on_missing=True)
        if scope is None:
            return 400, {"ok": False, "error": f"unknown scope {scope_id!r}"}
        if scope.is_global:
            option = schema.option_for_key(key)
            if option is not None and option.layer != GLOBAL:
                return 400, {
                    "ok": False,
                    "error": f"{key!r} is a repo-layer option — it cannot be edited from the "
                    "Global scope",
                }
        anchor = scope.repo_root if scope.is_repo else Path.home()
        try:
            result = apply_edit(anchor, key, raw, include_repo=not scope.is_global)
        except EditError as exc:
            return 400, {"ok": False, "error": str(exc)}
        except OSError as exc:
            # a LATE failure (permissions, disk full, read-only FS, mkdir denied) after the value
            # passed coercion + validation — return a clean 500 JSON, never sever the connection
            # with a bare traceback the browser shows as "request failed".
            return 500, {"ok": False, "error": f"could not write config file: {exc}"}
        return 200, {
            "ok": True,
            "key": result["key"],
            "value": _fmt_value(result["value"]),
            "control_value": _control_value(result["key"], result["value"]),
            "layer": result["layer"],
            "file": result["file"],
        }

    # ── drift panel ──────────────────────────────────────────────────────────────────────────
    def handle_drift(self, scope_id: str | None) -> tuple[int, dict[str, Any]]:
        """``/api/drift`` — the SAME two-way drift engine ``rig status`` uses, for one scope."""
        from .config_web_plan import build_scope_plan, compute_scope_drift

        scope = self._resolve_scope(scope_id)
        if scope is None:
            return 404, {"ok": False, "error": f"unknown scope {scope_id!r}"}
        try:
            scope_plan = build_scope_plan(scope)
            payload = compute_scope_drift(scope_plan)
        except Exception as exc:  # noqa: BLE001 — a broken catalog/config must not 500-traceback
            return 500, {"ok": False, "error": f"drift check failed: {exc}"}
        payload["ok"] = True
        return 200, payload

    # ── plan preview + interactive apply ────────────────────────────────────────────────────────
    def handle_plan_preview(self, scope_id: str | None) -> tuple[int, dict[str, Any]]:
        """``/api/plan`` — the SAME plan.build() `rig apply info` uses, tagged per action."""
        from .config_web_plan import NoConfigLayerError, build_scope_plan, preview_payload

        scope = self._resolve_scope(scope_id)
        if scope is None:
            return 404, {"ok": False, "error": f"unknown scope {scope_id!r}"}
        try:
            scope_plan = build_scope_plan(scope)
            payload = preview_payload(scope_plan)
        except NoConfigLayerError as exc:
            return 400, {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — a broken catalog/config must not 500-traceback
            return 500, {"ok": False, "error": f"could not build plan: {exc}"}
        payload["ok"] = True
        return 200, payload

    def handle_apply(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """``/api/apply`` — start an apply job for the SELECTED actions of a previewed plan.

        Verifies the echoed ``fingerprint`` against a freshly rebuilt plan (409 on mismatch — the
        config changed since the browser previewed it, nothing is applied) and that no OTHER
        apply job is running process-wide (409). The accepted job runs through the real
        ``riglib.actions.runner.run_plan`` in a background thread — see
        :class:`~riglib.config_web_plan.ApplyJobStore`.
        """
        from .config_web_plan import ApplyBusyError, NoConfigLayerError, PlanStaleError

        scope_id = payload.get("scope")
        fingerprint = payload.get("fingerprint")
        skip_keys = payload.get("skip_keys")
        if not isinstance(scope_id, str) or not isinstance(fingerprint, str):
            return 400, {"ok": False, "error": "apply requires string 'scope' and 'fingerprint'"}
        if skip_keys is None:
            skip_keys = []
        if not isinstance(skip_keys, list) or not all(isinstance(k, str) for k in skip_keys):
            return 400, {"ok": False, "error": "'skip_keys' must be a list of strings"}
        scope = self._resolve_scope(scope_id)
        if scope is None:
            return 404, {"ok": False, "error": f"unknown scope {scope_id!r}"}
        try:
            job = self.job_store.start(
                scope, expected_fingerprint=fingerprint, skip_keys=set(skip_keys)
            )
        except (PlanStaleError, ApplyBusyError) as exc:
            return 409, {"ok": False, "error": str(exc)}
        except NoConfigLayerError as exc:
            return 400, {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — a broken catalog/config must not 500-traceback
            return 500, {"ok": False, "error": f"could not start apply: {exc}"}
        return 200, {"ok": True, "job_id": job.id}

    def handle_apply_status(self, job_id: str | None) -> tuple[int, dict[str, Any]]:
        """``/api/apply/status`` — poll one apply job's live per-action progress."""
        if not job_id:
            return 400, {"ok": False, "error": "missing 'job' query parameter"}
        job = self.job_store.get(job_id)
        if job is None:
            return 404, {"ok": False, "error": f"unknown apply job {job_id!r}"}
        return 200, {
            "ok": True,
            "scope": job.scope_id,
            "fingerprint": job.fingerprint,
            "done": job.done,
            "error": job.error,
            "actions": [
                {
                    "key": row.key, "kind": row.kind, "category": row.category, "item": row.item,
                    "describe": row.describe, "status": row.status, "detail": row.detail,
                }
                for row in job.actions
            ],
        }

    def make_handler(self) -> type:
        """Build the ``BaseHTTPRequestHandler`` subclass bound to this app's GET/POST behaviour.

        Extracted from :meth:`serve` so the live-socket TEST drives the SAME handler the server
        runs — path whitelist (``/`` / ``/index.html`` / ``/edit``, else 404), the CSRF guard, the
        ``application/json`` requirement, the body cap, the GET/POST error mapping. A test that
        hand-rolled its own handler would leave every one of those guards uncovered (the bug a
        prior version of the test had). ``http.server`` is imported lazily, keeping the module
        import-light.
        """
        import http.server

        app = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            # Bound the time any one request can hold the SINGLE-threaded server: a slow-loris
            # client trickling a (capped-size) body would otherwise stall every other request
            # forever. BaseHTTPRequestHandler.timeout sets the socket read timeout per connection.
            timeout = 15

            def _send(
                self, code: int, body: bytes, ctype: str, *, no_store: bool = False
            ) -> None:
                # On ANY non-2xx we may not have drained the request body (a rejected oversize/415
                # POST returns before reading it). With HTTP/1.1 keep-alive an unread body would be
                # reparsed as the NEXT request line and desync the connection — so close it on every
                # error response. (The page only issues one fetch per edit, so this costs nothing.)
                if code >= 300:
                    self.close_connection = True
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if no_store:
                    self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _send_json(self, code: int, body: dict[str, Any]) -> None:
                # Every JSON response is either always-fresh (drift/plan/apply-status) or an
                # explicit edit result — never something a browser should cache on its own
                # heuristics, especially the 500ms-polled /api/apply/status (found in review).
                self._send(
                    code, json.dumps(body).encode("utf-8"), "application/json; charset=utf-8",
                    no_store=True,
                )

            def _reject_foreign_host(self) -> bool:
                # DNS-rebinding guard on EVERY request (GET included): a foreign Host that resolves
                # to loopback is refused, so an attacker page can't read the config HTML or POST an
                # edit under a rebind. Returns True (and sends 403) when the host is not loopback.
                if not is_allowed_host(self.headers):
                    self._send(403, b"forbidden host", "text/plain; charset=utf-8")
                    return True
                return False

            def _route_and_query(self) -> tuple[str, dict[str, str]]:
                from urllib.parse import parse_qs, urlsplit

                split = urlsplit(self.path)
                params = {k: v[-1] for k, v in parse_qs(split.query).items()}
                return split.path, params

            def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
                if self._reject_foreign_host():
                    return
                route, params = self._route_and_query()
                if route in ("/", "/index.html"):
                    try:
                        page = app.render_page(params.get("scope"))
                    except Exception as exc:  # noqa: BLE001
                        # a malformed/invalid rig.yaml makes cfg.load raise — return a readable
                        # 500 instead of letting the exception escape do_GET (which closes the
                        # socket with no response, leaving the user a blank page and no diagnostic).
                        msg = f"config-web could not load the config: {exc}"
                        self._send(500, msg.encode("utf-8"), "text/plain; charset=utf-8")
                        return
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                if route == "/api/scope":
                    code, body = app.handle_scope_fragment(params.get("scope"))
                    self._send_json(code, body)
                    return
                if route == "/api/drift":
                    # /api/drift is a GET (so it's easy for the JS to call on tab-load), but it
                    # triggers the SAME heavy compute as /api/plan (a full catalog scan + plan
                    # build + disk walk) — apply the SAME CSRF guard the mutating POSTs use, so a
                    # hostile page can't turn a blind cross-site GET into a compute-DoS against
                    # this single-threaded server (found in review). A local CLI client (curl,
                    # the tests) sends neither guard header and is unaffected.
                    if self._is_cross_site():
                        self._send_json(403, {"ok": False, "error": "cross-site request refused"})
                        return
                    code, body = app.handle_drift(params.get("scope"))
                    self._send_json(code, body)
                    return
                if route == "/api/apply/status":
                    code, body = app.handle_apply_status(params.get("job"))
                    self._send_json(code, body)
                    return
                self._send(404, b"not found", "text/plain; charset=utf-8")

            def _is_cross_site(self) -> bool:
                # CSRF / localhost-rebinding guard: refuse a write that a browser flags as
                # cross-site, so a hostile page the user visits can't drive one (see
                # is_cross_site_write). A local CLI client (curl/test) sends no such header. Pass
                # our bound port so an Origin on a DIFFERENT loopback port (another local service)
                # is a mismatch, not a match — same-origin is scheme+host+port.
                # The (host, port) the socket actually bound to. The stdlib types server_address
                # loosely (a generic socket address: tuple | str | Buffer); for our AF_INET TCP
                # server it is always a (host, port) tuple, so cast to that shape and read port[1].
                host_port = cast("tuple[str, int]", self.server.server_address)
                bound_port = host_port[1]
                return is_cross_site_write(self.headers, bound_port=bound_port)

            def _validated_content_length(self) -> int | None:
                """Parse + validate Content-Length. Returns ``None`` (and already sent 400/413)
                on a malformed/negative/oversize value — the caller must just return without
                sending anything else. Shared by :meth:`_drain_body` and :meth:`_read_json_body`
                so the cap/validation logic lives in exactly one place (found duplicated, in
                review).

                Parsed defensively: a missing header is 0; a MALFORMED value ("abc") must not let
                ``int()`` escape the handler; a NEGATIVE value must be rejected
                (``rfile.read(-1)`` reads until EOF — it would bypass the size cap AND block the
                single-threaded server on a slow/withheld body). Fail closed → 400/413.
                """
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                except ValueError:
                    self._send_json(400, {"ok": False, "error": "invalid Content-Length"})
                    return None
                if length < 0:
                    self._send_json(400, {"ok": False, "error": "invalid Content-Length"})
                    return None
                if length > MAX_EDIT_BODY_BYTES:
                    # reject (don't allocate) an implausibly large body — a memory-DoS guard.
                    self._send_json(413, {"ok": False, "error": "request body too large"})
                    return None
                return length

            def _reject_nonempty_body(self) -> bool:
                """For a route that expects NO body at all: validate Content-Length and reject a
                non-empty one WITHOUT reading it (never call ``rfile.read`` here — see the caller
                for why). Returns ``False`` (having already sent 400/413) on a malformed,
                oversize, OR merely non-empty Content-Length; ``True`` only for an absent/zero
                one. The caller must just return without sending anything else on ``False``.
                """
                length = self._validated_content_length()
                if length is None:
                    return False
                if length:
                    self._send_json(400, {"ok": False, "error": "this route does not accept a request body"})
                    return False
                return True

            def _read_json_body(self) -> tuple[bool, dict[str, Any]]:
                """Read + parse a JSON POST body under the shared guards. Returns ``(ok, payload)``.

                On failure this ALREADY sent the error response (400/413/415) and the second
                element is always ``{}`` (never a real payload) — the caller must just return
                without sending anything else.
                """
                ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if ctype != "application/json":
                    self._send_json(
                        415, {"ok": False, "error": "Content-Type must be application/json"}
                    )
                    return False, {}
                length = self._validated_content_length()
                if length is None:
                    return False, {}
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send_json(400, {"ok": False, "error": "invalid JSON body"})
                    return False, {}
                return True, (payload if isinstance(payload, dict) else {})

            def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
                if self._reject_foreign_host():
                    return
                route, params = self._route_and_query()
                if route not in ("/edit", "/api/plan", "/api/apply"):
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                # every mutating/compute-triggering POST shares the same CSRF guard — a hostile
                # page the user visits must not be able to drive an edit, a plan build, OR an
                # apply just by getting the browser to POST here.
                if self._is_cross_site():
                    self._send_json(403, {"ok": False, "error": "cross-site write refused"})
                    return
                if route == "/api/plan":
                    # /api/plan carries no body (the JS client sends none). REJECT rather than
                    # drain a non-empty one: draining means calling the blocking `rfile.read
                    # (length)`, which a client can trickle one byte every few seconds to hold the
                    # single-threaded server's only thread hostage well past the 15s PER-READ
                    # socket timeout (an inactivity timeout, not a whole-request deadline) — found
                    # in review as a slow-loris DoS the earlier "drain it" fix reintroduced. A 400
                    # response closes the connection (`_send`'s own `code >= 300` rule), so no
                    # keep-alive desync risk from leaving a body unread here either.
                    if not self._reject_nonempty_body():
                        return
                    code, body = app.handle_plan_preview(params.get("scope"))
                    self._send_json(code, body)
                    return
                ok, payload = self._read_json_body()
                if not ok:
                    return
                if route == "/edit":
                    code, body = app.handle_edit(payload)
                else:  # /api/apply
                    code, body = app.handle_apply(payload)
                self._send_json(code, body)

            def log_message(self, *args: Any) -> None:  # silence the default stderr access log
                return

        return _Handler

    def serve(self, *, port: int = DEFAULT_PORT, open_browser: bool = False) -> int:
        """Bind localhost and serve until interrupted. Returns the bound port. Blocks (foreground).

        ``port=0`` lets the OS pick a free port (used by tests). ``http.server`` / ``webbrowser``
        are imported lazily here so the module stays import-light.
        """
        import http.server
        import threading
        import webbrowser

        try:
            httpd = http.server.HTTPServer((HOST, port), self.make_handler())
        except OSError as exc:
            # the most common bind failure is a busy port (EADDRINUSE) — surface a clean,
            # actionable message instead of an uncaught traceback escaping the daemon/serve verb.
            raise OSError(
                f"config-web could not bind {HOST}:{port}: {exc}. "
                f"Is another instance already running? Try a different --port, or "
                f"`rig config-web status` / `stop`."
            ) from exc
        bound = httpd.server_address[1]
        url = f"http://{HOST}:{bound}/"
        print(f"rig config-web — serving at {url}  (Ctrl-C to stop)")
        # keep the open-browser Timer so a Ctrl-C inside its 0.4s window can cancel it — otherwise
        # it fires after server_close() and opens a tab pointing at a now-dead URL.
        timer = threading.Timer(0.4, lambda: webbrowser.open(url)) if open_browser else None
        if timer is not None:
            timer.start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")
        finally:
            if timer is not None:
                timer.cancel()
            httpd.server_close()
        return bound
