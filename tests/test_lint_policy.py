import json

from riglib.lint_policy import (
    RULE_BY_NAME,
    DEFAULT_OXLINT_PLUGINS,
    anti_slop_required,
    framework_plugins,
    render_oxlint_config,
    resolve_rule_severities,
    rule_activity,
    rule_policy_summary,
)


def parse_jsonc(text):
    # The generated JSON flavor only carries whole-line `//` header comments.
    return json.loads("\n".join(line for line in text.splitlines() if not line.startswith("//")))


def test_default_policy_uses_audited_severities():
    rules = resolve_rule_severities()
    assert rules["anti-slop/no-chained-type-assertions"] == "error"
    assert rules["anti-slop/no-reflect-get"] == "warn"
    assert rules["anti-slop/no-runtime-typeof"] == "off"
    assert rules["anti-slop/no-multiple-function-params"] == "off"
    assert rules["anti-slop/no-optional-function-parameters"] == "off"
    assert rules["typescript/no-unsafe-type-assertion"] == "error"
    assert rule_policy_summary() == {"error": 12, "warn": 4, "off": 5}


def test_all_true_really_enables_every_known_rule():
    rules = resolve_rule_severities({"all": True})
    assert set(rules.values()) == {"error"}


def test_disable_and_explicit_severity_win_after_all():
    rules = resolve_rule_severities(
        {
            "all": True,
            "disable": ["anti-slop/no-module-mocking"],
            "severity": {
                "anti-slop/no-reflect-get": "warn",
                "anti-slop/no-runtime-typeof": "off",
            },
        }
    )
    assert rules["anti-slop/no-module-mocking"] == "off"
    assert rules["anti-slop/no-reflect-get"] == "warn"
    assert rules["anti-slop/no-runtime-typeof"] == "off"


def test_group_can_be_disabled_and_rule_force_enabled():
    rules = resolve_rule_severities(
        {
            "groups": {"anti-slop": False},
            "enable": ["anti-slop/no-runtime-typeof"],
        }
    )
    assert rules["anti-slop/no-chained-type-assertions"] == "off"
    assert rules["anti-slop/no-runtime-typeof"] == "warn"
    assert rules["typescript/no-unsafe-type-assertion"] == "error"


def test_rendered_config_is_deterministic_and_registers_provider_when_needed():
    content = render_oxlint_config(type_aware=True)
    assert content == render_oxlint_config(type_aware=True)
    assert content.startswith("// Managed by Rig.")
    assert "Source of truth: global Rig config + repository rig.yaml" in content
    assert "Do not edit directly" in content
    assert 'import { defineConfig } from "oxlint";' in content
    assert 'name: "anti-slop"' in content
    assert '"anti-slop/no-runtime-typeof": "off"' in content
    assert '"anti-slop/no-multiple-function-params": "off"' in content
    assert '"typescript/no-unsafe-type-assertion": "error"' in content


def test_provider_can_disappear_when_anti_slop_group_is_off():
    rules = resolve_rule_severities({"groups": {"anti-slop": False}})
    assert not anti_slop_required(rules)
    assert 'name: "anti-slop"' not in render_oxlint_config({"groups": {"anti-slop": False}})


def test_type_aware_option_is_off_by_default_and_type_info_rules_are_omitted():
    content = render_oxlint_config()
    assert "typeAware" not in content
    assert "--type-check" not in content and "typeCheck" not in content
    assert '"typescript/no-unsafe-type-assertion"' not in content
    assert '"typescript/no-unnecessary-type-assertion"' not in content
    assert "oxlint-tsgolint" in content, "the omission must be explained in the header comment"
    assert '"typescript/no-non-null-assertion": "error"' in content


def test_type_aware_config_enables_option_and_type_info_rules():
    content = render_oxlint_config(type_aware=True)
    assert "options: { typeAware: true }," in content
    assert '"typescript/no-unsafe-type-assertion": "error"' in content
    assert '"typescript/no-unnecessary-type-assertion": "error"' in content
    assert "typeCheck" not in content


