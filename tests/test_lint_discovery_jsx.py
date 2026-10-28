"""tsconfig automatic-JSX-runtime evidence and the policy it adapts, incl. the real Oxlint."""

import json
import subprocess

import pytest

from riglib.lint_discovery_jsx import MAX_EXTENDS_DEPTH, automatic_jsx_runtime, tsconfig_jsx
from riglib.lint_policy import JSX_SCOPE_RULE, framework_plugins, render_oxlint_config, resolve_rule_severities

REACT = framework_plugins(["react"])


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_reads_jsx_from_jsonc_with_comments_and_trailing_commas(tmp_path):
    ts = write(tmp_path / "tsconfig.json", '{\n // c\n "compilerOptions": {"jsx": "react-jsx", /* x */},\n}\n')
    assert tsconfig_jsx(ts) == "react-jsx"


def test_follows_extends_chain_child_overrides_parent(tmp_path):
    write(tmp_path / "base.json", '{"compilerOptions": {"jsx": "react-jsxdev"}}')
    write(tmp_path / "mid.json", '{"extends": "./base"}')
    child = write(tmp_path / "tsconfig.json", '{"extends": "./mid.json"}')
    assert tsconfig_jsx(child) == "react-jsxdev"
    override = write(tmp_path / "other" / "tsconfig.json", '{"extends": "../base.json", "compilerOptions": {"jsx": "react"}}')
    assert tsconfig_jsx(override) == "react"


def test_extends_list_later_entry_wins_and_package_extends_ignored(tmp_path):
    write(tmp_path / "a.json", '{"compilerOptions": {"jsx": "react"}}')
    write(tmp_path / "b.json", '{"compilerOptions": {"jsx": "react-jsx"}}')
    ts = write(tmp_path / "tsconfig.json", '{"extends": ["@tsconfig/node/base.json", "./a.json", "./b.json"]}')
    assert tsconfig_jsx(ts) == "react-jsx"


@pytest.mark.parametrize("text", ["{not json", "[]", '{"extends": "./missing.json"}', '{"compilerOptions": {"jsx": 1}}'])
def test_malformed_or_missing_is_no_evidence(tmp_path, text):
    assert tsconfig_jsx(write(tmp_path / "tsconfig.json", text)) is None


def test_missing_file_and_cycle_and_depth_are_no_evidence(tmp_path):
    assert tsconfig_jsx(tmp_path / "tsconfig.json") is None
    write(tmp_path / "a.json", '{"extends": "./b.json"}')
    write(tmp_path / "b.json", '{"extends": "./a.json"}')
    assert tsconfig_jsx(tmp_path / "a.json") is None
    for i in range(MAX_EXTENDS_DEPTH + 2):
        write(tmp_path / f"c{i}.json", json.dumps({"extends": f"./c{i + 1}.json"}))
    write(tmp_path / f"c{MAX_EXTENDS_DEPTH + 2}.json", '{"compilerOptions": {"jsx": "react-jsx"}}')
    assert tsconfig_jsx(tmp_path / "c0.json") is None


def test_symlinked_tsconfig_is_not_followed(tmp_path):
    real = write(tmp_path / "real.json", '{"compilerOptions": {"jsx": "react-jsx"}}')
    (tmp_path / "tsconfig.json").symlink_to(real)
    assert tsconfig_jsx(tmp_path / "tsconfig.json") is None


def test_nearest_tsconfig_wins_walking_up_from_the_scope(tmp_path):
    write(tmp_path / "tsconfig.json", '{"compilerOptions": {"jsx": "react-jsx"}}')
    assert automatic_jsx_runtime(tmp_path, ".") and automatic_jsx_runtime(tmp_path, "pkg/web")
    write(tmp_path / "pkg" / "tsconfig.json", '{"compilerOptions": {"jsx": "react"}}')
    assert not automatic_jsx_runtime(tmp_path, "pkg/web")
    assert not automatic_jsx_runtime(tmp_path / "empty", ".")


