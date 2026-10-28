"""CLI dispatch smoke tests — exercise the argparse front-end end to end.
"""
from __future__ import annotations

from riglib.cli import main


def test_help_runs(capsys):
    import pytest

    # argparse exits with SystemExit(0) on --help; the help text must still print.
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "umbrella driver" in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "rig — the dev-environment" in out


def test_doctor_runs(tmp_path, capsys, monkeypatch):
    from riglib import errors

    # isolate HOME so `drift.scan_missing_targets()` can't read the dev machine's real
    # ~/.claude/settings.json (a dead hook there would flip the exit code to MISSING_TARGET).
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "rig doctor" in out
    # 0 = all present; 1 = optional deps missing on the CI box; 127 = a REQUIRED dep absent
    # (the documented missing-dependency class — honored by the exit-code contract).
    assert rc in (0, 1, errors.EXIT_MISSING_DEP)


def test_doctor_missing_required_dep_uses_127_contract(tmp_path, capsys, monkeypatch):
    """A missing REQUIRED dep exits with the documented missing-dependency class (127).

    The --help epilog promises `127  missing dependency`; doctor must honor that (the public
    exit-code contract scripts branch on) instead of the generic non-zero. A pure --yes-less run
    so nothing is installed; only required deps are forced absent.
    """
    from riglib import doctor, errors

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # force every dependency probe to "absent" so missing_required is non-empty.
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    rc = main(["doctor"])  # no --yes → reports + advises, installs nothing
    out = capsys.readouterr().out
    assert "missing dependencies above" in out
    assert rc == errors.EXIT_MISSING_DEP


def _corrupt_repo(tmp_path):
    """Build a throwaway non-bare git repo and corrupt its core.bare → true."""
    import subprocess

    repo = tmp_path / "broken"
    repo.mkdir()
    for args in (["init", "-q", "."], ["config", "core.bare", "true"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return repo


def test_doctor_flags_core_bare_corruption(tmp_path, capsys, monkeypatch):
    """A corrupted cwd checkout (core.bare=true on a work tree) exits with the repo-corrupt class.

    Doctor's dependency picture is irrelevant — repo corruption is the top-precedence failure, so
    it must win the exit code even if every dep is present.
    """
    from riglib import errors

    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # isolate ~/.claude scan
    repo = _corrupt_repo(tmp_path)
    monkeypatch.chdir(repo)
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "core.bare=true" in out
    assert "git is broken there" in out
    assert rc == errors.EXIT_REPO_CORRUPT


def test_doctor_fix_repairs_core_bare(tmp_path, capsys, monkeypatch):
    """`rig doctor --fix` repairs the corruption and then exits 0 (clean).

    Deps are mocked present so the exit code reflects the repair alone — otherwise a missing
    required dep on the host would route to the 127 branch and the assertion would pass vacuously.
    """
    import subprocess

    from riglib import doctor

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor, "_python_present", lambda name: True)
    repo = _corrupt_repo(tmp_path)
    monkeypatch.chdir(repo)
    rc = main(["doctor", "--fix"])
    out = capsys.readouterr().out
    assert "fixed:" in out
    val = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", "core.bare"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert val == "false"
    assert rc == 0  # corruption repaired AND deps present → fully clean


def test_doctor_repo_corrupt_outranks_missing_required_dep(tmp_path, capsys, monkeypatch):
    """The CORE precedence claim: repo corruption (exit 7) beats a missing required dep (127).

    Forces a missing required dep AND a corrupted cwd checkout — the exit code must be 7, not the
    127 missing-dependency class, because a broken .git makes every other check unreliable.
    """
    from riglib import doctor, errors

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)  # every binary "absent"
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    repo = _corrupt_repo(tmp_path)
    monkeypatch.chdir(repo)
    rc = main(["doctor"])  # no --yes → would otherwise exit 127 for the missing required deps
    out = capsys.readouterr().out
    assert "core.bare=true" in out
    assert rc == errors.EXIT_REPO_CORRUPT  # 7 wins over 127


def _git_hookless(repo, *args):
    import subprocess

    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
         "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


def test_doctor_fix_never_touches_legit_bare_repo_worktree(tmp_path, capsys, monkeypatch):
    """End-to-end destructive-trap guard: `rig doctor --fix` run from a worktree of a GENUINE bare
    repo must NOT flag it and must NOT rewrite the bare repo's shared core.bare (which would break
    that legitimate setup)."""
    from riglib import errors

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git_hookless(seed, "init", "-q", "-b", "main", ".")
    (seed / "f").write_text("x", encoding="utf-8")
    _git_hookless(seed, "add", "f")
    _git_hookless(seed, "commit", "-qm", "i")
    bare = tmp_path / "repo.git"
    _git_hookless(tmp_path, "init", "-q", "-b", "main", "--bare", str(bare))
    _git_hookless(seed, "push", "-q", str(bare), "main")
    wt = tmp_path / "bare-wt"
    _git_hookless(bare, "worktree", "add", "-q", str(wt), "main")

    monkeypatch.chdir(wt)
    rc = main(["doctor", "--fix"])
    out = capsys.readouterr().out
    assert "corrupted git config" not in out  # not flagged
    assert "fixed:" not in out  # nothing repaired
    # the genuine bare repo's core.bare is untouched (still true → still a valid bare repo)
    assert _git_hookless(bare, "config", "--get", "core.bare").stdout.strip() == "true"
    assert rc != errors.EXIT_REPO_CORRUPT


