"""The ``harness_wrappers`` block: `rig apply commit` provisions the per-harness PATH wrappers.

plan.build -> one ``harness_wrapper`` action per kind -> ``runner._do_harness_wrapper``
(``harness_launcher.install_wrapper``) -> drift (``rig status``) and verify read the SAME
``wrapper_provision.resolve`` state. Every test runs against a tmp dir under the suite's isolated
HOME (conftest), so the real ``~/.local`` / ``~/.config`` are never touched.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from riglib import config_schema, harness_launcher, wrapper_provision
from riglib.actions import runner
from riglib.actions.runner import _do_harness_wrapper, run_plan
from riglib.config import ConfigError, LoadedConfig, global_config_path, load, validate
from riglib.drift import DriftReport, _check_harness_wrapper
from riglib.harness_kinds import KINDS
from riglib.plan import InstallPlan, _build_harness_wrappers


def _cfg(block, tmp_path: Path) -> LoadedConfig:
    data = {"version": 1} if block is None else {"version": 1, "harness_wrappers": block}
    return LoadedConfig(data=data, repo_root=tmp_path)


def _plan(block, tmp_path: Path) -> InstallPlan:
    plan = InstallPlan()
    _build_harness_wrappers(_cfg(block, tmp_path), plan)
    return plan


@pytest.fixture
def bindir(tmp_path: Path) -> Path:
    return tmp_path / "wrappers"


def _enabled_plan(bindir: Path, tmp_path: Path, **extra) -> InstallPlan:
    return _plan({"enabled": True, "dir": str(bindir), **extra}, tmp_path)


def _real_binary(tmp_path: Path, name: str = "omp") -> Path:
    path = tmp_path / "vendor" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _recorded_real_bins() -> dict[str, str]:
    return harness_launcher.read_real_bins(harness_launcher.real_bin_config_path())


# ── plan: one action per kind, nothing unless opted in ─────────────────────────────────────
@pytest.mark.parametrize("block", [None, {}, {"enabled": False}, {"kinds": ["omp"]}, {"dir": "/x"}])
def test_plan_emits_nothing_unless_enabled_true(tmp_path, block):
    assert _plan(block, tmp_path).actions == []


def test_plan_enabled_emits_one_action_per_kind_under_the_default_dir(tmp_path):
    plan = _plan({"enabled": True}, tmp_path)

    assert [a.item for a in plan.actions] == list(KINDS)
    default_dir = Path.home() / ".local" / "share" / "rig" / "bin"
    for action in plan.actions:
        assert (action.kind, action.category) == ("harness_wrapper", "harness_wrappers")
        assert action.target == default_dir / action.item
        assert action.options == {"kind": action.item, "real_bin": None}


def test_plan_kinds_subset_is_emitted_in_canonical_order_without_duplicates(bindir, tmp_path):
    plan = _enabled_plan(bindir, tmp_path, kinds=["opencode", "omp", "omp"])

    assert [a.item for a in plan.actions] == ["omp", "opencode"]
    assert [a.target for a in plan.actions] == [bindir / "omp", bindir / "opencode"]


def test_plan_expands_dir_and_real_bin_and_attaches_real_bin_to_its_kind_only(tmp_path):
    plan = _plan(
        {"enabled": True, "dir": "~/wrapped", "kinds": ["omp", "claude"], "real_bin": {"omp": "~/src/omp/bin/omp"}},
        tmp_path,
    )

    by_kind = {a.item: a for a in plan.actions}
    assert by_kind["omp"].target == Path.home() / "wrapped" / "omp"
    assert by_kind["omp"].options["real_bin"] == str(Path.home() / "src" / "omp" / "bin" / "omp")
    assert by_kind["claude"].options["real_bin"] is None


def test_plan_does_no_io(bindir, tmp_path):
    _enabled_plan(bindir, tmp_path, real_bin={"omp": str(tmp_path / "missing" / "omp")})

    assert not bindir.exists()
    assert not harness_launcher.real_bin_config_path().exists()


def test_plan_build_includes_the_wrapper_actions(fake_agent_tools, tmp_path, bindir):
    from riglib.catalog import Catalog
    from riglib.plan import build

    cfg = LoadedConfig(
        data={"version": 1, "skills": {"enabled": False}, "harness_wrappers": {"enabled": True, "dir": str(bindir), "kinds": ["codex"]}},
        repo_root=tmp_path,
    )
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")

    wrappers = [a for a in plan.actions if a.category == "harness_wrappers"]
    assert [(a.kind, a.item, a.target) for a in wrappers] == [("harness_wrapper", "codex", bindir / "codex")]


# ── runner: install, idempotency, never overwrite what rig did not write ───────────────────
def test_apply_installs_every_wrapper_then_is_idempotent(bindir, tmp_path):
    plan = _enabled_plan(bindir, tmp_path)

    first = run_plan(plan)

    assert [r.status for r in first.results] == ["created"] * len(KINDS)
    for kind in KINDS:
        target = bindir / kind
        assert harness_launcher.wrapper_state(kind, target) == "current"
        assert stat.S_IMODE(target.stat().st_mode) == 0o755
        assert f"harness exec {kind} --" in target.read_text(encoding="utf-8")
    before = {k: (bindir / k).stat().st_mtime_ns for k in KINDS}

    second = run_plan(plan)

    assert [r.status for r in second.results] == ["skipped"] * len(KINDS)
    assert second.changed == 0
    assert {k: (bindir / k).stat().st_mtime_ns for k in KINDS} == before


def test_apply_rewrites_a_stale_rig_wrapper(bindir, tmp_path):
    plan = _enabled_plan(bindir, tmp_path, kinds=["omp"])
    run_plan(plan)
    target = bindir / "omp"
    target.write_text(f"#!/bin/sh\n{harness_launcher.WRAPPER_MARKER} (omp)\necho old rig\n", encoding="utf-8")
    assert harness_launcher.wrapper_state("omp", target) == "stale"

    result = run_plan(plan).results[0]

    assert result.status == "updated"
    assert harness_launcher.wrapper_state("omp", target) == "current"


@pytest.mark.parametrize("on_conflict", ["skip", "overwrite", "backup"])
def test_apply_never_overwrites_a_foreign_file_whatever_on_conflict_says(bindir, tmp_path, on_conflict):
    bindir.mkdir()
    (bindir / "omp").write_text("#!/bin/sh\necho my own omp\n", encoding="utf-8")
    plan = _enabled_plan(bindir, tmp_path, kinds=["omp", "claude"])
    plan.on_conflict = on_conflict

    report = run_plan(plan)

    by_kind = {r.action.item: r for r in report.results}
    assert by_kind["omp"].status == "error"
    assert str(bindir / "omp") in by_kind["omp"].detail and "rig apply commit" in by_kind["omp"].detail
    assert (bindir / "omp").read_text(encoding="utf-8") == "#!/bin/sh\necho my own omp\n"
    assert sorted(p.name for p in bindir.iterdir()) == ["claude", "omp"]  # no backup copy
    # one refused kind does not stop the others
    assert by_kind["claude"].status == "created"
    assert report.errors == [by_kind["omp"]]


def test_apply_never_overwrites_a_foreign_symlink(bindir, tmp_path):
    real = _real_binary(tmp_path)
    bindir.mkdir()
    (bindir / "omp").symlink_to(real)
    plan = _enabled_plan(bindir, tmp_path, kinds=["omp"])

    result = run_plan(plan).results[0]

    assert result.status == "error" and "symlink" in result.detail
    assert (bindir / "omp").is_symlink() and (bindir / "omp").resolve() == real.resolve()
    assert real.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"


def test_apply_records_the_real_binary_and_is_idempotent_about_it(bindir, tmp_path):
    real = _real_binary(tmp_path)
    plan = _enabled_plan(bindir, tmp_path, kinds=["omp"], real_bin={"omp": str(real)})

    first = run_plan(plan).results[0]
    assert first.status == "created"
    assert _recorded_real_bins() == {"omp": str(real.resolve())}
    assert run_plan(plan).results[0].status == "skipped"

    other = _real_binary(tmp_path / "fork")
    retargeted = _enabled_plan(bindir, tmp_path, kinds=["omp"], real_bin={"omp": str(other)})
    third = run_plan(retargeted).results[0]

    assert third.status == "updated" and str(other.resolve()) in third.detail
    assert _recorded_real_bins() == {"omp": str(other.resolve())}


def test_apply_reports_a_missing_real_binary_and_writes_nothing(bindir, tmp_path):
    plan = _enabled_plan(bindir, tmp_path, kinds=["omp"], real_bin={"omp": str(tmp_path / "nope" / "omp")})

    result = run_plan(plan).results[0]

    assert result.status == "error" and "real binary" in result.detail
    assert not (bindir / "omp").exists()
    assert not harness_launcher.real_bin_config_path().exists()


def test_apply_refuses_a_relative_wrapper_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("RIG_TEST_UNSET_DIR", raising=False)
    plan = _plan({"enabled": True, "dir": "$RIG_TEST_UNSET_DIR/bin", "kinds": ["omp"]}, tmp_path)
    monkeypatch.chdir(tmp_path)

    result = run_plan(plan).results[0]

    assert result.status == "error" and "not absolute" in result.detail
    assert not (tmp_path / "$RIG_TEST_UNSET_DIR").exists()


def test_dry_run_writes_nothing(bindir, tmp_path):
    report = run_plan(_enabled_plan(bindir, tmp_path), dry_run=True)

    assert {r.status for r in report.results} == {"planned"}
    assert not bindir.exists()


# ── drift: rig status reports the per-kind wrapper state ───────────────────────────────────
def _drift(plan: InstallPlan) -> list[tuple[str, str, str]]:
    report = DriftReport()
    for action in plan.actions:
        _check_harness_wrapper(action, report)
    return [(d.direction, d.category, d.item) for d in report.items]


def test_drift_reports_missing_stale_and_foreign_and_is_clean_once_applied(bindir, tmp_path):
    plan = _enabled_plan(bindir, tmp_path, kinds=["omp", "claude", "codex"])
    assert _drift(plan) == [("missing", "harness_wrappers", k) for k in ("omp", "claude", "codex")]

    run_plan(plan)
    assert _drift(plan) == []

    (bindir / "claude").write_text(f"{harness_launcher.WRAPPER_MARKER} (claude)\nold\n", encoding="utf-8")
    (bindir / "codex").unlink()
    (bindir / "codex").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    report = DriftReport()
    for action in plan.actions:
        _check_harness_wrapper(action, report)

    assert [(d.direction, d.item) for d in report.items] == [("modified", "claude"), ("modified", "codex")]
    stale, foreign = report.items
    assert "rig apply commit" in stale.detail
    assert "not written by rig" in foreign.detail and "rig apply commit" in foreign.detail
    assert stale.target == bindir / "claude"


def test_drift_reports_a_real_binary_record_that_disagrees_with_config(bindir, tmp_path):
    real = _real_binary(tmp_path)
    plan = _enabled_plan(bindir, tmp_path, kinds=["omp"], real_bin={"omp": str(real)})
    run_plan(plan)
    assert _drift(plan) == []

    other = _real_binary(tmp_path / "fork")
    retargeted = _enabled_plan(bindir, tmp_path, kinds=["omp"], real_bin={"omp": str(other)})
    report = DriftReport()
    _check_harness_wrapper(retargeted.actions[0], report)

    assert [(d.direction, d.item) for d in report.items] == [("modified", "omp real_bin")]
    assert str(other.resolve()) in report.items[0].detail and "rig apply commit" in report.items[0].detail
    run_plan(retargeted)
    assert _drift(retargeted) == []


def test_drift_does_not_judge_a_real_binary_the_config_does_not_declare(bindir, tmp_path):
    real = _real_binary(tmp_path)
    harness_launcher.install_wrapper("omp", bindir / "omp", real_bin=str(real))

    assert _drift(_enabled_plan(bindir, tmp_path, kinds=["omp"])) == []


def test_drift_reports_a_missing_configured_real_binary(bindir, tmp_path):
    plan = _enabled_plan(bindir, tmp_path, kinds=["omp"], real_bin={"omp": str(tmp_path / "gone" / "omp")})
    run_plan(_enabled_plan(bindir, tmp_path, kinds=["omp"]))

    report = DriftReport()
    _check_harness_wrapper(plan.actions[0], report)

    assert [(d.direction, d.item) for d in report.items] == [("modified", "omp real_bin")]
    assert "existing absolute path" in report.items[0].detail


def test_verify_passes_only_for_a_current_executable_wrapper(bindir, tmp_path):
    from riglib.verify import verify_plan

    plan = _enabled_plan(bindir, tmp_path, kinds=["omp"])
    assert [r.passed for r in verify_plan(plan).results] == [False]

    run_plan(plan)
    assert [r.passed for r in verify_plan(plan).results] == [True]

    (bindir / "omp").chmod(0o644)
    assert [r.passed for r in verify_plan(plan).results] == [False]


# ── config: validation + the GLOBAL-layer-only rule ────────────────────────────────────────
@pytest.mark.parametrize(
    "block",
    [
        {},
        {"enabled": True},
        {"enabled": False, "dir": "~/bin", "kinds": ["omp"], "real_bin": {"omp": "/opt/omp/bin/omp"}},
        {"enabled": True, "dir": "$HOME/bin", "kinds": list(KINDS), "real_bin": {"omp": "~/src/omp"}},
        {"enabled": True, "kinds": ["omp", "codex"], "real_bin": {"codex": "/usr/local/bin/codex"}},
    ],
)
def test_validate_accepts(block):
    validate({"version": 1, "harness_wrappers": block})


@pytest.mark.parametrize(
    "block, message",
    [
        (None, "harness_wrappers must be a mapping"),
        ({"bogus": 1}, "bogus"),
        ({"enabled": "yes"}, "harness_wrappers.enabled must be a bool"),
        ({"dir": ""}, "harness_wrappers.dir"),
        ({"dir": 5}, "harness_wrappers.dir"),
        ({"dir": "relative/bin"}, "harness_wrappers.dir"),
        ({"kinds": []}, "harness_wrappers.kinds must list at least one"),
        ({"kinds": "omp"}, "harness_wrappers.kinds must be a list"),
        ({"kinds": ["omp", "pi"]}, "harness_wrappers.kinds"),
        ({"real_bin": ["omp"]}, "harness_wrappers.real_bin must be a mapping"),
        ({"real_bin": {"pi": "/x"}}, "harness_wrappers.real_bin.pi"),
        ({"real_bin": {"omp": ""}}, "harness_wrappers.real_bin.omp"),
        ({"real_bin": {"omp": "rel/omp"}}, "harness_wrappers.real_bin.omp"),
        ({"real_bin": {"omp": 3}}, "harness_wrappers.real_bin.omp"),
        ({"kinds": ["omp"], "real_bin": {"claude": "/x/claude"}}, "harness_wrappers.real_bin.claude"),
    ],
)
def test_validate_rejects(block, message):
    with pytest.raises(ConfigError) as excinfo:
        validate({"version": 1, "harness_wrappers": block})
    assert message in str(excinfo.value)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_repo_rig_yaml_cannot_set_the_block(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / "rig.yaml", "version: 1\nharness_wrappers:\n  enabled: true\n  real_bin: {omp: /tmp/evil}\n")

    with pytest.raises(ConfigError, match="harness_wrappers is a global-only config block"):
        load(repo)


def test_an_explicit_config_layer_cannot_set_the_block(tmp_path):
    other = tmp_path / "other.yaml"
    _write(other, "version: 1\nharness_wrappers: {enabled: true}\n")

    with pytest.raises(ConfigError, match="global-only"):
        load(tmp_path, explicit_config=other)


def test_the_global_layer_may_set_the_block(tmp_path):
    _write(global_config_path(), "version: 1\nharness_wrappers: {enabled: true, kinds: [omp]}\n")

    loaded = load(tmp_path / "nowhere", include_repo=False)

    assert loaded.data["harness_wrappers"] == {"enabled": True, "kinds": ["omp"]}


# ── registries: the block is wired everywhere a global block must be ───────────────────────
def test_the_block_is_wired_into_every_registry():
    from riglib.action_tags import ACTION_TAGS
    from riglib.areas import AREAS
    from riglib.layers import GLOBAL, layer_for_category
    from riglib.schema import AREAS as WIZARD_AREAS
    from riglib.schema import writable_layer_for_category
    from riglib.verify import _VERIFIERS

    assert "harness_wrapper" in runner._HANDLERS
    assert "harness_wrapper" in ACTION_TAGS
    assert "harness_wrapper" in _VERIFIERS
    assert any(a.key == "harness_wrappers" and a.layer == GLOBAL for a in AREAS)
    assert any(a.category == "harness_wrappers" for a in WIZARD_AREAS)
    assert layer_for_category("harness_wrappers") == GLOBAL
    assert writable_layer_for_category("harness_wrappers") == GLOBAL


def test_the_json_schema_describes_the_block_and_marks_it_global_only():
    doc = config_schema.json_schema()
    node = doc["properties"]["harness_wrappers"]

    assert node["x-rig-global-only"] is True
    assert set(node["properties"]) == {"enabled", "dir", "kinds", "real_bin"}
    assert node["properties"]["kinds"]["items"]["enum"] == list(KINDS)
    assert node["properties"]["kinds"]["default"] == list(KINDS)
    assert node["properties"]["enabled"]["default"] is False
    assert config_schema.schema_file_in_sync(), "schema/rig.schema.json must match the registry"


def test_settings_default_to_all_kinds_and_the_documented_dir():
    settings = wrapper_provision.settings_from_config({"enabled": True})

    assert settings.kinds == KINDS
    assert settings.dir == wrapper_provision.DEFAULT_DIR == "~/.local/share/rig/bin"
    assert settings.real_bin == {}


# ── end to end through the CLI: apply info previews, apply commit converges, status agrees ──
def _cli_env(tmp_path: Path, fake_agent_tools: Path, bindir: Path, extra: str = "") -> Path:
    _write(
        global_config_path(),
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        f"harness_wrappers:\n  enabled: true\n  dir: {bindir}\n  kinds: [omp, claude]\n{extra}",
    )
    workdir = tmp_path / "nongit"
    workdir.mkdir()
    return workdir


def test_cli_preview_writes_nothing_and_commit_then_status_converge(tmp_path, fake_agent_tools, bindir, capsys):
    from riglib.cli import main

    workdir = _cli_env(tmp_path, fake_agent_tools, bindir)

    assert main(["apply", "--only", "harness_wrappers", "-C", str(workdir)]) == 0
    assert not bindir.exists()
    capsys.readouterr()

    # `rig status` also judges every other default-on area of this scratch HOME, so its exit code
    # says nothing about the wrappers: read the wrappers' own area line and drift rows.
    area = "harness launcher wrappers (rig harness exec)"
    main(["status", "-C", str(workdir)])
    drift_out = capsys.readouterr().out
    assert f"{area}: drift (2 declared-but-missing/modified)" in drift_out
    assert "harness_wrappers/omp: wrapper not installed" in drift_out
    assert "harness_wrappers/claude: wrapper not installed" in drift_out

    assert main(["apply", "commit", "--only", "harness_wrappers", "-C", str(workdir)]) == 0
    out = capsys.readouterr().out
    assert "2 changed" in out
    assert sorted(p.name for p in bindir.iterdir()) == ["claude", "omp"]

    assert main(["apply", "commit", "--only", "harness_wrappers", "-C", str(workdir)]) == 0
    assert "0 changed" in capsys.readouterr().out

    main(["status", "-C", str(workdir)])
    synced = capsys.readouterr().out
    assert f"{area}: in sync" in synced
    assert "harness_wrappers/" not in synced


def test_cli_commit_fails_loudly_on_a_foreign_file_and_keeps_it(tmp_path, fake_agent_tools, bindir, capsys):
    from riglib.cli import main

    workdir = _cli_env(tmp_path, fake_agent_tools, bindir)
    bindir.mkdir()
    (bindir / "omp").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

    rc = main(["apply", "commit", "--only", "harness_wrappers", "-C", str(workdir)])

    out = capsys.readouterr().out
    assert rc != 0
    assert "refusing to overwrite" in out and str(bindir / "omp") in out
    assert (bindir / "omp").read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"
    assert harness_launcher.wrapper_state("claude", bindir / "claude") == "current"


def test_cli_commit_writes_the_real_binary_record(tmp_path, fake_agent_tools, bindir, capsys):
    from riglib.cli import main

    real = _real_binary(tmp_path)
    workdir = _cli_env(tmp_path, fake_agent_tools, bindir, extra=f"  real_bin:\n    omp: {real}\n")

    assert main(["apply", "commit", "--only", "harness_wrappers", "-C", str(workdir)]) == 0
    capsys.readouterr()

    record = json.loads(harness_launcher.real_bin_config_path().read_text(encoding="utf-8"))
    assert record == {"omp": str(real.resolve())}
    assert os.access(bindir / "omp", os.X_OK)