def rules_of(automatic, plugins=REACT, rules_config=None):
    text = render_oxlint_config(rules_config, flavor="json", plugins=plugins, automatic_jsx=automatic)
    return json.loads("\n".join(ln for ln in text.splitlines() if not ln.startswith("//")))["rules"]


def test_policy_turns_the_rule_off_only_for_automatic_runtime_with_react_plugin():
    assert rules_of(True)[JSX_SCOPE_RULE] == "off"
    assert JSX_SCOPE_RULE not in rules_of(False)
    assert JSX_SCOPE_RULE not in rules_of(True, plugins=())
    assert JSX_SCOPE_RULE not in resolve_rule_severities()


@pytest.mark.parametrize("severity", ["off", "warn", "error"])
@pytest.mark.parametrize("automatic", [True, False])
def test_explicit_rules_override_is_final_under_any_runtime(automatic, severity):
    rules = rules_of(automatic, rules_config={"severity": {JSX_SCOPE_RULE: severity}})
    assert rules[JSX_SCOPE_RULE] == severity


def test_explicit_rule_is_validated_and_not_emitted_without_the_react_plugin():
    assert JSX_SCOPE_RULE not in rules_of(True, plugins=(), rules_config={"severity": {JSX_SCOPE_RULE: "error"}})
    with pytest.raises(ValueError, match="must be off, warn, or error"):
        resolve_rule_severities({"severity": {JSX_SCOPE_RULE: "nope"}})


TSX_OK = "export const A = () => <div>hi</div>;\n"
TSX_NO_KEY = "export const B = () => [<i />, <i />];\n"


def run_real(tmp_path, jsx, source, rules_config=None):
    from tests.lint_real_helpers import pinned_tool

    real = str(pinned_tool("oxlint", "RIG_TEST_OXLINT_BIN"))
    write(tmp_path / "tsconfig.json", json.dumps({"compilerOptions": {"jsx": jsx}}))
    write(tmp_path / "a.tsx", source)
    from riglib.lint_discovery_jsx import AUTOMATIC_JSX_VALUES

    config = render_oxlint_config(
        {"groups": {"anti-slop": False}, **(rules_config or {})}, flavor="json", plugins=REACT, automatic_jsx=automatic_jsx_runtime(tmp_path, ".")
    )
    assert (jsx in AUTOMATIC_JSX_VALUES) == automatic_jsx_runtime(tmp_path, ".")
    write(tmp_path / ".oxlintrc.json", config)
    return subprocess.run([real, "-c", ".oxlintrc.json", "a.tsx"], cwd=tmp_path, capture_output=True, text=True, check=False)


@pytest.mark.parametrize("jsx", ["react-jsx", "react-jsxdev"])
def test_real_oxlint_automatic_runtime_passes_without_react_import(tmp_path, jsx):
    done = run_real(tmp_path, jsx, TSX_OK)
    assert done.returncode == 0, done.stdout + done.stderr


def test_real_oxlint_classic_runtime_still_requires_react_in_scope(tmp_path):
    done = run_real(tmp_path, "react", TSX_OK)
    assert done.returncode == 1 and "react-in-jsx-scope" in done.stdout, done.stdout + done.stderr


def test_real_oxlint_still_reports_missing_key_under_automatic_runtime(tmp_path):
    done = run_real(tmp_path, "react-jsx", TSX_NO_KEY)
    assert done.returncode == 1 and "jsx-key" in done.stdout and "react-in-jsx-scope" not in done.stdout, done.stdout


def test_real_oxlint_explicit_off_under_classic_runtime_passes_without_react_import(tmp_path):
    done = run_real(tmp_path, "react", TSX_OK, {"severity": {JSX_SCOPE_RULE: "off"}})
    assert done.returncode == 0, done.stdout + done.stderr


def test_real_oxlint_explicit_error_under_automatic_runtime_still_fails(tmp_path):
    done = run_real(tmp_path, "react-jsx", TSX_OK, {"severity": {JSX_SCOPE_RULE: "error"}})
    assert done.returncode == 1 and "react-in-jsx-scope" in done.stdout, done.stdout
