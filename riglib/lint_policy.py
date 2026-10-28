"""Resolve Rig-owned lint policy into generated Oxlint configuration.

The policy source is Rig configuration, not ``oxlint.config.ts``. The config loader already
cascades built-in defaults < global ``~/.config/rig/config.yaml`` < repository ``rig.yaml``;
this module resolves the merged ``linters.rules`` block into deterministic rule severities and
renders the generated Oxc config.

Stdlib-only at import time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

Severity = str
VALID_SEVERITIES = {"off", "warn", "error"}


@dataclass(frozen=True)
class RuleSpec:
    name: str
    group: str
    default: Severity
    all_severity: Severity
    provider: str = "oxlint"
    # True for rules the tsgolint backend executes. They only run under `options.typeAware`, which
    # needs `oxlint-tsgolint` and TypeScript >= 7 (https://oxc.rs/docs/guide/usage/linter/type-aware);
    # emitting them elsewhere makes Oxlint fail or silently skip them.
    needs_type_info: bool = False


# Defaults are policy, not a mirror of the plugin's inventory. `all_severity` answers the stronger
# explicit request `all: true`: even intentionally off-by-default rules become enabled.
RULES: tuple[RuleSpec, ...] = (
    RuleSpec("typescript/no-unsafe-type-assertion", "typescript-core", "error", "error", needs_type_info=True),
    RuleSpec("typescript/no-unnecessary-type-assertion", "typescript-core", "error", "error", needs_type_info=True),
    RuleSpec("typescript/no-non-null-assertion", "typescript-core", "error", "error"),
    RuleSpec("typescript/ban-ts-comment", "typescript-core", "error", "error"),
    RuleSpec("anti-slop/no-chained-type-assertions", "anti-slop", "error", "error", "anti-slop"),
    RuleSpec("anti-slop/no-known-value-widening", "anti-slop", "error", "error", "anti-slop"),
    RuleSpec("anti-slop/no-widen-then-assert", "anti-slop", "error", "error", "anti-slop"),
    RuleSpec("anti-slop/no-unsafe-dictionary-type", "anti-slop", "error", "error", "anti-slop"),
    RuleSpec("anti-slop/require-safety-comment-for-type-assertion", "anti-slop", "error", "error", "anti-slop"),
    RuleSpec("anti-slop/no-object-parameters", "anti-slop", "error", "error", "anti-slop"),
    RuleSpec("anti-slop/no-unknown-type-aliases", "anti-slop", "error", "error", "anti-slop"),
    RuleSpec("anti-slop/no-unknown-returns", "anti-slop", "error", "error", "anti-slop"),
    RuleSpec("anti-slop/no-reflect-get", "anti-slop", "warn", "error", "anti-slop"),
    RuleSpec("anti-slop/no-reflect-apply", "anti-slop", "warn", "error", "anti-slop"),
    RuleSpec("anti-slop/no-module-mocking", "anti-slop", "warn", "error", "anti-slop"),
    RuleSpec("anti-slop/no-unknown-parameters", "anti-slop", "warn", "error", "anti-slop"),
    RuleSpec("anti-slop/no-runtime-typeof", "anti-slop", "off", "error", "anti-slop"),
    RuleSpec("anti-slop/no-conditional-empty-object-spread", "anti-slop", "off", "error", "anti-slop"),
    RuleSpec("anti-slop/no-shape-in-symbol-names", "anti-slop", "off", "error", "anti-slop"),
    RuleSpec("anti-slop/no-multiple-function-params", "anti-slop", "off", "error", "anti-slop"),
    RuleSpec("anti-slop/no-optional-function-parameters", "anti-slop", "off", "error", "anti-slop"),
)

RULE_BY_NAME = {rule.name: rule for rule in RULES}
GROUPS = tuple(dict.fromkeys(rule.group for rule in RULES))


def _string_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError("lint rule enable/disable values must be arrays of non-empty rule names")
    return tuple(value)


def _validate_known(names: tuple[str, ...], field: str) -> None:
    unknown = sorted(set(names) - set(RULE_BY_NAME))
    if unknown:
        raise ValueError(f"unknown lint rule(s) in {field}: {', '.join(unknown)}")


def _apply_groups(severities: dict[str, Severity], groups: Any) -> None:
    if groups is None:
        groups = {}
    if not isinstance(groups, Mapping):
        raise ValueError("linters.rules.groups must be a mapping of group name to bool")
    unknown_groups = sorted(set(groups) - set(GROUPS))
    if unknown_groups:
        raise ValueError(f"unknown lint rule group(s): {', '.join(unknown_groups)}")
    for group, enabled in groups.items():
        if not isinstance(enabled, bool):
            raise ValueError(f"linters.rules.groups.{group} must be a bool")
        for rule in RULES:
            if rule.group == group:
                severities[rule.name] = rule.default if enabled else "off"


def _apply_enable_disable(severities: dict[str, Severity], cfg: Mapping[str, Any]) -> None:
    enable = _string_list(cfg.get("enable"))
    disable = _string_list(cfg.get("disable"))
    _validate_known(enable, "linters.rules.enable")
    _validate_known(disable, "linters.rules.disable")
    for name in enable:
        spec = RULE_BY_NAME[name]
        severities[name] = spec.default if spec.default != "off" else "warn"
    for name in disable:
        severities[name] = "off"


# Not a policy rule (no default, not counted): Oxlint's React "React must be in scope" check, which the
# automatic JSX runtime makes a false positive. Rig only decides its severity when it renders the policy.
JSX_SCOPE_RULE = "react/react-in-jsx-scope"


def _check_severity(name: str, severity: Any) -> None:
    if severity not in VALID_SEVERITIES:
        raise ValueError(
            f"linters.rules.severity.{name} must be off, warn, or error (got {severity!r}); "
            'quote it in YAML -- an unquoted off/on/yes/no is parsed as a boolean: severity: "off"'
        )


def _apply_explicit(severities: dict[str, Severity], explicit: Any) -> None:
    if explicit is None:
        explicit = {}
    if not isinstance(explicit, Mapping):
        raise ValueError("linters.rules.severity must be a mapping of rule name to off|warn|error")
    unknown = sorted(set(explicit) - set(RULE_BY_NAME) - {JSX_SCOPE_RULE})
    if unknown:
        raise ValueError(f"unknown lint rule(s) in linters.rules.severity: {', '.join(unknown)}")
    for name, severity in explicit.items():
        _check_severity(name, severity)
        if name != JSX_SCOPE_RULE:
            severities[name] = str(severity)


def resolve_rule_severities(rules_config: Mapping[str, Any] | None = None) -> dict[str, Severity]:
    """Resolve one already-cascaded ``linters.rules`` mapping.

    Resolution order is intentionally explicit:

    1. built-in per-rule defaults;
    2. ``groups`` toggles;
    3. ``all: true`` (enable every known rule at its strict ``all_severity``);
    4. ``enable`` / ``disable``;
    5. explicit ``severity`` mapping (final authority).

    ``enable`` restores a rule to its normal default when non-off, otherwise to ``warn``. This
    makes enabling an opinionated off-by-default rule useful without unexpectedly turning it into
    a blocking error. ``all: true`` is deliberately stronger and enables everything.
    """
    cfg: Mapping[str, Any] = rules_config or {}
    severities = {rule.name: rule.default for rule in RULES}
    _apply_groups(severities, cfg.get("groups", {}))

    all_rules = cfg.get("all", False)
    if not isinstance(all_rules, bool):
        raise ValueError("linters.rules.all must be a bool")
    if all_rules:
        severities = {rule.name: rule.all_severity for rule in RULES}

    _apply_enable_disable(severities, cfg)
    _apply_explicit(severities, cfg.get("severity", {}))
    return severities


def anti_slop_required(severities: Mapping[str, Severity]) -> bool:
    return any(
        severity != "off" and RULE_BY_NAME.get(name, RuleSpec(name, "", "off", "off")).provider == "anti-slop"
        for name, severity in severities.items()
    )


_HEADER = (
    "// Managed by Rig. Source of truth: global Rig config + repository rig.yaml.",
    "// Do not edit directly: `rig apply` reconciles this file. Temporary diagnostic edits are allowed locally,",
    "// but move the final change into Rig policy before commit.",
)
_PRINT_WIDTH = 100
_ANTI_SLOP_PLUGIN = {"name": "anti-slop", "specifier": "./tools/oxlint/anti-slop/index.ts"}
_CATEGORIES = {"correctness": "error", "suspicious": "error", "perf": "error"}
_BAN_TS_COMMENT_OPTIONS = {
    "ts-ignore": True,
    "ts-nocheck": True,
    "ts-expect-error": "allow-with-description",
    "minimumDescriptionLength": 8,
}
TYPE_AWARE_REQUIREMENT = "requires type-aware linting (oxlint-tsgolint declared and TypeScript >= 7)"


def _emitted_rules(
    severities: Mapping[str, Severity], type_aware: bool, *, js_plugin: bool
) -> tuple[dict[str, Severity], tuple[str, ...]]:
    """Split resolved severities into rules to emit and type-info rules omitted for lack of tsgolint.

    Without the anti-slop JS plugin registered, even an ``off`` ``anti-slop/*`` entry makes Oxlint
    refuse the whole config ("Plugin 'anti-slop' not found"), so those entries are not emitted.
    """
    emitted: dict[str, Severity] = {}
    omitted: list[str] = []
    for name in sorted(severities):
        spec = RULE_BY_NAME.get(name)
        if spec is not None and spec.provider == "anti-slop" and not js_plugin:
            continue
        if spec is not None and spec.needs_type_info and not type_aware:
            omitted.append(name)
        else:
            emitted[name] = severities[name]
    return emitted, tuple(omitted)


def _omission_comment(omitted: tuple[str, ...]) -> list[str]:
    if not omitted:
        return []
    return [
        "// Type-info rules omitted (need oxlint-tsgolint + TypeScript >= 7): " + ", ".join(omitted) + ".",
    ]


# Oxlint's `plugins` field REPLACES its default set, so a framework plugin list must restate the defaults
# (https://oxc.rs/docs/guide/usage/linter/plugins). Only frameworks with a built-in plugin appear here:
# Svelte has none, so a Svelte repo keeps the generic defaults.
DEFAULT_OXLINT_PLUGINS = ("eslint", "typescript", "unicorn", "oxc")
_FRAMEWORK_PLUGINS: dict[str, tuple[str, ...]] = {
    "react": ("react",),
    "next": ("react", "nextjs"),
    "vue": ("vue",),
    "nuxt": ("vue",),
    "svelte": (),
}


def framework_plugins(frameworks: Sequence[str]) -> tuple[str, ...]:
    """Built-in Oxlint plugins for the detected frameworks, in stable order, nothing for unknown ones."""
    found: list[str] = []
    for framework in sorted(frameworks):
        for plugin in _FRAMEWORK_PLUGINS.get(framework, ()):
            if plugin not in found:
                found.append(plugin)
    return tuple(found)


def _plugin_list(plugins: Sequence[str]) -> list[str]:
    """Defaults plus the requested extras; empty when there are no extras (Oxlint then keeps its defaults)."""
    extras = [p for p in plugins if p not in DEFAULT_OXLINT_PLUGINS]
    return [*DEFAULT_OXLINT_PLUGINS, *dict.fromkeys(extras)] if extras else []


def render_oxlint_config(
    rules_config: Mapping[str, Any] | None = None,
    *,
    flavor: Literal["ts", "json"] = "ts",
    type_aware: bool = False,
    plugins: Sequence[str] = (),
    plugin_specifier: str | None = None,
    automatic_jsx: bool = False,
) -> str:
    """Render the deterministic Oxlint config from Rig policy.

    ``flavor="ts"`` is ``oxlint.config.ts`` (needs Node >= 22.18); ``"json"`` is a
    ``.oxlintrc.json`` for older Node. ``type_aware`` must be true only when ``oxlint-tsgolint`` is
    declared and TypeScript >= 7 is resolved: it adds ``options.typeAware`` and the type-info rules.
    ``plugins`` are extra built-in plugins (see :func:`framework_plugins`); the defaults are restated
    automatically because setting ``plugins`` replaces them. ``plugin_specifier`` is
    the absolute path of an externally hosted bundle (the Rig tool cache): the config then lives outside
    the repository, so no repo-relative ignore of the bundle is needed or emitted.
    ``automatic_jsx`` (explicit ``react-jsx``/``react-jsxdev`` tsconfig evidence) turns
    ``react/react-in-jsx-scope`` off when the React plugin is on; an explicit ``linters.rules.severity`` entry
    for that rule is final under any runtime.
    """
    severities = resolve_rule_severities(rules_config)
    plugin = anti_slop_required(severities)
    emitted, omitted = _emitted_rules(severities, type_aware, js_plugin=plugin)
    if "react" in plugins:
        explicit = (rules_config or {}).get("severity") or {}
        if JSX_SCOPE_RULE in explicit:
            emitted[JSX_SCOPE_RULE] = str(explicit[JSX_SCOPE_RULE])
        elif automatic_jsx:
            emitted[JSX_SCOPE_RULE] = "off"
    header = [*_HEADER, *_omission_comment(omitted)]
    builtin = _plugin_list(plugins)
    specifier = plugin_specifier or str(_ANTI_SLOP_PLUGIN["specifier"])
    if flavor == "json":
        return _render_json(header, emitted, plugin=plugin, type_aware=type_aware, builtin=builtin, specifier=specifier)
    return _render_ts(header, emitted, plugin=plugin, type_aware=type_aware, builtin=builtin, specifier=specifier)


def _render_json(
    header: list[str], rules: Mapping[str, Severity], *, plugin: bool, type_aware: bool, builtin: list[str],
    specifier: str,
) -> str:
    doc: dict[str, Any] = {}
    if type_aware:
        doc["options"] = {"typeAware": True}
    if builtin:
        doc["plugins"] = builtin
    if plugin:
        doc["jsPlugins"] = [{"name": _ANTI_SLOP_PLUGIN["name"], "specifier": specifier}]
    doc["categories"] = dict(_CATEGORIES)
    doc["rules"] = {
        name: [severity, dict(_BAN_TS_COMMENT_OPTIONS)] if name == "typescript/ban-ts-comment" and severity != "off" else severity
        for name, severity in rules.items()
    }
    body = json.dumps(doc, indent=2)
    if builtin:
        # Oxfmt collapses a string array that fits the print width onto one line; match it so the file passes.
        inline = f'  "plugins": {json.dumps(builtin)},'
        if len(inline) <= _PRINT_WIDTH:
            expanded = '  "plugins": [\n' + ",\n".join(f"    {json.dumps(p)}" for p in builtin) + "\n  ],"
            body = body.replace(expanded, inline, 1)
    return "\n".join(header) + "\n" + body + "\n"


def _render_ts(
    header: list[str], rules: Mapping[str, Severity], *, plugin: bool, type_aware: bool, builtin: list[str],
    specifier: str,
) -> str:
    lines = [
        *header,
        "",
        'import { defineConfig } from "oxlint";',
        "",
        "export default defineConfig({",
    ]
    if type_aware:
        lines.append("  options: { typeAware: true },")
    if builtin:
        lines.append(f"  plugins: {json.dumps(builtin)},")
    if plugin:
        entry = f'{{ name: "{_ANTI_SLOP_PLUGIN["name"]}", specifier: {json.dumps(specifier)} }}'
        one_line = f"  jsPlugins: [{entry}],"
        # Oxfmt's canonical form (printWidth 100): the generated file must pass the repo's own format check.
        lines.extend([one_line] if len(one_line) <= _PRINT_WIDTH else ["  jsPlugins: [", f"    {entry},", "  ],"])
    lines.extend(
        [
            '  categories: { correctness: "error", suspicious: "error", perf: "error" },',
            "  rules: {",
        ]
    )
    for name, severity in rules.items():
        if name == "typescript/ban-ts-comment" and severity != "off":
            lines.extend(
                [
                    f'    "{name}": [',
                    f'      "{severity}",',
                    "      {",
                    '        "ts-ignore": true,',
                    '        "ts-nocheck": true,',
                    '        "ts-expect-error": "allow-with-description",',
                    "        minimumDescriptionLength: 8,",
                    "      },",
                    "    ],",
                ]
            )
        else:
            lines.append(f"    {json.dumps(name)}: {json.dumps(severity)},")
    lines.extend(["  },", "});", ""])
    return "\n".join(lines)


def rule_activity(rules_config: Mapping[str, Any] | None, *, type_aware: bool) -> dict[str, tuple[bool, str]]:
    """Per rule: is it actually enforced, and when not, why (feeds ``rig lint rules``)."""
    activity: dict[str, tuple[bool, str]] = {}
    for name, severity in resolve_rule_severities(rules_config).items():
        if severity == "off":
            activity[name] = (False, "severity is off in the resolved policy")
        elif RULE_BY_NAME[name].needs_type_info and not type_aware:
            activity[name] = (False, TYPE_AWARE_REQUIREMENT)
        else:
            activity[name] = (True, "")
    return activity


def rule_policy_summary(rules_config: Mapping[str, Any] | None = None) -> dict[str, int]:
    severities = resolve_rule_severities(rules_config)
    return {
        "error": sum(value == "error" for value in severities.values()),
        "warn": sum(value == "warn" for value in severities.values()),
        "off": sum(value == "off" for value in severities.values()),
    }
