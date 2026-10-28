"""CLI dispatch smoke tests — exercise the argparse front-end end to end (part 2 of 2).
"""
from __future__ import annotations

from riglib.cli import main


def test_no_tui_preview_existing_config_matches_apply(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """A no-TUI `rig init` on a repo with an EXISTING rig.yaml previews THAT config's plan (what
    `rig apply` would do), not the default scaffold — so the preview can't mislead. The fixture
    config disables skills, so the previewed plan must carry NO skill action."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # force a TTY so we exercise the wizard→ImportError→preview branch (no-textual)
    monkeypatch.setattr("riglib.setup_wizard.is_interactive", lambda: True)
    monkeypatch.setattr(
        "riglib.tui.run_wizard",
        lambda _root, stack=None: (_ for _ in ()).throw(ImportError("No module named 'textual'")),
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\n"
        "ci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n",
        encoding="utf-8",
    )
    rc = main(["init", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PREVIEW" in out
    # the existing config disables skills → previewing IT (not the default, which installs skills)
    # must show NO skill action, and must advise `rig apply`.
    assert "skills/" not in out
    assert "rig apply" in out
    # --plan so the per-action lines are listed (small plan would list anyway, but be explicit)
    capsys.readouterr()
    main(["init", "-C", str(repo), "--plan"])
    assert "skills/" not in capsys.readouterr().out


def test_no_tty_preview_existing_config(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """The no-TTY branch (`reason="no-tty"`) with an EXISTING rig.yaml previews THAT config via
    `_load_plan` — same as the no-textual branch, but reached by the TTY gate, not an ImportError."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr("riglib.setup_wizard.is_interactive", lambda: False)  # no TTY
    # textual would import fine here; the wizard must still never launch without a TTY.
    monkeypatch.setattr(
        "riglib.tui.run_wizard",
        lambda _root: (_ for _ in ()).throw(AssertionError("wizard must not launch without a TTY")),
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\nskills: {{enabled: false}}\n"
        "agent_hooks: {enabled: false}\nci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n",
        encoding="utf-8",
    )
    rc = main(["init", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no TTY" in out
    assert "PREVIEW" in out
    assert "skills/" not in out  # existing (skills-disabled) config, not the default scaffold
    # no-tty next-steps advise a TTY, never "install the TUI (hint above)"
    assert "interactive terminal (TTY)" in out
    assert "rig apply" in out


def test_init_external_config_apply_backs_up_then_applies(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """The sharpest branch of `_persist_rig_yaml`: an external `--config` over an EXISTING
    committed rig.yaml, with `--apply`. The old config must be timestamp-backed-up (never
    silently lost), the template committed, AND the plan applied (skills land in HOME)."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rig.yaml").write_text(
        "version: 1\n# OLD customized\nskills: {enabled: false}\n", encoding="utf-8"
    )
    template = tmp_path / "template.yaml"
    template.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {universal: {all: true}, by_type: {enable: [cli]}}\n"
        "agent_hooks: {enabled: false}\nci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n",
        encoding="utf-8",
    )
    rc = main(["init", "-C", str(repo), "--config", str(template), "--yes", "--apply"])
    out = capsys.readouterr().out
    assert rc == 0
    # the OLD config was backed up, not discarded
    assert any(p.name.startswith("rig.yaml.rig-bak-") for p in repo.iterdir())
    # the template is now committed AND applied
    assert "skills" in (repo / "rig.yaml").read_text(encoding="utf-8")
    assert "Applying" in out
    assert (tmp_path / "home" / ".agents" / "skills").exists()


def test_init_external_config_backs_up_without_apply(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """The copy+backup branch on the NON-apply path: external `--config` over an existing
    rig.yaml, no `--apply`. The old config is backed up and the template committed, but the plan
    is only PREVIEWED — nothing is applied."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rig.yaml").write_text(
        "version: 1\n# OLD customized\nskills: {enabled: false}\n", encoding="utf-8"
    )
    template = tmp_path / "template.yaml"
    template.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {universal: {all: true}, by_type: {enable: [cli]}}\n"
        "agent_hooks: {enabled: false}\nci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n",
        encoding="utf-8",
    )
    rc = main(["init", "-C", str(repo), "--config", str(template), "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    assert any(p.name.startswith("rig.yaml.rig-bak-") for p in repo.iterdir())  # old backed up
    assert "skills" in (repo / "rig.yaml").read_text(encoding="utf-8")  # template committed
    assert "PREVIEW of `rig apply`" in out  # only previewed
    assert "Applying" not in out
    assert not (tmp_path / "home" / ".agents" / "skills").exists()  # NOT applied


def test_init_clobber_guard_precedes_catalog_scan(tmp_path, capsys, monkeypatch):
    """The clobber-guard runs BEFORE the catalog scan: an existing rig.yaml + a BROKEN agent-tools
    source must still give the clear 'already exists → run rig apply' (exit 2), not a CatalogError
    — the moved-up guard preserves the pre-refactor error precedence."""
    # a non-existent source makes any Catalog.scan fail — but the guard must fire first.
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rig.yaml").write_text("version: 1\n# customized\nskills: {enabled: false}\n", encoding="utf-8")
    rc = main(["init", "-C", str(repo), "--yes"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "already exists" in out
    assert "agent-tools" not in out  # the catalog error never surfaces — the guard won the race


def test_init_dryrun_over_existing_config_previews_that_config(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`rig init --dry-run` on a repo that ALREADY has a (skills-disabled) rig.yaml previews THAT
    config's plan — what `rig apply` would do — not the default scaffold (which installs skills).
    Consistent with the no-TUI preview; nothing is written."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    original = (
        f"version: 1\nagent_tools_source: {fake_agent_tools}\nskills: {{enabled: false}}\n"
        "agent_hooks: {enabled: false}\nci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n"
    )
    (repo / "rig.yaml").write_text(original, encoding="utf-8")
    rc = main(["init", "-C", str(repo), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PREVIEW of `rig apply`" in out
    assert "skills/" not in out  # existing config disables skills → not the default scaffold
    assert "already exists" in out
    assert "run `rig apply commit`" in out
    assert (repo / "rig.yaml").read_text() == original  # untouched


def test_init_config_is_repo_yaml_reports_no_write(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`rig init --config <the repo's OWN rig.yaml> --yes` writes nothing (the config is already
    in place), so the message must NOT claim a phantom 'scaffolded' write."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\nskills: {{enabled: false}}\n"
        "agent_hooks: {enabled: false}\nci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n",
        encoding="utf-8",
    )
    rc = main(["init", "-C", str(repo), "--config", "rig.yaml", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Nothing written (rig.yaml already in place)" in out
    assert "scaffolded" not in out  # never claim a write that didn't happen
    assert "run `rig apply commit`" in out


def _mk_plan(n_skills: int, n_hooks: int = 0):
    """Synthetic InstallPlan with N skill + M agent-hook actions (deterministic, no catalog)."""
    from pathlib import Path

    from riglib.plan import Action, InstallPlan

    acts = [
        Action(kind="copy_skill", category="skills", item=f"skill-{i}",
               source=Path("/src"), target=Path(f"/dst/skill-{i}"))
        for i in range(n_skills)
    ] + [
        Action(kind="install_agent_hook", category="agent_hooks", item=f"hook-{i}",
               source=Path("/src"), target=Path(f"/dst/hook-{i}"))
        for i in range(n_hooks)
    ]
    return InstallPlan(actions=acts, on_conflict="backup")


def test_print_plan_summarizes_large_plan_by_default(capsys):
    from riglib.cli_plan_print import _print_plan

    _print_plan(_mk_plan(20, 3))
    out = capsys.readouterr().out
    assert "Plan: 23 action(s)" in out
    assert "20 skills" in out and "3 agent-hooks" in out  # per-carrier counts
    assert "run with --plan to list all 23 actions" in out
    # the full per-action wall is NOT dumped by default
    assert "→ /dst/skill-0" not in out


def test_print_plan_full_lists_every_action(capsys):
    from riglib.cli_plan_print import _print_plan

    _print_plan(_mk_plan(20, 3), full=True)
    out = capsys.readouterr().out
    assert "skills/skill-0 → /dst/skill-0" in out
    assert "skills/skill-19" in out
    assert "agent_hooks/hook-2" in out
    assert "run with --plan" not in out  # no summary hint when fully listed


def test_print_plan_small_plan_stays_inline(capsys):
    from riglib.cli_plan_print import _print_plan

    _print_plan(_mk_plan(2, 1))  # 3 actions ≤ inline max → full list, no summary
    out = capsys.readouterr().out
    assert "skills/skill-0 → /dst/skill-0" in out
    assert "run with --plan" not in out


def test_init_plan_flag_lists_full_actions(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`rig init --plan` threads through to a full per-action listing (not the summary)."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rc = main(["init", "-C", str(tmp_path), "--yes", "--dry-run", "--plan"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "skills/shell-timeouts" in out  # a concrete action line, proving full output


def test_setup_default_refuses_existing_config(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    existing = "version: 1\n# my customized config\nskills: {enabled: false}\n"
    (repo / "rig.yaml").write_text(existing, encoding="utf-8")
    rc = main(["init", "-C", str(repo), "--yes"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "already exists" in out
    assert (repo / "rig.yaml").read_text() == existing  # not clobbered


def test_setup_failclosed_leaves_no_config(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """A config that fails catalog validation must not leave a committed rig.yaml behind."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    repo.mkdir()
    template = tmp_path / "template.yaml"
    template.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "ci: {items: {nonexistent_gate: {enabled: true}}}\n",
        encoding="utf-8",
    )
    rc = main(["init", "-C", str(repo), "--config", str(template), "--yes"])
    # error-system v2: an unknown catalog item exits with the unknown-item class (4), not the
    # generic config class (2). The fail-closed guarantee is unchanged: no rig.yaml is written.
    from riglib import errors

    assert rc == errors.EXIT_UNKNOWN_ITEM
    assert not (repo / "rig.yaml").exists()  # fail-closed: no invalid config written


def test_setup_external_config_backs_up_existing(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    old = "version: 1\n# customized\nskills: {enabled: false}\nagent_hooks: {enabled: false}\n" \
          "ci: {enabled: false}\nmcp: {enabled: false}\ngit_hooks: {dispatcher: {enabled: false}}\n"
    (repo / "rig.yaml").write_text(old, encoding="utf-8")
    template = tmp_path / "template.yaml"
    template.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\nskills: {{enabled: false}}\n"
        "agent_hooks: {enabled: false}\nci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n",
        encoding="utf-8",
    )
    rc = main(["init", "-C", str(repo), "--config", str(template), "--yes"])
    assert rc == 0
    # the old config was backed up, not silently discarded
    assert any(p.name.startswith("rig.yaml.rig-bak-") for p in repo.iterdir())


def test_setup_with_external_config_persists_repo_yaml(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    template = tmp_path / "template.yaml"
    template.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {universal: {all: true}, by_type: {enable: [cli]}}\n"
        "agent_hooks: {enabled: false}\nci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n",
        encoding="utf-8",
    )
    rc = main(["init", "-C", str(repo), "--config", str(template), "--yes"])
    assert rc == 0
    # the external template is now committed into the repo as rig.yaml
    assert (repo / "rig.yaml").is_file()


def test_status_scans_empty_ci_target_for_extras(tmp_path, capsys, fake_agent_tools, monkeypatch):
    import subprocess

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    repo.mkdir()
    # CI is the REPO layer — its extras are only scanned inside a real git repo (error-system
    # v2: a non-git dir has no repo layer), so this must be an actual repository.
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "rogue.yml").write_text("name: rogue\n", encoding="utf-8")
    cfg = repo / "rig.yaml"
    # ci enabled but all:false → zero CI actions, yet an undeclared workflow exists
    cfg.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: true, all: false}\n",
        encoding="utf-8",
    )
    rc = main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert "rogue" in out  # disk→config extra surfaced despite no CI actions
    assert rc == 3  # drift detected


def test_status_scans_all_harness_agent_hook_targets_for_extras(
    tmp_path, capsys, fake_agent_tools, monkeypatch
):
    import subprocess

    home = tmp_path / "home"
    xdg = home / ".config"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    codex_hook = home / ".codex/hooks/rogue-codex.pre-bash.json"
    opencode_hook = xdg / "opencode/hooks/rogue-opencode.pre-bash.json"
    codex_hook.parent.mkdir(parents=True)
    opencode_hook.parent.mkdir(parents=True)
    codex_hook.write_text("{}", encoding="utf-8")
    opencode_hook.write_text("{}", encoding="utf-8")
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\n"
        "agent_hooks: {enabled: true, all: false}\n"
        "harness: {kind: claude-code, kinds: [codex, opencode]}\n"
        "ci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n"
        "permissions: {enabled: false}\n",
        encoding="utf-8",
    )

    rc = main(["status", "-C", str(repo)])
    out = capsys.readouterr().out

    assert rc == 3
    assert str(codex_hook) in out
    assert str(opencode_hook) in out


def test_apply_refuses_without_config(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()  # no rig.yaml, no global config
    rc = main(["apply", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "no rig.yaml" in out
    assert not (tmp_path / "home" / ".agents").exists()  # HOME untouched


def test_apply_relative_config_resolves_under_C(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {universal: {all: true}, by_type: {enable: [cli]}}\n"
        "agent_hooks: {enabled: false}\nci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n",
        encoding="utf-8",
    )
    # relative --config must resolve against -C repo, not the test's cwd
    rc = main(["apply", "-C", str(repo), "--config", "rig.yaml", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Plan:" in out


def test_init_dryrun_writes_nothing(tmp_path, capsys, fake_agent_tools, monkeypatch):
    # `rig init` is the canonical onboarding front door; --dry-run prints the plan, writes nothing.
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rc = main(["init", "-C", str(tmp_path), "--yes", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Plan:" in out
    assert not (tmp_path / "rig.yaml").exists()  # dry-run writes nothing


def test_init_default_scaffold_includes_harness_auto_mode(tmp_path, capsys, fake_agent_tools, monkeypatch):
    # the default scaffold `rig init --yes` writes recommends auto-mode ON (the front door
    # provisions autonomy, made safe by the agent-hook guards it also installs).
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["init", "-C", str(repo), "--yes"])
    assert rc == 0
    written = (repo / "rig.yaml").read_text(encoding="utf-8")
    assert "harness:" in written
    assert "auto_mode: true" in written


def test_export_writes_file(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    out_path = tmp_path / "rig.yaml"
    rc = main(["export", "-C", str(tmp_path), "-o", str(out_path)])
    assert rc == 0
    assert out_path.is_file()
    assert "version: 1" in out_path.read_text(encoding="utf-8")
    # refuses to overwrite without --force
    rc2 = main(["export", "-C", str(tmp_path), "-o", str(out_path)])
    assert rc2 == 2


def _pyproject_project_version() -> str:
    """Read `[project] version` straight from the repo's pyproject.toml — an INDEPENDENT
    parse (not via `riglib._version`) so the drift guard also catches a resolver bug."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    body = text.split("[project]", 1)[1].split("\n[", 1)[0]
    match = re.search(r"""^version\s*=\s*['"]([^'"]+)['"]""", body, re.MULTILINE)
    assert match is not None, "pyproject [project] version not found"
    return match.group(1)


def _force_checkout_resolution(monkeypatch) -> None:
    """Force the dist-absent code path: make `importlib.metadata` report rig-cli absent.

    Since pyproject.toml is now checked FIRST (rig-cli#67), this helper is no longer
    required for the primary production path; ``resolve_version()`` already prefers
    pyproject in a checkout. It is kept to test that the pyproject-only path (zero
    installed dist metadata) still returns the correct version — useful for verifying the
    resolver works on a pristine clone where ``pip install -e .`` was never run.
    """
    from importlib.metadata import PackageNotFoundError

    from riglib import _version

    def _raise(_name):
        raise PackageNotFoundError

    monkeypatch.setattr(_version, "_dist_version", _raise)


def _version_output(capsys, monkeypatch) -> str:
    """Run `rig --version` and return the printed version token (argparse exits 0).

    `riglib.__version__` is resolved once at package import — before any test monkeypatch —
    so re-resolve it now against the (possibly patched) `_version` and patch the value the
    CLI reads, so `--version` reflects the path under test.
    """
    import pytest

    import riglib
    from riglib import _version as _vmod

    monkeypatch.setattr(riglib, "__version__", _vmod.resolve_version())
    # cli imported `__version__` by value (`from . import __version__`); patch its binding.
    monkeypatch.setattr("riglib.cli.__version__", _vmod.resolve_version())

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out.strip()
    # argparse prints `rig <version>`.
    assert out.startswith("rig "), out
    return out.split(" ", 1)[1].strip()


def test_version_prints(capsys, monkeypatch):
    printed = _version_output(capsys, monkeypatch)
    assert printed  # non-empty


def test_version_matches_pyproject_no_drift(capsys, monkeypatch):
    """THE durable guard: `rig --version` must equal pyproject `[project] version`.

    Pinned to the live-checkout path (PackageNotFoundError forced) so it measures REAL
    drift, not local install state. A hardcoded `__version__` literal that nobody bumps
    (the rig-cli#70 bug) cannot drift from pyproject if this passes; reintroduce a literal,
    or bump pyproject without the dynamic read flowing through, and this fails.
    """
    _force_checkout_resolution(monkeypatch)
    assert _version_output(capsys, monkeypatch) == _pyproject_project_version()


def test_version_is_not_the_stale_literal(capsys, monkeypatch):
    """The old permanently-stale `0.1.0` literal is gone — the bump landed."""
    _force_checkout_resolution(monkeypatch)
    assert _version_output(capsys, monkeypatch) != "0.1.0"


def test_resolve_version_falls_back_to_pyproject_in_checkout(monkeypatch):
    """In a live checkout (no installed dist metadata), `resolve_version()` parses pyproject.

    rig runs from its repo via the `bin/rig` sys.path shim, so `importlib.metadata` raises
    PackageNotFoundError; the pyproject fallback must produce the real version, not the
    `0.0.0+unknown` sentinel.
    """
    from riglib import _version

    _force_checkout_resolution(monkeypatch)
    assert _version.resolve_version() == _pyproject_project_version()
    assert _version.resolve_version() != _version._UNKNOWN
    # The independent parser also matches (covers a resolver-vs-parser disagreement).
    assert _version._version_from_pyproject() == _pyproject_project_version()


def test_version_prefers_pyproject_over_stale_dist_metadata(monkeypatch):
    """pyproject.toml wins over stale egg-info left by a previous ``pip install -e .``.

    Reproduces rig-cli#67: after bumping ``pyproject.toml`` the in-tree egg-info still
    reports the OLD version.  ``importlib.metadata`` reads that egg-info and returned the
    stale value; the fix makes ``resolve_version()`` check pyproject first.
    """
    from riglib import _version

    # Simulate stale editable-install egg-info reporting an old version.
    monkeypatch.setattr(_version, "_dist_version", lambda _name: "0.0.0+stale")

    result = _version.resolve_version()
    assert result == _pyproject_project_version(), (
        f"Expected pyproject version, got {result!r} — stale egg-info shadowed pyproject"
    )
    assert result != "0.0.0+stale"


def test_parse_project_version_scoped_and_array_robust():
    """The pyproject `version` parser is scoped to `[project]` and survives `[`-arrays.

    Two ways a naive parse breaks: (a) a `version` key in another table is read by mistake;
    (b) a multi-line array whose continuation line starts with `[` prematurely ends the
    `[project]` table. Both must resolve to the real `[project] version`.
    """
    from riglib import _version

    toml = (
        "[build-system]\n"
        'version = "99.0.0"\n'  # decoy: not the project version
        "\n"
        "[project]\n"
        'name = "rig-cli"\n'
        "classifiers = [\n"
        '    ["a", "b"],\n'  # continuation line starting with `[` — must NOT end the table
        "]\n"
        'version = "0.2.0"\n'
        "\n"
        "[tool.x]\n"
        'version = "1.2.3"\n'  # decoy after the table
    )
    assert _version._parse_project_version(toml) == "0.2.0"
    # No `[project]` table at all → None (resolver then falls to the sentinel).
    assert _version._parse_project_version("[tool.x]\nversion = '1.0.0'\n") is None


def test_version_ignores_adjacent_host_pyproject(monkeypatch):
    """pyproject.toml with a different `[project] name` is not trusted.

    Covers the ``pip install --target ./vendor`` layout where the adjacent pyproject belongs
    to the host project, not to rig-cli. ``_parse_project_version`` with ``require_name``
    must return None for it, causing the resolver to fall through to ``importlib.metadata``.
    """
    from riglib import _version

    host_toml = "[project]\nname = \"some-host-app\"\nversion = \"9.9.9\"\n"
    rig_toml = "[project]\nname = \"rig-cli\"\nversion = \"1.2.3\"\n"

    # Host pyproject → None (name mismatch).
    assert _version._parse_project_version(host_toml, require_name="rig-cli") is None
    # Rig's own pyproject → version (name matches).
    assert _version._parse_project_version(rig_toml, require_name="rig-cli") == "1.2.3"
    # No require_name → still returns version (backward compat for callers that don't guard).
    assert _version._parse_project_version(host_toml) == "9.9.9"

    # End-to-end: when the adjacent file is a host project's, resolve_version falls to dist.
    monkeypatch.setattr(_version, "_version_from_pyproject", lambda: None)
    monkeypatch.setattr(_version, "_dist_version", lambda _name: "0.5.0")
    assert _version.resolve_version() == "0.5.0"


def test_spotlight_sweep_command_drops_sentinels(tmp_path, monkeypatch, capsys):
    # `rig spotlight-sweep` (the launchd job's command) reads the merged spotlight config and
    # drops .metadata_never_index into matched dirs under the configured roots.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    work = tmp_path / "work"
    (work / "proj/node_modules").mkdir(parents=True)
    (work / "proj/dist").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = repo / "rig.yaml"
    cfg.write_text(
        f"version: 1\nspotlight:\n  enabled: true\n  roots:\n    - {work}\n", encoding="utf-8"
    )
    rc = main(["spotlight-sweep", "-C", str(repo), "--config", str(cfg)])
    assert rc == 0
    assert (work / "proj/node_modules/.metadata_never_index").is_file()
    assert (work / "proj/dist/.metadata_never_index").is_file()
    assert "spotlight-sweep:" in capsys.readouterr().out


def test_spotlight_sweep_bare_help(capsys):
    import pytest

    # a bare invocation with -h prints help and exits 0 (argparse).
    with pytest.raises(SystemExit) as exc:
        main(["spotlight-sweep", "-h"])
    assert exc.value.code == 0


def test_apply_exit_nonzero_when_verify_fails(tmp_path, capsys, fake_agent_tools, monkeypatch):
    # a verify FAILURE (a provisioned artifact that did not take effect) flips the apply exit code
    # to non-zero even when every install action itself succeeded.
    from riglib import verify

    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = repo / "rig.yaml"
    cfg.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\n"
        "ci: {enabled: false}\nmcp: {enabled: false}\ngit_hooks: {dispatcher: {enabled: false}}\n",
        encoding="utf-8",
    )

    @verify.register_verifier("provision_permissions")
    def _always_fail(action):
        return [verify.VerifyResult("permissions", "test", False, "forced failure for test")]

    try:
        rc = main(["apply", "commit", "-C", str(repo), "--config", str(cfg)])
        out = capsys.readouterr().out
        assert rc == 1
        assert "forced failure for test" in out
        assert "check(s) FAILED" in out
    finally:
        verify._VERIFIERS.pop("provision_permissions", None)


def test_spotlight_sweep_noop_when_disabled(tmp_path, monkeypatch, capsys):
    # the persistent launchd agent keeps invoking `spotlight-sweep` after a config removal; it must
    # become a no-op (never write sentinels) once spotlight is disabled/absent — the opt-out contract.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    work = tmp_path / "work"
    (work / "proj/node_modules").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = repo / "rig.yaml"
    cfg.write_text(f"version: 1\nspotlight:\n  enabled: false\n  roots:\n    - {work}\n", encoding="utf-8")
    rc = main(["spotlight-sweep", "-C", str(repo), "--config", str(cfg)])
    assert rc == 0
    assert not (work / "proj/node_modules/.metadata_never_index").exists()
    assert "disabled" in capsys.readouterr().out
