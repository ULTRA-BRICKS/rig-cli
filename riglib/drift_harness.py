"""Drift checks for harness settings: mode, pi extension, execpolicy, ecosystem readme, guard, approval and per-harness knobs."""

from __future__ import annotations

import json
from pathlib import Path

from .actions import fsutil
from . import ecosystem_readme
from .actions.harness_extras import pi_extension_policy_json
from .actions.harness_mode import (
    desired_harness_value,
    harness_settings_file,
    self_merge_allow_present,
    self_merge_legacy_allow_present,
    self_merge_permissions_addable,
)
from .actions.managed_blocks import (
    ECOSYSTEM_README_BEGIN_MARKER,
    ECOSYSTEM_README_END_MARKER,
    EXECPOLICY_BEGIN_MARKER,
    EXECPOLICY_END_MARKER,
    _splice_managed_block,
    ecosystem_readme_block_text,
    ecosystem_readme_file,
    execpolicy_block_text,
    execpolicy_rules_file,
)
from .plan import Action
from .drift_model import DriftItem, DriftReport


def _check_harness(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured harness auto/permission mode and the file on disk.

    missing  — the settings file or the managed key is absent.
    modified — the managed key on disk has a different value than the config declares
               (someone flipped auto-mode off, or the harness rewrote it). apply converges.
    Only the single managed key is compared; other settings in the file are irrelevant here.
    """
    if str(action.options.get("kind", "claude-code")) != "claude-code":
        _check_harness_mode(action, report)
        return
    (section, key), value = desired_harness_value(action)
    config_file = harness_settings_file(action)
    if not config_file.is_file():
        report.items.append(
            DriftItem("missing", "harness", action.item, config_file, "harness settings file not written")
        )
        return
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except ValueError:
        report.items.append(
            DriftItem("modified", "harness", action.item, config_file, "harness settings file is malformed JSON")
        )
        return
    sect = data.get(section, {}) if isinstance(data, dict) else {}
    current = sect.get(key) if isinstance(sect, dict) else None
    if current is None:
        report.items.append(
            DriftItem("missing", "harness", action.item, config_file, f"{section}.{key} not set")
        )
    elif current != value:
        report.items.append(
            DriftItem(
                "modified", "harness", action.item, config_file,
                f"{section}.{key} is '{current}', config declares '{value}'",
            )
        )
    # the self-merge provisioning is two more managed things in the same file (auto-mode only): the
    # HARD ``permissions.allow`` ship rules and the SOFT ``autoMode.allow`` carve-out. Skip both for
    # a non-dict root — the mode-key path above already reported that file, and a second row for the
    # same malformed file would just be noise.
    if action.options.get("self_merge") and isinstance(data, dict):
        _check_self_merge(action, report, config_file, data, current == value)


def _check_self_merge(
    action: Action, report: DriftReport, config_file: Path, data: dict, mode_converged: bool
) -> None:
    # The ACTIVE ship rules are expected ONLY when auto is actually in effect on disk (defaultMode
    # == the declared auto value). When the mode is non-auto — a skip that left it interactive, or
    # a hand-flip — apply does NOT write the ship rules, so don't flag them missing; the mode-key
    # drift row above already signals non-convergence (codex #159 P2). `addable` (not
    # `not present`) so a MALFORMED permissions.allow — already a shape `modified` from the
    # permissions check — doesn't get an inaccurate 'absent' row either.
    if mode_converged and self_merge_permissions_addable(data):
        detail = "self-merge ship rules absent from permissions.allow/deny (config declares self_merge)"
        if self_merge_legacy_allow_present(data):
            detail += "; legacy never-matching Bash(*/pr-ship.sh:*) / Bash(*/ship.sh:*) allow rules present"
        report.items.append(DriftItem("missing", "harness", action.item, config_file, detail))
    if not self_merge_allow_present(data):
        report.items.append(
            DriftItem(
                "missing", "harness", action.item, config_file,
                "self-merge carve-out absent from autoMode.allow (config declares self_merge)",
            )
        )


def _check_harness_mode(action: Action, report: DriftReport) -> None:
    """Drift for a non-claude kind's mode key — shares the runner's probe so apply and status agree.

    missing  — the harness file or the managed key is absent (apply writes it).
    modified — the file is malformed / mis-shaped / holds a non-plain value (apply leaves it — a
               hand fix), or the key carries a different value (apply converges it).
    """
    from .actions.harness_mode import desired_harness_mode, harness_mode_probe

    key_path, value = desired_harness_mode(action)
    dotted = ".".join(key_path)
    path = action.target
    state, current = harness_mode_probe(action)
    if state == "absent":
        report.items.append(DriftItem("missing", "harness", action.item, path, f"{dotted} not set (harness config file not written)"))
    elif state == "missing":
        report.items.append(DriftItem("missing", "harness", action.item, path, f"{dotted} not set"))
    elif state == "malformed":
        report.items.append(DriftItem("modified", "harness", action.item, path, "harness config file is malformed — fix it by hand"))
    elif state == "unverifiable":
        report.items.append(DriftItem("modified", "harness", action.item, path, f"{dotted} cannot be verified: {current}"))
    elif state == "shape":
        report.items.append(DriftItem("modified", "harness", action.item, path, f"{dotted}: '{current}' is not an object — fix it by hand"))
    elif state == "conflict":
        report.items.append(DriftItem("modified", "harness", action.item, path, f"{current} is not a plain value (rig wants '{value}') — fix it by hand"))
    elif current != value:
        report.items.append(DriftItem("modified", "harness", action.item, path, f"{dotted} is '{current}', config declares '{value}'"))


def _check_pi_extension(action: Action, report: DriftReport) -> None:
    """Flag drift for the pi ``permission-guard`` extension provisioning (two managed artifacts).

    missing  — the installed extension dir is absent, OR the policy file is absent. ``rig apply``
               installs/writes them.
    modified — the installed extension dir differs from the agent-tools source (stale copy), OR the
               policy file's content differs from the rig-desired JSON (hand-edited/outdated).

    Uses the SAME serializer (:func:`pi_extension_policy_json`) and identity check the install
    handler uses, so apply and status never disagree on what rig manages.
    """
    ext_name = str(action.options.get("extension", "permission-guard"))
    if not action.target.is_dir():
        report.items.append(
            DriftItem("missing", "permissions", action.item, action.target, f"pi extension '{ext_name}' not installed")
        )
    elif not fsutil.dirs_identical(action.source, action.target):
        report.items.append(
            DriftItem(
                "modified", "permissions", action.item, action.target,
                f"installed pi extension '{ext_name}' differs from the agent-tools source",
            )
        )
    policy_file = Path(action.options["policy_file"])
    desired = pi_extension_policy_json(action)
    # No separate `is_file()` pre-check: `read_text()` itself raises `FileNotFoundError` when the
    # path is absent, so checking first would only duplicate the "missing" DriftItem AND open a
    # TOCTOU window (a delete between the check and the read). One read, one set of outcomes: a
    # path that IS a directory now reports "unreadable" via the OSError branch below instead of
    # a bogus "missing" — arguably more accurate anyway (something IS there, just not a file).
    try:
        current = policy_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        report.items.append(
            DriftItem("missing", "permissions", action.item, policy_file, "pi permission policy file not written")
        )
        return
    except (OSError, UnicodeDecodeError) as exc:
        # The permission-guard extension itself fails CLOSED on an unreadable/invalid policy file
        # (falls back to its baked baseline) — drift must report the same broken artifact, not
        # crash `rig status`/config-web out from under the caller.
        report.items.append(
            DriftItem("modified", "permissions", action.item, policy_file, f"pi permission policy file unreadable: {exc}")
        )
        return
    if current != desired:
        report.items.append(
            DriftItem("modified", "permissions", action.item, policy_file, "pi permission policy file differs from rig-desired")
        )


def _check_execpolicy(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured codex execpolicy block and the on-disk .rules file.

    missing  — the .rules file is absent, or holds no rig-managed block (apply APPENDS it).
    modified — the managed block is stale (apply rewrites it), OR the file has unbalanced/misordered
               markers rig refuses to rewrite (a HAND fix — apply hard-errors, does not reconcile).

    Routes through the SAME :func:`_splice_managed_block` resolver apply uses, so the drift verdict
    and its message can never overstate what apply will actually do."""
    desired = execpolicy_block_text(action)
    path = execpolicy_rules_file(action)
    if not path.is_file():
        report.items.append(
            DriftItem("missing", "permissions", action.item, path, "execpolicy .rules file not written")
        )
        return
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            content = fh.read()
    except OSError as exc:
        report.items.append(
            DriftItem("modified", "permissions", action.item, path, f"cannot read {path}: {exc}")
        )
        return
    _, state, _detail = _splice_managed_block(
        content, desired, EXECPOLICY_BEGIN_MARKER, EXECPOLICY_END_MARKER
    )
    if state == "ok":
        return  # in sync
    if state == "create":
        report.items.append(
            DriftItem("missing", "permissions", action.item, path, "no rig-managed execpolicy block (apply adds it)")
        )
    elif state == "conflict":
        report.items.append(
            DriftItem("modified", "permissions", action.item, path,
                      "unbalanced/misordered rig-managed execpolicy markers — reconcile by hand, then re-run")
        )
    else:  # update
        report.items.append(
            DriftItem("modified", "permissions", action.item, path,
                      "rig-managed execpolicy block is stale (apply rewrites it)")
        )


def _check_ecosystem_readme(action: Action, report: DriftReport) -> None:
    """Flag drift between the canonical README Ecosystem block and the on-disk README (rig-cli#308).

    missing  — the README is absent, or holds no rig-managed block and no unmarked
               ``## Ecosystem`` heading either (apply APPENDS the block cleanly).
    modified — the managed block is stale (apply rewrites it), the file has unbalanced/misordered
               markers, OR it already has a bare ``## Ecosystem`` heading with no rig markers (all
               three are a HAND fix — apply hard-errors, does not reconcile or duplicate).

    Routes through the SAME :func:`_splice_managed_block` resolver apply uses, so the drift verdict
    and its message can never overstate what apply will actually do."""
    desired = ecosystem_readme_block_text(action)
    path = ecosystem_readme_file(action)
    if not path.is_file():
        report.items.append(
            DriftItem("missing", "ecosystem_readme", action.item, path, "README Ecosystem block not written")
        )
        return
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            content = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        # a README target is a pre-existing, arbitrary user-authored file — non-UTF-8 bytes are a
        # real, reachable case here (unlike execpolicy's own rig-generated .rules file), so this
        # must report actionable drift instead of letting rig status crash.
        report.items.append(
            DriftItem("modified", "ecosystem_readme", action.item, path, f"cannot read {path}: {exc}")
        )
        return
    _, state, _detail = _splice_managed_block(
        content, desired, ECOSYSTEM_README_BEGIN_MARKER, ECOSYSTEM_README_END_MARKER
    )
    if state == "ok":
        return  # in sync
    if state == "create":
        if ecosystem_readme.has_unmarked_ecosystem_heading(content):
            report.items.append(
                DriftItem("modified", "ecosystem_readme", action.item, path,
                          "an unmarked '## Ecosystem' section already exists — apply refuses to "
                          "append a duplicate; wrap it in the rig markers by hand, then re-run")
            )
        else:
            report.items.append(
                DriftItem("missing", "ecosystem_readme", action.item, path, "no rig-managed Ecosystem block (apply adds it)")
            )
    elif state == "conflict":
        report.items.append(
            DriftItem("modified", "ecosystem_readme", action.item, path,
                      "unbalanced/misordered rig-managed ecosystem-block markers — reconcile by hand, then re-run")
        )
    else:  # update
        report.items.append(
            DriftItem("modified", "ecosystem_readme", action.item, path,
                      "rig-managed Ecosystem block is stale (apply rewrites it)")
        )


def _check_harness_guard(action: Action, report: DriftReport) -> None:
    """Flag drift between the generated omp guard extension and the on-disk file.

    missing  — the extension file is absent (apply writes it, atomically).
    modified — the content differs (apply backs up and regenerates), OR the guard's own
               runtime self-check fired: the ``.incompatible`` marker next to it means omp's
               event contract changed and the guard is blocking fail-closed — a loud,
               self-reported signal, not a silent no-op.
    """
    from .actions.harness_guard import desired_guard_content, guard_extension_file
    from .omp_guard import INCOMPATIBLE_MARKER_NAME

    path = guard_extension_file(action)
    if not path.is_file():
        report.items.append(
            DriftItem("missing", "permissions", action.item, path, "guard extension not written")
        )
        return
    try:
        current = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        report.items.append(
            DriftItem("modified", "permissions", action.item, path, f"cannot read {path}: {exc}")
        )
        return
    if current != desired_guard_content(action):
        report.items.append(
            DriftItem("modified", "permissions", action.item, path,
                      "guard extension differs (apply backs up and regenerates)")
        )
    marker = path.parent / INCOMPATIBLE_MARKER_NAME
    if marker.exists():
        report.items.append(
            DriftItem("modified", "permissions", action.item, marker,
                      "guard self-check fired: incompatible omp event shape — the guard is "
                      "blocking fail-closed; re-run `rig apply` and check the omp version")
        )


def _check_harness_approval(action: Action, report: DriftReport) -> None:
    """Flag drift between the desired approval posture and the harness config.

    missing  — the config file is absent, or a managed key is not set (apply adds it).
    modified — the file is malformed, a parent is non-dict, or a managed key carries a
               DIFFERENT value (the user's call — apply never clobbers; surfaced so status
               isn't a silent green).
    A matching value WITHOUT rig's receipt is 'compatible unmanaged' — first-class, NOT drift.
    Shares :func:`_approval_lookup` with the install handler so apply and status can never
    disagree on what a key state means.
    """
    from .actions.harness_approval import (
        approval_config_file,
        approval_desired_keys,
        approval_posture_relaxes,
        approval_receipt_file,
        read_approval_receipt,
        receipt_installed_values,
    )
    from .actions.shared import approval_lookup

    path = approval_config_file(action)
    data = _load_approval_mapping(action, path, report)
    if data is None:
        return
    try:
        keys = approval_desired_keys(action)
    except ValueError as exc:
        report.items.append(DriftItem("modified", "permissions", action.item, path, str(exc)))
        return
    # the SAME receipt-ownership decision the writer makes: a key still holding what rig installed
    # is converged by apply, so status must not tell the user to change it by hand
    rig_installed = receipt_installed_values(read_approval_receipt(approval_receipt_file(action)))
    for key_path, scalar in keys:
        state, value = approval_lookup(data, key_path)
        item = _approval_key_item(action, path, ".".join(key_path), state, value, scalar, rig_installed)
        if item is not None:
            report.items.append(item)
    # Correlate the interlock's two halves: a RELAXED posture with the guard belt gone or
    # drifted is the most dangerous combined state — never render it as routine drift.
    if approval_posture_relaxes(action) and all(approval_lookup(data, kp)[1] == scalar for kp, scalar in keys):
        item = _approval_guard_item(action)
        if item is not None:
            report.items.append(item)


def _load_approval_mapping(action: Action, path: Path, report: DriftReport) -> dict | None:
    """Parse the approval config to a mapping (empty file -> {}); report drift and return None otherwise."""
    import yaml  # lazy, like the rest of the config stack

    if not path.is_file():
        report.items.append(
            DriftItem("missing", "permissions", action.item, path, "approval config not written")
        )
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        report.items.append(
            DriftItem("modified", "permissions", action.item, path,
                      "approval config is unreadable (permissions/encoding) — fix it by hand")
        )
        return None
    except yaml.YAMLError:
        report.items.append(
            DriftItem("modified", "permissions", action.item, path,
                      "approval config is malformed YAML")
        )
        return None
    if not isinstance(data, dict) and data is not None:
        # apply HARD-ERRORS here ("fix it by hand") — drift must not promise an apply-set.
        report.items.append(
            DriftItem("modified", "permissions", action.item, path,
                      "approval config is not a YAML mapping — fix it by hand")
        )
        return None
    return data if isinstance(data, dict) else {}


def _approval_key_item(
    action: Action, path: Path, dotted: str, state: str, value: object, scalar: object,
    rig_installed: dict,
) -> DriftItem | None:
    """The drift row for one managed approval key, or None when it converged / is compatible."""
    if state == "shape":
        return DriftItem("modified", "permissions", action.item, path,
                         f"{dotted} parent '{value}' is not an object — fix it by hand")
    if state == "missing":
        return DriftItem("missing", "permissions", action.item, path,
                         f"approval posture {dotted} not provisioned (apply sets {scalar!r})")
    if value == scalar:
        return None
    if value is not None and rig_installed.get(dotted) == value:
        return DriftItem("modified", "permissions", action.item, path,
                         f"{dotted} is {value!r} (rig-installed; config now declares {scalar!r} — "
                         "apply converges it; on_conflict=skip leaves it)")
    return DriftItem("modified", "permissions", action.item, path,
                     f"{dotted} is {value!r} (rig wants {scalar!r}; apply never clobbers — "
                     "change it by hand or remove the key)")


def _approval_guard_item(action: Action) -> DriftItem | None:
    """Drift row when the relaxed posture's guard belt is missing or drifted."""
    # the interlock's other half, via the SAME resolver the runner uses — the most
    # dangerous combined state must not depend on the plan's option wiring.
    from .actions.harness_guard import expected_guard_content, resolve_guard_target

    gpath = resolve_guard_target(action)
    if gpath is None:
        return None
    try:
        gcontent = gpath.read_text(encoding="utf-8", errors="replace") if gpath.is_file() else None
    except OSError:
        gcontent = None
    if gcontent == expected_guard_content(gpath):
        return None
    return DriftItem("modified", "permissions", action.item, gpath,
                     "approval posture is relaxed but the guard belt is missing or "
                     "drifted — enforcement is OFF (re-run `rig apply`)")


def _check_omp_idle_ttl(action: Action, report: DriftReport) -> None:
    """Flag missing/mismatched task.agentIdleTtlMs against the resolved keepalive write."""
    import yaml
    from .actions.harness_settings import OMP_IDLE_TTL_KEY, OMP_IDLE_TTL_VALUE, _idle_ttl_parking_disabled
    from .actions.shared import approval_lookup

    path = action.target
    dotted = ".".join(OMP_IDLE_TTL_KEY)
    desired_value = action.options.get("ms", OMP_IDLE_TTL_VALUE)
    if not path.is_file():
        report.items.append(
            DriftItem("missing", "harness", action.item, path,
                      f"{dotted} not provisioned (apply sets {desired_value!r})")
        )
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.items.append(
            DriftItem("modified", "harness", action.item, path,
                      f"cannot read {path}: {exc}")
        )
        return
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        report.items.append(
            DriftItem("modified", "harness", action.item, path,
                      "omp config is malformed YAML")
        )
        return
    if loaded is not None and not isinstance(loaded, dict):
        report.items.append(
            DriftItem("modified", "harness", action.item, path,
                      "omp config is not a YAML mapping — fix it by hand")
        )
        return
    data = loaded if isinstance(loaded, dict) else {}
    state, value = approval_lookup(data, OMP_IDLE_TTL_KEY)
    if state == "shape":
        report.items.append(
            DriftItem("modified", "harness", action.item, path,
                      f"{dotted} parent '{value}' is not an object — fix it by hand")
        )
    elif state == "missing":
        report.items.append(
            DriftItem("missing", "harness", action.item, path,
                      f"{dotted} not provisioned (apply sets {desired_value!r})")
        )
    elif not (_idle_ttl_parking_disabled(value) or value == desired_value):
        # A non-positive on-disk value is a standing "parking disabled" choice apply
        # never overwrites (see _do_provision_omp_idle_ttl) regardless of desired_value
        # -- drift must treat it as clean too, or it would report permanent unfixable
        # drift for a value apply will never touch.
        report.items.append(
            DriftItem("modified", "harness", action.item, path,
                      f"{dotted} is {value!r} (rig wants {desired_value!r}; apply never "
                      "clobbers — change it by hand or remove the key)")
        )


def _check_codex_skills_budget(action: Action, report: DriftReport) -> None:
    """Flag missing/mismatched skills.max_context_tokens against the resolved codex write.

    A ``"missing"`` key is reported as ``"missing (apply sets N)"`` ONLY when apply will actually
    write it. Two on-disk shapes apply refuses to text-edit (a genuine TOML multiline string, or
    ``skills`` already defined via dotted keys/an inline table — see
    :func:`riglib.actions.harness_settings._skills_budget_unmanageable_reason`, shared with the writer) are
    reported as ``"modified"`` with the SAME remediation instead — otherwise ``rig status`` would
    promise a convergence ``rig apply`` never performs, looping forever (review finding: apply and
    drift must classify these identically, not just agree on the desired VALUE).
    """
    from .actions.harness_settings import (
        CODEX_SKILLS_BUDGET_KEY,
        CODEX_SKILLS_BUDGET_VALUE,
        _skills_budget_unmanageable_reason,
    )
    from .actions.shared import approval_lookup
    from .actions.toml_edit import _toml_parser

    path = action.target
    dotted = ".".join(CODEX_SKILLS_BUDGET_KEY)
    desired_value = action.options.get("max_context_tokens", CODEX_SKILLS_BUDGET_VALUE)
    tomllib = _toml_parser()
    if tomllib is None:
        report.items.append(
            DriftItem("modified", "harness", action.item, path,
                      "no TOML parser on this Python — cannot verify skills.max_context_tokens")
        )
        return
    if not path.is_file():
        report.items.append(
            DriftItem("missing", "harness", action.item, path,
                      f"{dotted} not provisioned (apply sets {desired_value!r})")
        )
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.items.append(
            DriftItem("modified", "harness", action.item, path,
                      f"cannot read {path}: {exc}")
        )
        return
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        report.items.append(
            DriftItem("modified", "harness", action.item, path,
                      "codex config.toml is malformed TOML")
        )
        return
    state, value = approval_lookup(data, CODEX_SKILLS_BUDGET_KEY)
    if state == "shape":
        report.items.append(
            DriftItem("modified", "harness", action.item, path,
                      f"{dotted} parent '{value}' is not a table — fix it by hand")
        )
    elif state == "missing":
        reason = _skills_budget_unmanageable_reason(text, data)
        if reason:
            report.items.append(
                DriftItem("modified", "harness", action.item, path,
                          f"{reason}; add `{dotted} = {desired_value}` by hand "
                          "(apply will never do this automatically)")
            )
        else:
            report.items.append(
                DriftItem("missing", "harness", action.item, path,
                          f"{dotted} not provisioned (apply sets {desired_value!r})")
            )
    elif value != desired_value:
        # ANY differing on-disk value (lower or higher) is a standing choice apply never
        # overwrites (see _do_provision_codex_skills_budget) -- surfaced here for visibility,
        # never auto-fixed.
        report.items.append(
            DriftItem("modified", "harness", action.item, path,
                      f"{dotted} is {value!r} (rig wants {desired_value!r}; apply never "
                      "clobbers — change it by hand or remove the key)")
        )


def _check_omp_web_search(action: Action, report: DriftReport) -> None:
    """Flag drift between the desired webSearchExclude list and omp config.yml.

    missing  — the file is absent, or a desired name is not in the list (apply adds it).
    modified — malformed YAML, or providers.webSearchExclude is the wrong shape.
    A list that already contains every desired name is NOT drift (extras stay).
    """
    import yaml  # lazy, like the rest of the config stack

    from .omp_web_search import missing_exclude_entries

    path = action.target
    desired = [str(x) for x in action.options.get("exclude") or [] if str(x)]
    if not path.is_file():
        report.items.append(
            DriftItem("missing", "harness", action.item, path,
                      "omp web_search exclude not written (apply sets providers.webSearchExclude)")
        )
        return
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        report.items.append(
            DriftItem("modified", "harness", action.item, path,
                      "omp config.yml is unreadable (permissions/encoding) — fix it by hand")
        )
        return
    except yaml.YAMLError:
        report.items.append(
            DriftItem("modified", "harness", action.item, path,
                      "omp config.yml is malformed YAML")
        )
        return
    if not isinstance(data, dict) and data is not None:
        report.items.append(
            DriftItem("modified", "harness", action.item, path,
                      "omp config.yml is not a YAML mapping — fix it by hand")
        )
        return
    data = data if isinstance(data, dict) else {}
    state, missing = missing_exclude_entries(data, desired)
    if state == "shape":
        report.items.append(
            DriftItem("modified", "harness", action.item, path,
                      "providers.webSearchExclude is the wrong shape — fix it by hand")
        )
        return
    if missing:
        report.items.append(
            DriftItem("missing", "harness", action.item, path,
                      f"providers.webSearchExclude missing {', '.join(missing)} (apply adds them)")
        )


def _check_problem_reporting(action: Action, report: DriftReport) -> None:
    from .problem_reporting import resolve

    result = resolve(action.target)
    if result.state != "ok":
        direction = "missing" if result.state in {"create_file", "create"} else "modified"
        report.items.append(DriftItem(direction, action.category, action.item, action.target, result.detail))


def _check_instruction_policy(action: Action, report: DriftReport) -> None:
    """Flag drift between the advisory instruction policy block and the instruction file.

    missing/modified mirror the execpolicy checker exactly (same splice resolver)."""
    from .actions.harness_settings import (
        INSTRUCTION_POLICY_BEGIN_MARKER,
        INSTRUCTION_POLICY_END_MARKER,
        instruction_policy_block_text,
    )
    from .actions.managed_blocks import _splice_managed_block

    desired = instruction_policy_block_text(action)
    path = action.target
    if not path.is_file():
        report.items.append(
            DriftItem("missing", "permissions", action.item, path,
                      "instruction policy file not written")
        )
        return
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            content = fh.read()
    except OSError as exc:
        report.items.append(
            DriftItem("modified", "permissions", action.item, path, f"cannot read {path}: {exc}")
        )
        return
    _, state, _detail = _splice_managed_block(
        content, desired, INSTRUCTION_POLICY_BEGIN_MARKER, INSTRUCTION_POLICY_END_MARKER
    )
    if state == "ok":
        return
    if state == "create":
        report.items.append(
            DriftItem("missing", "permissions", action.item, path,
                      "no rig-managed instruction policy block (apply adds it)")
        )
    elif state == "conflict":
        report.items.append(
            DriftItem("modified", "permissions", action.item, path,
                      "unbalanced/misordered rig-managed instruction policy markers — reconcile "
                      "by hand, then re-run")
        )
    else:  # update
        report.items.append(
            DriftItem("modified", "permissions", action.item, path,
                      "rig-managed instruction policy block is stale (apply rewrites it)")
        )