def test_json_flavor_is_valid_jsonc_with_the_managed_header():
    content = render_oxlint_config(flavor="json")
    assert content.startswith("// Managed by Rig.")
    assert "defineConfig" not in content
    doc = parse_jsonc(content)
    assert isinstance(doc, dict)
    assert "options" not in doc
    assert doc["jsPlugins"] == [{"name": "anti-slop", "specifier": "./tools/oxlint/anti-slop/index.ts"}]
    assert doc["rules"]["anti-slop/no-runtime-typeof"] == "off"
    assert doc["rules"]["typescript/ban-ts-comment"][0] == "error"
    assert "typescript/no-unsafe-type-assertion" not in doc["rules"]


def test_json_flavor_type_aware_sets_option():
    doc = parse_jsonc(render_oxlint_config(flavor="json", type_aware=True))
    assert doc["options"] == {"typeAware": True}
    assert doc["rules"]["typescript/no-unsafe-type-assertion"] == "error"


def test_json_flavor_drops_plugin_when_anti_slop_group_is_off():
    doc = parse_jsonc(render_oxlint_config({"groups": {"anti-slop": False}}, flavor="json"))
    assert "jsPlugins" not in doc


def test_rule_activity_reports_why_type_info_rules_are_inactive():
    activity = rule_activity(None, type_aware=False)
    assert set(activity) == set(RULE_BY_NAME)
    active, reason = activity["typescript/no-unsafe-type-assertion"]
    assert active is False
    assert "oxlint-tsgolint" in reason and "TypeScript" in reason
    assert activity["typescript/no-non-null-assertion"] == (True, "")
    assert activity["anti-slop/no-runtime-typeof"][0] is False
    assert "off" in activity["anti-slop/no-runtime-typeof"][1]
    assert rule_activity(None, type_aware=True)["typescript/no-unsafe-type-assertion"] == (True, "")


def test_policy_disabled_type_info_rule_is_reported_as_disabled():
    activity = rule_activity({"disable": ["typescript/no-unsafe-type-assertion"]}, type_aware=True)
    assert activity["typescript/no-unsafe-type-assertion"][0] is False
    assert "off" in activity["typescript/no-unsafe-type-assertion"][1]


def test_framework_plugins_are_documented_builtins_only_for_detected_frameworks():
    assert framework_plugins(()) == ()
    assert framework_plugins(("react",)) == ("react",)
    assert framework_plugins(("next", "react")) == ("react", "nextjs")
    assert framework_plugins(("nuxt", "vue")) == ("vue",)
    assert framework_plugins(("svelte",)) == (), "Oxlint has no built-in Svelte plugin"
    assert framework_plugins(("angular",)) == ()


def test_no_extra_plugins_leaves_oxlint_defaults_untouched():
    assert "plugins" not in parse_jsonc(render_oxlint_config(flavor="json"))
    assert "plugins:" not in render_oxlint_config()
    assert "plugins" not in parse_jsonc(render_oxlint_config(flavor="json", plugins=("typescript",)))


def test_framework_plugins_restate_the_defaults_because_oxlint_replaces_them():
    doc = parse_jsonc(render_oxlint_config(flavor="json", plugins=framework_plugins(("react",))))
    assert doc["plugins"] == [*DEFAULT_OXLINT_PLUGINS, "react"]
    assert "vue" not in doc["plugins"] and "nextjs" not in doc["plugins"]
    ts = render_oxlint_config(plugins=framework_plugins(("next",)))
    assert f"plugins: {json.dumps([*DEFAULT_OXLINT_PLUGINS, 'react', 'nextjs'])}," in ts


def test_framework_plugins_never_turn_on_type_aware_mode():
    doc = parse_jsonc(render_oxlint_config(flavor="json", plugins=("react",)))
    assert "options" not in doc


def test_anti_slop_rules_are_not_emitted_without_the_js_plugin():
    """A real Oxlint rejects the whole config for `anti-slop/*` entries when the plugin is not registered."""
    config = {"groups": {"anti-slop": False}}
    assert "anti-slop/" not in render_oxlint_config(config)
    assert not any(name.startswith("anti-slop/") for name in parse_jsonc(render_oxlint_config(config, flavor="json"))["rules"])
    assert "anti-slop/no-reflect-get" in parse_jsonc(render_oxlint_config(flavor="json"))["rules"]