def test_doctor_fix_then_missing_dep_releases_precedence_to_127(tmp_path, capsys, monkeypatch):
    """After --fix REPAIRS the corruption, the exit code must reflect the NEXT problem (a missing
    required dep → 127), not stay at 7 — the repo-corrupt precedence is released once it's fixed."""
    from riglib import doctor, errors

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)  # required deps absent
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    repo = _corrupt_repo(tmp_path)
    monkeypatch.chdir(repo)
    rc = main(["doctor", "--fix"])  # repairs core.bare, then surfaces the missing deps
    out = capsys.readouterr().out
    assert "fixed:" in out
    assert rc == errors.EXIT_MISSING_DEP  # 127, not 7 — corruption gone, deps now govern


def test_doctor_repo_corrupt_outranks_missing_target(tmp_path, capsys, monkeypatch):
    """Precedence: repo corruption (exit 7) also beats a missing-target (exit 5).

    Plants a dead hook reference in the harness settings.json AND a corrupted cwd checkout; the
    exit code must be 7, matching the help epilog's "doctor exits 7 ahead of any other class".
    """
    import json

    from riglib import errors

    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    # a hook command pointing at a script that does not exist → a missing-target finding
    settings = {
        "hooks": {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": str(tmp_path / "gone" / "hook.py")}]}
            ]
        }
    }
    (claude / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    repo = _corrupt_repo(tmp_path)
    monkeypatch.chdir(repo)
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "missing targets" in out  # the dead target IS reported …
    assert "core.bare=true" in out  # … alongside the corruption …
    assert rc == errors.EXIT_REPO_CORRUPT  # … but repo-corrupt (7) wins the exit code over 5


def test_doctor_fix_failure_advises_manual_not_rerun(tmp_path, capsys, monkeypatch):
    """When --fix is given but the repair FAILS, advise the manual fix, not a useless `--fix` re-run."""
    from riglib import core_bare, errors

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(core_bare, "fix_core_bare", lambda finding: False)  # simulate a write failure
    repo = _corrupt_repo(tmp_path)
    monkeypatch.chdir(repo)
    rc = main(["doctor", "--fix"])
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "re-run `rig doctor --fix`" not in out  # the misleading advice must NOT appear
    assert rc == errors.EXIT_REPO_CORRUPT


def test_setup_dryrun_default(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rc = main(["init", "-C", str(tmp_path), "--yes", "--dry-run"])
    out = capsys.readouterr().out
    assert "Plan:" in out
    assert rc == 0
    assert not (tmp_path / "rig.yaml").exists()  # dry-run writes nothing


def test_apply_dryrun_with_config(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = tmp_path / "rig.yaml"
    cfg.write_text(
        f"agent_tools_source: {fake_agent_tools}\n"
        "skills: {universal: {all: true}, by_type: {enable: [cli]}}\n"
        "agent_hooks: {enabled: false}\nci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n",
        encoding="utf-8",
    )
    # --plan forces the full per-action list (the default condenses a large plan to a summary).
    rc = main(["apply", "-C", str(tmp_path), "--config", str(cfg), "--dry-run", "--plan"])
    out = capsys.readouterr().out
    assert "Plan:" in out
    assert "skills/shell-timeouts" in out
    assert rc == 0


def test_apply_rejects_malformed_config_three_part(tmp_path, capsys, fake_agent_tools, monkeypatch):
    # roadmap §5 "it should work" = a malformed config fails LOUDLY on apply: exit 2, the 3-part
    # block, and the schema path of the offending key — not a silent partial reconcile.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = tmp_path / "rig.yaml"
    cfg.write_text(
        f"agent_tools_source: {fake_agent_tools}\nharness: {{aut_mode: true}}\n",  # typo key
        encoding="utf-8",
    )
    rc = main(["apply", "-C", str(tmp_path), "--config", str(cfg), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "unknown harness key: aut_mode" in out
    assert "schema path: harness.aut_mode" in out
    assert "fix:" in out


def test_schema_command_prints_valid_json(capsys):
    rc = main(["schema"])
    assert rc == 0
    import json

    doc = json.loads(capsys.readouterr().out)
    assert doc["$schema"].startswith("http://json-schema.org/draft-07")
    assert doc["additionalProperties"] is False
    assert "harness" in doc["properties"]


def test_schema_command_check_passes_for_committed_file(capsys):
    # the committed schema file must be in sync — `rig schema --check` is a CI-usable gate.
    rc = main(["schema", "--check"])
    assert rc == 0
    assert "in sync" in capsys.readouterr().out


def test_schema_check_fails_when_file_missing(tmp_path, capsys, monkeypatch):
    # point the resolver at a non-existent file → --check must exit 2 and say how to fix it.
    target = tmp_path / "schema" / "rig.schema.json"
    monkeypatch.setattr("riglib.config_schema.schema_file_path", lambda: target)
    rc = main(["schema", "--check"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "missing or out of sync" in out
    assert "rig schema --write" in out


def test_schema_check_fails_when_file_stale(tmp_path, capsys, monkeypatch):
    # a hand-edited / stale file (wrong bytes) is drift → --check exits 2.
    target = tmp_path / "schema" / "rig.schema.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"stale": true}\n', encoding="utf-8")
    monkeypatch.setattr("riglib.config_schema.schema_file_path", lambda: target)
    rc = main(["schema", "--check"])
    assert rc == 2
    assert "out of sync" in capsys.readouterr().out


def test_schema_write_regenerates_file(tmp_path, capsys, monkeypatch):
    # --write (re)generates the file from the registry; a follow-up --check then passes.
    from riglib import config_schema

    target = tmp_path / "schema" / "rig.schema.json"
    monkeypatch.setattr("riglib.config_schema.schema_file_path", lambda: target)
    assert not target.exists()
    rc = main(["schema", "--write"])
    assert rc == 0
    assert "wrote" in capsys.readouterr().out
    assert target.read_text(encoding="utf-8") == config_schema.render_schema_json()
    assert main(["schema", "--check"]) == 0


def test_setup_dryrun_never_launches_wizard(tmp_path, capsys, fake_agent_tools, monkeypatch):
    # even though the wizard import would succeed here, --dry-run must stay headless.
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    called = {"wizard": False}

    def _boom(_root):
        called["wizard"] = True
        return 0

    monkeypatch.setattr("riglib.tui.app.run_wizard", _boom, raising=False)
    rc = main(["init", "-C", str(tmp_path), "--dry-run"])
    assert rc == 0
    assert not called["wizard"]
    assert "Plan:" in capsys.readouterr().out


def test_init_stack_flag_reaches_the_wizard(tmp_path, fake_agent_tools, monkeypatch):
    # a TTY `rig init --stack …` must thread the explicit preset through to the wizard, not
    # silently drop it at the interactive boundary (regression: the wizard ignored --stack).
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr("riglib.setup_wizard.is_interactive", lambda: True)
    monkeypatch.setattr("riglib.cli_init._tui_importable", lambda: True)
    monkeypatch.delenv("RIG_NO_TUI", raising=False)
    seen = {}

    def _capture(root, stack=None):
        seen["root"] = str(root)
        seen["stack"] = stack
        return 0

    monkeypatch.setattr("riglib.tui.app.run_wizard", _capture, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["init", "-C", str(repo), "--stack", "backend/python"])
    assert rc == 0
    assert seen["stack"] == "backend/python"


def test_textual_fallback_previews_and_writes_nothing(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """textual is a CORE dependency now (it ships WITH rig), so it is missing only on a genuinely
    broken environment. When it IS missing, `rig init` must NOT silently scaffold+apply — it shows
    a non-destructive PREVIEW (writes nothing, applies nothing) plus a SINGLE-LINE broken-env
    message (no multi-step install hint) and how-to-proceed.

    This is the non-PEP-668 shape (a managed interpreter where textual is missing for some other
    reason) — see test_textual_fallback_pep668_offers_break_system_packages for the PEP-668 case,
    which uses different wording since "broken install" is the wrong diagnosis there."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RIG_NO_TUI", raising=False)

    # force a TTY + a (simulated) broken env where textual isn't importable: the no-textual
    # fallback branch, distinct from the no-TTY branch covered by test_init_no_tty_previews_*.
    monkeypatch.setattr("riglib.setup_wizard.is_interactive", lambda: True)
    monkeypatch.setattr("riglib.cli_init._tui_importable", lambda: False)
    # pin BOTH the PEP-668 branch and which dep is reported missing deterministically — real
    # host state (this box may genuinely be missing textual/rich, or not) must not leak in.
    monkeypatch.setattr("riglib.doctor.externally_managed", lambda: False)
    monkeypatch.setattr("riglib.cli_init._missing_tui_deps", lambda: ["textual"])
    # Pin the absent-vs-too-old classification too (a review finding, k3, round 20):
    # `_setup_preview_no_tui` does its OWN `find_spec`/`textual_too_old()` re-derivation on
    # top of the mocked `_missing_tui_deps` above (to tell "genuinely absent" from
    # "present but too old" for the diagnosis wording) — on a host where textual happens to
    # be genuinely INSTALLED but BELOW the 0.66 floor (a realistic "stale pre-floor-bump
    # venv" state, exactly the population rig-cli#292 targets), the real `textual_too_old()`
    # would return True and flip this test to the wrong branch/wording for an environment
    # reason, not a code bug — the same "fails for the wrong reason" class this diff already
    # fixes in several sibling tests.
    monkeypatch.setattr("riglib.doctor.textual_too_old", lambda: False)
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["init", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    # the one-line broken-env message — NOT the old multi-line do-it-yourself install hint
    assert "textual is missing from rig's environment" in out
    assert "broken install" in out
    # the old verbose install-hint forms are GONE (no "Install it to choose interactively", no
    # `uv tool install 'rig-cli[tui]'`/`pipx inject` hint, no bare-pip extra)
    assert "Install it to choose interactively" not in out
    assert "rig-cli[tui]" not in out
    assert "pip install 'rig-cli[tui]'" not in out
    # principle 1 — the plan is unambiguously a PREVIEW, never read as done work
    assert "Plan:" in out
    assert "PREVIEW" in out
    assert "Nothing was written and nothing was applied" in out
    # principle 3 — concrete ways to proceed (reinstall rig + the headless paths)
    assert "rig init --yes" in out
    assert "reinstall rig" not in out  # this branch's own hint below replaces the old bare phrase
    assert "pipx install hyper-rig" in out
    # principle 2 — no-args mutates nothing
    assert not (repo / "rig.yaml").exists()
    assert not (tmp_path / "home" / ".agents" / "skills").exists()


def test_textual_fallback_pep668_offers_break_system_packages(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """On a PEP-668 externally-managed interpreter (common on modern Homebrew/distro Python —
    this is the exact shape a local `install.sh` dev checkout hits, not a "broken install"), the
    no-textual preview must say so plainly and offer the --break-system-packages fallback that
    actually works for a dev checkout, alongside the recommended pipx path."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RIG_NO_TUI", raising=False)

    monkeypatch.setattr("riglib.setup_wizard.is_interactive", lambda: True)
    monkeypatch.setattr("riglib.cli_init._tui_importable", lambda: False)
    monkeypatch.setattr("riglib.doctor.externally_managed", lambda: True)
    monkeypatch.setattr("riglib.doctor._python_present", lambda name: False)
    # both core TUI deps missing here (not just textual) — exercises the multi-dep naming +
    # fallback-command path, deterministically (real host state must not leak in).
    monkeypatch.setattr("riglib.cli_init._missing_tui_deps", lambda: ["textual", "rich"])
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["init", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "textual + rich are missing" in out
    assert "this Python refuses direct installs (PEP 668)" in out
    assert "broken install" not in out  # wrong diagnosis for this case — must not appear
    assert "pipx install hyper-rig" in out
    assert "--break-system-packages" in out
    assert "pip install 'rig-cli[tui]'" not in out
    assert "Plan:" in out
    assert "PREVIEW" in out
    assert "Nothing was written and nothing was applied" in out
    assert "rig init --yes" in out
    assert not (repo / "rig.yaml").exists()
    assert not (tmp_path / "home" / ".agents" / "skills").exists()


def test_textual_fallback_pep668_upgrades_a_too_old_textual(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """Regression for the "remediation loop" review finding: a present-but-too-old textual
    routes through the same "missing" messaging as a genuinely absent one, but the printed
    fallback command must actually carry a version floor (`textual>=0.66`) -- a bare
    `pip install textual` against an already-installed 0.55 reports "already satisfied" and
    fixes nothing, leaving the user stuck seeing the identical message on every re-run. Also
    asserts the printed command is SHELL-QUOTED (`'textual>=0.66'`, not a bare `>`) -- an
    unquoted `>` in copy-pasted shell text is a redirection operator that would silently strip
    the version constraint on paste. And asserts the DIAGNOSIS line itself says "too old", not
    "missing" -- a review finding: a user with an old-but-installed textual would otherwise go
    hunting for a "missing" package that isn't missing at all.

    `importlib.util.find_spec` is explicitly mocked (not left to the real interpreter) --
    a review finding: an earlier version of this test relied on textual actually being
    importable in the TEST process to reach the "too old" wording branch at all, so it
    would fail for the WRONG reason (environment mismatch, not a real bug) in a hermetic
    venv without textual installed -- exactly the posture `test_tui_wizard.py` defends
    with its own `pytest.importorskip("textual")`."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RIG_NO_TUI", raising=False)

    monkeypatch.setattr("riglib.setup_wizard.is_interactive", lambda: True)
    monkeypatch.setattr("riglib.cli_init._tui_importable", lambda: False)
    monkeypatch.setattr("riglib.doctor.externally_managed", lambda: True)
    monkeypatch.setattr("riglib.doctor.textual_too_old", lambda: True)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr("riglib.cli_init._missing_tui_deps", lambda: ["textual"])
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["init", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "'textual>=0.66'" in out  # shell-quoted (shlex.join), not a bare unquoted `>`
    assert "textual is too old" in out
    assert "textual is missing" not in out


def test_textual_fallback_says_missing_not_too_old_when_genuinely_absent(
    tmp_path, capsys, fake_agent_tools, monkeypatch
):
    """Regression for a review finding on the "too old" wording fix itself: `textual_too_old()`
    fails closed (True) for a genuinely absent textual too (its own documented contract, since
    `PackageNotFoundError` can't be told apart from "importable but unparseable" without also
    checking `find_spec`) -- so gating the "too old" wording on `textual_too_old()` alone
    mislabels a genuinely absent textual as "too old" instead of "missing", the mirror image
    of the bug this wording distinction exists to fix. Must check `find_spec` first."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RIG_NO_TUI", raising=False)

    monkeypatch.setattr("riglib.setup_wizard.is_interactive", lambda: True)
    monkeypatch.setattr("riglib.cli_init._tui_importable", lambda: False)
    monkeypatch.setattr("riglib.doctor.externally_managed", lambda: False)
    # the REAL-WORLD state for genuine absence: find_spec fails, and textual_too_old() fails
    # closed (True) as its own contract documents -- do NOT mock textual_too_old itself, or
    # this test can't distinguish the bug from the fix.
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    monkeypatch.setattr("riglib.cli_init._missing_tui_deps", lambda: ["textual"])
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["init", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "textual is missing" in out
    assert "textual is too old" not in out


def test_textual_too_old_non_pep668_gets_a_direct_upgrade_command(
    tmp_path, capsys, fake_agent_tools, monkeypatch
):
    """Regression for a review finding: on a NON-externally-managed interpreter (pyenv, the
    python.org macOS installer, asdf -- none carry PEP 668's marker), a present-but-too-old
    textual used to get ONLY the generic "pipx install hyper-rig" / "uv tool install hyper-rig"
    reinstall suggestions -- neither of which upgrades textual in a dev-checkout's CURRENT
    interpreter (a fresh pipx/uv-tool install creates a SEPARATE managed environment that
    never touches the running symlinked checkout at all, the diff's own PEP-668-branch
    comment already explains this for that branch). The user was stuck reproducing the
    identical "too old" diagnosis on every re-run -- the exact remediation-loop class this
    whole mechanism exists to close, just reopened on the one branch that lacked the fix."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RIG_NO_TUI", raising=False)

    monkeypatch.setattr("riglib.setup_wizard.is_interactive", lambda: True)
    monkeypatch.setattr("riglib.cli_init._tui_importable", lambda: False)
    monkeypatch.setattr("riglib.doctor.externally_managed", lambda: False)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr("riglib.doctor.textual_too_old", lambda: True)
    monkeypatch.setattr("riglib.cli_init._missing_tui_deps", lambda: ["textual"])
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["init", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "textual is too old" in out
    assert "'textual>=0.66'" in out  # a real, executable upgrade command for THIS interpreter
    assert "pipx install hyper-rig" in out  # still offered as the alternative
    # a review finding: the alternative needs the SAME "remove ~/.local/bin/rig first"
    # caveat the PEP-668 branch already has -- a bare "pipx install hyper-rig" leaves an
    # existing dev-checkout symlink pointed at the same too-old install.
    assert "remove ~/.local/bin/rig" in out
    # a review finding: "a broken install" is the wrong diagnosis for "too old" -- a pacman
    # package or a deliberately-pinned venv isn't broken.
    assert "a broken install" not in out


def test_textual_fallback_names_the_dep_actually_missing(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`_tui_importable()` fails when EITHER textual OR rich is absent — with textual present
    and only rich missing, the message must say "rich", never hardcode "textual" (review
    finding: contradictory guidance when the wrong name is reported)."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RIG_NO_TUI", raising=False)

    monkeypatch.setattr("riglib.setup_wizard.is_interactive", lambda: True)
    monkeypatch.setattr("riglib.cli_init._tui_importable", lambda: False)
    monkeypatch.setattr("riglib.doctor.externally_managed", lambda: False)
    monkeypatch.setattr("riglib.cli_init._missing_tui_deps", lambda: ["rich"])
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["init", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "rich is missing from rig's environment" in out
    assert "textual is missing" not in out


def test_missing_tui_deps_real_check_against_this_interpreter():
    """Unmocked: `_missing_tui_deps()`'s real `find_spec` + version-gate path returns exactly
    the subset of {"textual", "rich"} that genuinely can't run the wizard in THIS process —
    every other test monkeypatches this function away, so exercise the real implementation at
    least once. Regression for a review finding: the ORIGINAL version of this test computed
    its expectation from `find_spec` alone, which encoded the pre-rig-cli#292 contract — on
    any interpreter with a genuinely-installed-but-too-old textual (a CI image pinned below
    the 0.66 floor), that would fail while the implementation was behaving correctly; folding
    `doctor.textual_too_old()` into the expectation matches the real contract everywhere."""
    import importlib.util

    from riglib import doctor
    from riglib.cli_tui import _missing_tui_deps

    expected = [
        m
        for m in ("textual", "rich")
        if importlib.util.find_spec(m) is None or (m == "textual" and doctor.textual_too_old())
    ]
    assert _missing_tui_deps() == expected


def test_missing_tui_deps_reports_textual_when_present_but_too_old(monkeypatch):
    """Regression for rig-cli#292: `refresh_bindings()` (called by the wizard's
    `_set_controls_enabled`) was added to Textual between 0.60 and 0.63 -- a present-but-older
    textual (a distro package, a stale pre-floor-bump venv) must be reported as needing
    (re)install, not silently treated as satisfied just because it imports.

    Mock `find_spec` explicitly too (a review finding, round 18: an earlier version of this
    test mocked only `textual_too_old`, so on a hermetic venv without textual installed at
    all, `find_spec` returns None and textual lands in the list via the ABSENT path instead
    -- the test passes green without the mocked `textual_too_old` ever actually deciding
    anything, the same "passes for the wrong reason" class the sibling test right below
    documents and fixes)."""
    from riglib.cli_tui import _missing_tui_deps

    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr("riglib.doctor.textual_too_old", lambda: True)
    assert "textual" in _missing_tui_deps()


def test_missing_tui_deps_omits_textual_when_new_enough(monkeypatch):
    """`textual_too_old` alone doesn't gate whether textual is reported missing --
    `_missing_tui_deps` ALSO does a real `find_spec("textual")` first (a review finding:
    an earlier version of this test relied on textual actually being importable in the
    TEST process to reach the "not missing" branch, so it would fail for the WRONG reason
    -- environment mismatch, not a real bug -- in a hermetic venv without textual
    installed). Mock `find_spec` explicitly rather than relying on the real interpreter."""
    from riglib.cli_tui import _missing_tui_deps

    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr("riglib.doctor.textual_too_old", lambda: False)
    assert "textual" not in _missing_tui_deps()


def test_tui_importable_false_when_textual_present_but_too_old(monkeypatch):
    """The other half of the same regression: `_tui_importable()` -- the gate `cmd_setup`
    actually checks before launching the wizard -- must refuse a too-old textual too, not just
    report it in the diagnostic list. Without this, `rig init` launches a wizard that crashes
    mid-Apply the moment `_set_controls_enabled` calls `refresh_bindings()`."""
    import importlib.util

    from riglib.cli_tui import _tui_importable

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr("riglib.doctor.textual_too_old", lambda: True)
    assert _tui_importable() is False


def test_tui_importable_true_when_present_and_new_enough(monkeypatch):
    import importlib.util

    from riglib.cli_tui import _tui_importable

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr("riglib.doctor.textual_too_old", lambda: False)
    assert _tui_importable() is True


def test_textual_and_rich_are_core_dependencies():
    """The TUI ships WITH rig: textual + rich are CORE runtime deps in pyproject's
    `[project].dependencies`, so `pipx install hyper-rig` / `uv tool install hyper-rig` / `pip install
    rig-cli` all bring the wizard — never an optional `[tui]` extra the user must add separately."""
    # tomllib is stdlib on Python 3.11+; use the backport on 3.10.
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    from pathlib import Path

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    names = {d.split(">")[0].split("=")[0].split("[")[0].strip().lower() for d in deps}
    assert "textual" in names, f"textual must be a core dep, got {deps}"
    assert "rich" in names, f"rich must be a core dep, got {deps}"
    # the old optional `[tui]` extra is gone — it is no longer an opt-in step.
    assert "tui" not in data["project"].get("optional-dependencies", {})


def test_textual_floor_matches_across_pyproject_install_sh_and_doctor():
    """The `textual>=X.Y` floor is spelled out in THREE places that must never drift apart
    (each carries its own comment saying so, but nothing enforced it): `pyproject.toml`'s
    `dependencies`, `install.sh`'s first-bootstrap probe, and `riglib.doctor._TEXTUAL_MIN_VERSION`
    (the actual runtime gate `_tui_importable`/`textual_too_old` compare against). A future
    edit to only one or two of the three would silently reopen the rig-cli#292 crash for
    whichever site fell behind."""
    import re
    from pathlib import Path

    from riglib import doctor

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    repo_root = Path(__file__).resolve().parent.parent
    floor_str = ".".join(str(n) for n in doctor._TEXTUAL_MIN_VERSION)

    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject["project"]["dependencies"]
    textual_dep = next(d for d in deps if d.split(">")[0].split("=")[0].strip().lower() == "textual")
    assert textual_dep == f"textual>={floor_str}", (
        f"pyproject.toml's textual floor ({textual_dep!r}) is out of sync with "
        f"doctor._TEXTUAL_MIN_VERSION ({floor_str!r})"
    )

    install_sh = (repo_root / "install.sh").read_text(encoding="utf-8")
    match = re.search(r'_missing_core\+=\("textual>=([0-9.]+)"\)', install_sh)
    assert match is not None, "install.sh's textual floor entry not found in the expected form"
    assert match.group(1) == floor_str, (
        f"install.sh's textual floor ({match.group(1)!r}) is out of sync with "
        f"doctor._TEXTUAL_MIN_VERSION ({floor_str!r})"
    )


def test_install_sh_echoes_missing_deps_shell_quoted_not_raw_star_join():
    """Regression for a review finding (GLM-cc-last, round 21): `install.sh`'s copy-paste
    safety fix (`pkgs=$(printf '%q ' "${_missing_core[@]}")` instead of the bare
    `"${_missing_core[*]}"` join) had zero test coverage -- the floor-sync test above only
    regexes the EXECUTED probe line, never the DISPLAYED one. A future "simplification"
    reverting to the bare join would pass the whole suite green while reopening the exact
    bug the fix exists to close: an unquoted `>` in an echoed `textual>=0.66` is a shell
    redirection operator when copy-pasted, silently truncating the package list and
    creating a stray file named `=0.66`. Pin the SOURCE shape directly (a real shell
    invocation would need `import textual` etc. to actually be absent, which isn't
    reliable/hermetic in a unit test -- the source-shape check is what's actionable here)."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    install_sh = (repo_root / "install.sh").read_text(encoding="utf-8")

    assert "printf '%q '" in install_sh, (
        "install.sh no longer shell-quotes the displayed missing-deps list via printf %q"
    )
    # the raw `[*]`-join form must not reappear anywhere in an echoed line (it's fine, and
    # still present, in the EXECUTED `"${_missing_core[@]}"` array-expansion form).
    assert '"${_missing_core[*]}"' not in install_sh, (
        "install.sh reverted to the unquoted bare space-joined display form"
    )


def test_init_tty_launches_tui_directly_no_install_step(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """A TTY `rig init` with textual present (the normal case, since it ships with rig) launches
    the wizard DIRECTLY — no install subprocess, no "installing the setup UI" line, no hint."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RIG_NO_TUI", raising=False)
    monkeypatch.setattr("riglib.setup_wizard.is_interactive", lambda: True)
    monkeypatch.setattr("riglib.cli_init._tui_importable", lambda: True)

    def _no_subprocess(*args, **kwargs):
        raise AssertionError("rig init must NOT run any install subprocess — textual ships with rig")

    monkeypatch.setattr("subprocess.run", _no_subprocess)
    launched = {"v": False}

    def _wizard(_root, stack=None):
        launched["v"] = True
        return 0

    monkeypatch.setattr("riglib.tui.run_wizard", _wizard)
    rc = main(["init", "-C", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert launched["v"] is True
    assert "installing the setup UI" not in out
    assert "Install it to choose interactively" not in out
    assert "rig-cli[tui]" not in out


def test_init_no_tty_previews_without_launching_wizard(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """A bare `rig init` in a NON-TTY context (piped / CI / agent) must NOT launch the fullscreen
    wizard (it would hang) — it falls to the non-destructive PREVIEW, writing nothing."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr("riglib.setup_wizard.is_interactive", lambda: False)
    # even though textual would "import" fine here, the wizard must never be reached without a TTY.
    monkeypatch.setattr(
        "riglib.tui.run_wizard",
        lambda _root: (_ for _ in ()).throw(AssertionError("wizard must not launch without a TTY")),
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["init", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no TTY" in out
    assert "PREVIEW" in out
    assert "Nothing was written and nothing was applied" in out
    assert not (repo / "rig.yaml").exists()
    assert not (tmp_path / "home" / ".agents" / "skills").exists()


def test_init_no_tui_flag_skips_wizard(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`rig init --no-tui` never launches the wizard — just a non-destructive preview."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RIG_NO_TUI", raising=False)
    monkeypatch.setattr("riglib.setup_wizard.is_interactive", lambda: True)
    monkeypatch.setattr(
        "riglib.tui.run_wizard",
        lambda _root: (_ for _ in ()).throw(AssertionError("wizard must not launch under --no-tui")),
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["init", "-C", str(repo), "--no-tui"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "TUI disabled" in out
    assert "PREVIEW" in out
    assert "Nothing was written and nothing was applied" in out
    assert not (repo / "rig.yaml").exists()


def test_init_rig_no_tui_env_skips_wizard(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`RIG_NO_TUI=1` is the env half of --no-tui: no wizard, just a preview."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RIG_NO_TUI", "1")
    monkeypatch.setattr("riglib.setup_wizard.is_interactive", lambda: True)
    monkeypatch.setattr(
        "riglib.tui.run_wizard",
        lambda _root: (_ for _ in ()).throw(AssertionError("wizard must not launch when RIG_NO_TUI is set")),
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["init", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "TUI disabled" in out
    assert "Nothing was written and nothing was applied" in out
    assert not (repo / "rig.yaml").exists()


def test_tui_opted_out_env_parsing(monkeypatch):
    """RIG_NO_TUI is truthy for anything except empty / 0 / false / no / off (case-insensitive)."""
    import riglib.cli_tui as cli

    for falsy in ("", "0", "false", "FALSE", "no", "off", "  "):
        monkeypatch.setenv("RIG_NO_TUI", falsy)
        assert cli._tui_opted_out() is False
    for truthy in ("1", "true", "yes", "on", "2", "anything"):
        monkeypatch.setenv("RIG_NO_TUI", truthy)
        assert cli._tui_opted_out() is True
    monkeypatch.delenv("RIG_NO_TUI", raising=False)
    assert cli._tui_opted_out() is False


def test_init_yes_scaffolds_without_applying(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`rig init --yes` writes rig.yaml (the committed config) but does NOT apply — the plan is
    framed as a PREVIEW of `rig apply`, and nothing lands on disk under HOME."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["init", "-C", str(repo), "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    # DONE: rig.yaml is scaffolded (config only)
    assert (repo / "rig.yaml").is_file()
    assert "wrote" in out
    assert "NOTHING applied" in out
    # PREVIEW: the plan is explicitly not applied, and points at `rig apply`
    assert "PREVIEW of `rig apply`" in out
    assert "run `rig apply commit`" in out
    # not applied: no skills installed into the (isolated) HOME
    assert not (tmp_path / "home" / ".agents" / "skills").exists()


def test_init_apply_flag_applies(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`rig init --yes --apply` is the explicit one-shot: it writes rig.yaml AND applies the
    plan (skills land under the isolated HOME), reporting what was DONE."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["init", "-C", str(repo), "--yes", "--apply"])
    out = capsys.readouterr().out
    assert rc == 0
    assert (repo / "rig.yaml").is_file()
    # applied: the apply header + a real results Summary, and skills actually installed
    assert "Applying" in out
    assert "Summary:" in out
    assert "PREVIEW" not in out  # an apply is DONE work, never labeled a preview
    assert (tmp_path / "home" / ".agents" / "skills").exists()


def test_init_apply_alone_is_headless_oneshot(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`rig init --apply` (no --yes, no --config) is an explicit instruction, so it stays
    HEADLESS (never the wizard/preview) and bootstraps the default rig.yaml + applies it."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # a bare-init code path would try the TUI; --apply must NOT go there.
    monkeypatch.setattr(
        "riglib.tui.run_wizard",
        lambda _root: (_ for _ in ()).throw(AssertionError("wizard must not launch under --apply")),
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["init", "-C", str(repo), "--apply"])
    out = capsys.readouterr().out
    assert rc == 0
    assert (repo / "rig.yaml").is_file()  # default scaffold written
    assert "Applying" in out
    assert (tmp_path / "home" / ".agents" / "skills").exists()  # and applied


def test_init_apply_over_existing_config_refuses(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`rig init --apply` on a repo that ALREADY has a customized rig.yaml hits the clobber-guard
    (exit 2) — init never overwrites a committed config with the default; use `rig apply`."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    existing = "version: 1\n# customized\nskills: {enabled: false}\n"
    (repo / "rig.yaml").write_text(existing, encoding="utf-8")
    rc = main(["init", "-C", str(repo), "--apply"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "already exists" in out
    assert (repo / "rig.yaml").read_text() == existing  # not clobbered
    assert not (tmp_path / "home" / ".agents" / "skills").exists()  # nothing applied


def test_init_apply_dryrun_suppresses_apply(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`--dry-run` outranks `--apply`: the plan is previewed, nothing is written, nothing applied."""
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["init", "-C", str(repo), "--yes", "--apply", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Plan:" in out
    assert "dry-run" in out
    assert "Applying" not in out  # --apply is suppressed by --dry-run
    assert not (repo / "rig.yaml").exists()
    assert not (tmp_path / "home" / ".agents" / "skills").exists()
