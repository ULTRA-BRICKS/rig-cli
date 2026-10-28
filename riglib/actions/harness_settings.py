"""omp idle TTL, codex skills budget, omp web search and instruction-policy provisioning."""

from __future__ import annotations

from pathlib import Path
import shutil

from . import fsutil
from ..codex_skills_budget import DEFAULT_MAX_CONTEXT_TOKENS, SKILLS_BUDGET_KEY
from ..omp_idle_ttl import DEFAULT_OMP_IDLE_TTL_MS, IDLE_TTL_KEY
from ..plan import Action
from .managed_blocks import _splice_managed_block
from .omp_yaml_state import _mark_omp_yaml_created, _omp_yaml_skip_existing
from .result import ActionResult
from .shared import _set_nested, _should_backup, approval_lookup
from .toml_edit import _TOML_UNVERIFIABLE_REMEDY, _toml_has_multiline_string, _toml_parser, _toml_table_bounds


OMP_IDLE_TTL_KEY = IDLE_TTL_KEY

OMP_IDLE_TTL_VALUE = DEFAULT_OMP_IDLE_TTL_MS

OMP_IDLE_TTL_LIVE_SESSION_NOTE = "live omp processes keep spawn-cached TTL until restart"


def _idle_ttl_parking_disabled(value: object) -> bool:
    """True when the on-disk value already disables parking (``<= 0``)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value <= 0


def _do_provision_omp_idle_ttl(action: Action, on_conflict: str) -> ActionResult:
    """Merge the resolved ``task.agentIdleTtlMs`` grace window into omp config.yml — additive, never clobber."""
    import yaml

    config_file = action.target
    desired_value = action.options.get("ms", OMP_IDLE_TTL_VALUE)
    dotted = ".".join(OMP_IDLE_TTL_KEY)
    label = f"omp-idle-ttl/{action.item}"
    loaded = _load_omp_yaml_mapping(action, config_file, label)
    if isinstance(loaded, ActionResult):
        return loaded
    data, existed = loaded

    state, value = approval_lookup(data, OMP_IDLE_TTL_KEY)
    if state == "shape":
        return ActionResult(
            action, "error",
            f"{label}: '{value}' is not an object in {config_file}",
        )
    if state == "present" and _idle_ttl_parking_disabled(value):
        return ActionResult(
            action, "skipped",
            f"{label}: parking already disabled ({value!r}) in {config_file}; {OMP_IDLE_TTL_LIVE_SESSION_NOTE}",
        )
    if state == "present" and value == desired_value:
        return ActionResult(
            action, "skipped",
            f"{label}: {dotted} already {value!r} in {config_file}; {OMP_IDLE_TTL_LIVE_SESSION_NOTE}",
        )
    if state == "present":
        return ActionResult(
            action, "skipped",
            f"{label}: {dotted} is {value!r} (rig wants {desired_value!r}; "
            "apply never clobbers — change it by hand or remove the key)",
        )

    # A missing key is additive, not a conflict: skip-forever would leave permanent
    # drift when a sibling (provision_harness_approval) created the file in this
    # same apply. YAML dump may drop comments — same tax the approval writer pays.
    config_file.parent.mkdir(parents=True, exist_ok=True)
    backup_note = _backup_existing_note(config_file, existed, on_conflict)
    _set_nested(data, OMP_IDLE_TTL_KEY, desired_value)
    config_file.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8",
    )
    status = "backed_up" if backup_note else ("updated" if existed else "created")
    return ActionResult(
        action, status,
        f"{label}: set {dotted}={desired_value} in {config_file}{backup_note}; {OMP_IDLE_TTL_LIVE_SESSION_NOTE}",
    )


def _backup_existing_note(config_file: Path, existed: bool, on_conflict: str) -> str:
    """Back up an EXISTING config before a rewrite (per policy); returns the detail note or ''."""
    if not (existed and _should_backup(on_conflict)):
        return ""
    bak = fsutil.backup_path(config_file)
    shutil.copy2(str(config_file), str(bak))
    return f" (backed up prior → {bak})"


def _load_omp_yaml_mapping(action: Action, config_file: Path, label: str) -> tuple[dict, bool] | ActionResult:
    """Parse omp config.yml as ``(mapping, existed)``; malformed/non-mapping is an error result."""
    import yaml

    existed = config_file.is_file()
    if not existed:
        return {}, False
    try:
        loaded = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return ActionResult(
            action, "error",
            f"{label}: {config_file} is malformed YAML ({exc.__class__.__name__}) — "
            "fix it by hand; rig never rewrites a config it cannot parse",
        )
    if loaded is not None and not isinstance(loaded, dict):
        return ActionResult(
            action, "error",
            f"{label}: {config_file} is not a YAML mapping — fix it by hand",
        )
    return loaded or {}, True


# ── codex skills-context budget (skills.max_context_tokens, ~/.codex/config.toml) ─────────
# Codex CLI truncates/omits skill descriptions from its model-visible catalog once a personal
# skill library's rendered text exceeds Codex's own (much smaller) context budget for that
# catalog (openai/codex#24299). rig's default (CODEX_SKILLS_BUDGET_VALUE, 50000) was verified
# (2026-09-18, InvestigateCodexSkillLoading) to roughly double visible description length and
# plateau around 40KB. Additive TOML merge, never clobbers a value already on disk (drift only)
# -- the same idle-TTL convention above, applied to a [skills] TABLE key instead of a root key
# or a nested YAML mapping.


CODEX_SKILLS_BUDGET_KEY = SKILLS_BUDGET_KEY

CODEX_SKILLS_BUDGET_VALUE = DEFAULT_MAX_CONTEXT_TOKENS


def _skills_budget_unmanageable_reason(text: str, data: dict) -> str | None:
    """Why the ``[skills]`` table in this TOML ``text``/parsed ``data`` cannot be safely
    text-edited to add ``max_context_tokens``, or ``None`` when it can. SHARED by the writer
    (:func:`_do_provision_codex_skills_budget`) and drift (:func:`riglib.drift._check_codex_skills_budget`)
    so both classify the exact same two on-disk shapes identically — ``approval_lookup``'s
    ``"missing"`` state alone does not distinguish "apply will create/append the key" from "apply
    refuses and leaves it for the user"; without this shared check drift reports a permanent,
    self-contradictory "missing (apply sets N)" for a value apply will never actually write.
    """
    table = CODEX_SKILLS_BUDGET_KEY[0]
    if _toml_has_multiline_string(text):
        return f"a TOML multiline string is present — rig cannot safely locate [{table}] text-level"
    if _toml_table_bounds(text, table) is None and isinstance(data.get(table), dict):
        return f"{table} is defined via dotted keys or an inline table (no literal [{table}] header)"
    return None


def _insert_toml_table_key(text: str, table: str, key: str, value: int) -> str:
    """Insert ``key = value`` (a plain integer) into a top-level ``[table]``, preserving every
    other byte. An existing literal ``[table]`` section (see :func:`_toml_table_bounds`) gets the
    key appended at the end of its body — any trailing BLANK lines in that body are moved PAST
    the new key so it never reads as attached to whatever blank run precedes the NEXT section (a
    trailing COMMENT is not hoisted — table membership is header-driven, so this is cosmetic, not
    a correctness gap); otherwise a brand-new ``[table]`` section is appended at EOF. Callers own
    the two preconditions checked by :func:`_skills_budget_unmanageable_reason` before calling
    this: ``text`` must contain no genuine multiline TOML string, and ``table`` must not already
    exist as a dotted-key/inline-table entry with no literal header — both would let
    :func:`_toml_table_bounds` return a false ``None`` and this function then append a colliding
    duplicate ``[table]`` header. The writer's post-merge parse check is still the final safety
    net regardless.
    """
    bounds = _toml_table_bounds(text, table)
    if bounds is None:
        prefix = text.rstrip("\n")
        block = f"[{table}]\n{key} = {value}\n"
        return (prefix + "\n\n" if prefix else "") + block
    _, end = bounds
    lines = text[:end].splitlines(keepends=True)
    trailing_blank: list[str] = []
    while lines and lines[-1].strip() == "":
        trailing_blank.append(lines.pop())
    head = "".join(lines)
    if head and not head.endswith("\n"):
        head += "\n"
    return head + f"{key} = {value}\n" + "".join(reversed(trailing_blank)) + text[end:]


def _do_provision_codex_skills_budget(action: Action, on_conflict: str) -> ActionResult:
    """Merge ``skills.max_context_tokens`` into codex config.toml — additive, never clobber.

    Idempotent (an equal value is ``skipped``); an existing file is always backed up before a
    write (mirroring the TOML mode writer, not only on a differing value). A present value of ANY
    magnitude — lower or higher than the resolved default — is left untouched forever: apply only
    WRITES when the key is absent, the same convention :func:`_do_provision_omp_idle_ttl`
    established for ``task.agentIdleTtlMs`` (drift still surfaces a mismatch; apply never
    auto-fixes it). Two on-disk shapes :func:`_insert_toml_table_key`'s text-level editor cannot
    safely handle (a genuine TOML multiline string, or ``skills`` already defined via dotted keys
    / an inline table) are reported and left for the user rather than risked — see
    :func:`_skills_budget_unmanageable_reason`, shared with drift so both surfaces agree.
    """
    config_file = action.target
    desired_value = action.options.get("max_context_tokens", CODEX_SKILLS_BUDGET_VALUE)
    dotted = ".".join(CODEX_SKILLS_BUDGET_KEY)
    table, leaf = CODEX_SKILLS_BUDGET_KEY
    label = f"codex-skills-budget/{action.item}"
    tomllib = _toml_parser()
    if tomllib is None:
        return ActionResult(action, "error", f"{label}: {config_file} left untouched — {_TOML_UNVERIFIABLE_REMEDY}")
    existed = config_file.is_file()
    loaded = _read_codex_toml(action, label, tomllib, existed)
    if isinstance(loaded, ActionResult):
        return loaded
    text, data = loaded

    state, value = approval_lookup(data, CODEX_SKILLS_BUDGET_KEY)
    if state == "shape":
        return ActionResult(action, "error", f"{label}: '{value}' is not a table in {config_file}")
    if state == "present" and value == desired_value:
        return ActionResult(action, "skipped", f"{label}: {dotted} already {value!r} in {config_file}")
    if state == "present":
        return ActionResult(
            action, "skipped",
            f"{label}: {dotted} is {value!r} (rig wants {desired_value!r}; "
            "apply never clobbers — change it by hand or remove the key)",
        )

    # state == "missing": the key needs writing, but the text-level editor only understands a
    # literal [table] header — the two shapes it cannot safely handle are reported, not risked.
    reason = _skills_budget_unmanageable_reason(text, data)
    if reason:
        return ActionResult(
            action, "skipped",
            f"{label}: {config_file} — {reason}; add `{dotted} = {desired_value}` by hand",
        )

    merged = _insert_toml_table_key(text, table, leaf, desired_value)
    try:
        tomllib.loads(merged)  # never leave codex a config it cannot load
    except tomllib.TOMLDecodeError:
        return ActionResult(action, "error", f"{label}: merged {config_file} would not parse as TOML — left untouched")
    backup_note = _backup_existing_note(config_file, existed, on_conflict)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(merged, encoding="utf-8")
    status = "backed_up" if backup_note else ("updated" if existed else "created")
    return ActionResult(action, status, f"{label}: set {dotted}={desired_value} in {config_file}{backup_note}")


def _read_codex_toml(action: Action, label: str, tomllib, existed: bool) -> tuple[str, dict] | ActionResult:
    """Read and parse an existing codex config.toml as ``(text, data)``, or an error result."""
    if not existed:
        return "", {}
    config_file = action.target
    try:
        text = config_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return ActionResult(action, "error", f"{label}: cannot read {config_file}: {exc}")
    try:
        return text, tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return ActionResult(
            action, "error",
            f"{label}: {config_file} is malformed TOML — fix it by hand; rig never rewrites a config it cannot parse",
        )


def _do_provision_omp_web_search(action: Action, on_conflict: str) -> ActionResult:
    """Additively merge ``providers.webSearchExclude`` into omp config.yml.

    Appends missing desired names; never removes extras. A matching list is a no-op.
    A non-list / non-mapping parent is a conflict (fix by hand). Malformed YAML is a
    hard error — same as the approval writer. Re-serializing an existing file drops
    comments, so ``on_conflict=skip`` leaves drift unreconciled.
    """
    import yaml  # lazy, like the rest of the config stack

    from ..omp_web_search import ensure_exclude, missing_exclude_entries

    desired = [str(x) for x in action.options.get("exclude") or [] if str(x)]
    if not desired:
        return ActionResult(action, "skipped", f"web-search/{action.item}: empty exclude, nothing to write")
    config_file = action.target
    label = f"web-search/{action.item}"
    loaded = _load_omp_yaml_mapping(action, config_file, label)
    if isinstance(loaded, ActionResult):
        return loaded
    data, existed = loaded
    state, missing = missing_exclude_entries(data, desired)
    if state == "shape":
        return ActionResult(
            action, "skipped",
            f"web-search/{action.item}: providers.webSearchExclude parent/value is the wrong shape, "
            "left untouched (drift visible)",
        )
    if not missing:
        return ActionResult(
            action, "skipped",
            f"web-search/{action.item}: already current in {config_file}",
        )
    if _omp_yaml_skip_existing(config_file, existed, on_conflict):
        return ActionResult(
            action, "skipped",
            f"web-search/{action.item}: {config_file} would be rewritten (yaml comments are not "
            "preserved) but on_conflict=skip — left unchanged (drift NOT reconciled)",
        )
    config_file.parent.mkdir(parents=True, exist_ok=True)
    backup_note = _backup_existing_note(config_file, existed, on_conflict)
    added = ensure_exclude(data, desired)
    config_file.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    if not existed:
        _mark_omp_yaml_created(config_file)
    status = "backed_up" if backup_note else ("updated" if existed else "created")
    return ActionResult(
        action, status,
        f"web-search/{action.item}: added {', '.join(added)} to providers.webSearchExclude "
        f"in {config_file}{backup_note}",
    )


# ── advisory instruction policy provisioning (pi/commandcode AGENTS.md) ─────────────────────
# Instruction-file harnesses have NO execution-layer mechanism; rig records the deny/ask INTENT
# as a marker-delimited ADVISORY block in the global instruction file — truthfully tensed ("not
# enforced"), terse (it rides every turn's context). Rendered from the SAME rule registry the
# omp guard extension is generated from.
INSTRUCTION_POLICY_BEGIN_MARKER = "# >>> rig-managed instruction policy (do not edit) >>>"

INSTRUCTION_POLICY_END_MARKER = "# <<< rig-managed instruction policy (do not edit) <<<"


def _rule_pattern(rule) -> str:
    """The command shape a GuardRule targets, e.g. ``git push --force|-f`` (policy prose)."""
    from ..permissions import SIGNAL_MATCHERS

    if rule.matcher in SIGNAL_MATCHERS:  # no tokens to render — the matcher carries the shape
        return SIGNAL_MATCHERS[rule.matcher]
    if rule.matcher == "code_pattern":  # its only flag ("word") is a match mode, not argv
        return rule.tokens[0]
    if rule.matcher == "mcp_tool":
        shape = "MCP " + "|".join(rule.tokens)
        return shape + (" setting " + "|".join(rule.flags) + " > 0" if rule.flags else "")
    if rule.matcher == "write_path":
        return "writes to " + "|".join(rule.tokens)
    if rule.matcher == "http_write_url":
        shape = "curl/wget/http POST|PUT|PATCH to *." + "|*.".join(rule.tokens)
        shape += " " + "|".join(rule.value_prefixes)
        return shape + (" carrying " + "|".join(rule.flags) if rule.flags else "")
    base = " ".join(rule.tokens)
    if rule.flags:
        base += " " + "|".join(rule.flags)
    return base


def instruction_policy_block_text(action: Action) -> str:
    """The exact marker-delimited advisory block rig owns (shared by apply + drift)."""
    from ..permissions import OMP_GUARD_ASK_RULES, OMP_GUARD_DENY_RULES

    deny = " · ".join(_rule_pattern(r) for r in OMP_GUARD_DENY_RULES)
    ask = " · ".join(_rule_pattern(r) for r in OMP_GUARD_ASK_RULES)
    return "\n".join([
        INSTRUCTION_POLICY_BEGIN_MARKER,
        "# advisory policy — not enforced at the execution layer. This harness has no",
        "# command-granular permission mechanism rig can provision, so these rules are INTENT",
        "# the agent must self-enforce (the same baseline other harnesses enforce below the model):",
        f"# DENY: {deny}",
        f"# ASK (confirm with the operator first): {ask}",
        INSTRUCTION_POLICY_END_MARKER,
    ])


def _do_provision_problem_reporting(action: Action, on_conflict: str) -> ActionResult:
    from ..problem_reporting import apply

    return apply(action, on_conflict)


def _do_provision_instruction_policy(action: Action, on_conflict: str) -> ActionResult:
    """Splice the advisory policy block into the instruction file — idempotent, additive.

    Same reconcile discipline as the execpolicy block: append when absent (user content
    preserved), collapse duplicates to one, no-op a current block; ``skip`` leaves a stale
    block untouched; unbalanced/misordered markers are a hard error rig refuses to guess at.
    """
    from ..instruction_files import instruction_file_lock

    with instruction_file_lock(action.target):
        return _provision_instruction_policy_locked(action, on_conflict)


def _provision_instruction_policy_locked(action: Action, on_conflict: str) -> ActionResult:
    """Read and write under the same lock used by the global reporting-policy writer."""
    desired = instruction_policy_block_text(action)
    path = action.target
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(desired + "\n", encoding="utf-8")
        return ActionResult(action, "created", f"policy/{action.item}: wrote {path}")
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            content = fh.read()
    except OSError as exc:
        return ActionResult(action, "error", f"policy/{action.item}: cannot read {path}: {exc}")
    new_content, state, detail = _splice_managed_block(
        content, desired, INSTRUCTION_POLICY_BEGIN_MARKER, INSTRUCTION_POLICY_END_MARKER
    )
    if state == "conflict":
        return ActionResult(action, "error", f"policy/{action.item}: {detail}")
    if state == "ok":
        return ActionResult(action, "skipped", f"policy/{action.item}: already current in {path}")
    backup_note = ""
    if state == "update":
        if on_conflict == "skip":
            return ActionResult(
                action, "skipped",
                f"policy/{action.item}: {path} block differs but on_conflict=skip — left unchanged (drift NOT reconciled)",
            )
        if _should_backup(on_conflict):
            bak = fsutil.backup_path(path)
            shutil.copy2(str(path), str(bak))
            backup_note = f" (backed up prior → {bak})"
    path.write_text(new_content, encoding="utf-8")
    status = "backed_up" if backup_note else "updated"
    verb = "appended block to" if state == "create" else "reconciled block in"
    return ActionResult(action, status, f"policy/{action.item}: {verb} {path}{backup_note}")
