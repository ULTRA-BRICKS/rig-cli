"""`gh ship` delegator provisioning tests: content area (see test_ship_delegator_content for overview)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from riglib.actions.ship_delegator import (
    ship_delegator_content,
    ship_env_file_content,
    ship_env_file_path,
)
from riglib.drift import detect
from tests.ship_delegator_helpers import (
    _action,
    _apply,
    _canonical_ship,
    _delegator_path,
    _dropped_action_env_items,
    _git_repo,
    _plan_with_action,
    _script_env,
    _write_env_file,
)

# ── delegator content ──────────────────────────────────────────────────────────────
def test_content_is_executable_bash_that_delegates():
    text = ship_delegator_content()
    assert text.startswith("#!/usr/bin/env bash\n")
    # must reference ci/ship/ship.sh (both the repo-local branch and the AGENT_TOOLS_ROOT fallback)
    assert "ci/ship/ship.sh" in text
    # resolves AGENT_TOOLS_ROOT from the env or the machine-level env file — never a baked path
    assert "AGENT_TOOLS_ROOT" in text
    assert "agent-tools/env" in text
    assert "exec" in text


def test_content_is_a_portable_byte_stable_constant(tmp_path):
    # The fix for rig-cli#108: the rendered delegator is a pure constant — byte-stable across
    # renders and with NO machine-specific absolute path baked in — so a repo that COMMITS the
    # file (agent-tools#151) byte-matches what rig renders and a re-apply never dirties the tree.
    text = ship_delegator_content()
    assert ship_delegator_content() == text  # byte-stable
    assert str(tmp_path) not in text
    assert "/Users/" not in text and "/home/" not in text  # no machine home path
    # the machine root is reached only through the portable XDG env-file location
    # (${HOME:-} so `set -u` survives a sanitized env with an explicit AGENT_TOOLS_ROOT)
    assert "${XDG_CONFIG_HOME:-${HOME:-}/.config}/agent-tools/env" in text
    # the script is valid bash
    import subprocess as _sp

    rc = _sp.run(["bash", "-n"], input=text, capture_output=True, text=True)
    assert rc.returncode == 0, rc.stderr


def test_env_file_shell_quotes_a_dangerous_root(tmp_path):
    # agent_tools_source derives from user-controlled config and the delegator SOURCES the env
    # file, so a root with $(...) / backticks / spaces must be INERT — assigned literally, never
    # executed when `gh ship` runs.
    import subprocess as _sp

    pwned = tmp_path / "pwned"
    evil_root = f"/tmp/$(touch {pwned})/`id`"
    evil = Path(evil_root) / "ci" / "ship" / "ship.sh"
    content = ship_env_file_content(evil)
    # the raw injection must NOT appear unquoted
    assert "AGENT_TOOLS_ROOT=/tmp/$(touch" not in content
    # the ROOT (parent.parent.parent of ship.sh) is present, single-quoted; ship.sh path is not
    assert f"'{evil_root}'" in content
    assert "ci/ship/ship.sh'" not in content
    # sourcing the file yields the LITERAL path and executes nothing
    res = _sp.run(
        ["bash", "-c", 'source /dev/stdin; printf %s "$AGENT_TOOLS_ROOT"'],
        input=content, capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout == evil_root
    assert not pwned.exists(), "the injection was EXECUTED while sourcing the env file"


def test_delegator_execs_canonical_ship_via_env_file(tmp_path):
    # END-TO-END through the machine env file: no AGENT_TOOLS_ROOT in the environment, the
    # delegator sources ${XDG_CONFIG_HOME}/agent-tools/env (isolated by conftest) and execs the
    # canonical ship.sh, forwarding args. The canonical writes its args to a file as proof.
    out_marker = tmp_path / "ran.txt"
    canonical = tmp_path / "agent-tools" / "ci" / "ship" / "ship.sh"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(f'#!/usr/bin/env bash\necho "ship-ran $*" > {out_marker}\n', encoding="utf-8")
    canonical.chmod(0o755)
    _write_env_file(canonical)
    script = tmp_path / "pr-ship.sh"
    script.write_text(ship_delegator_content(), encoding="utf-8")
    script.chmod(0o755)
    # run from a NON-git dir so the repo-local branch is skipped and the env-file path is used
    import subprocess as _sp

    rundir = tmp_path / "run"
    rundir.mkdir()
    res = _sp.run([str(script), "42", "--dry-run"], cwd=rundir, capture_output=True, text=True,
                  env=_script_env())
    assert res.returncode == 0, res.stderr
    assert out_marker.read_text(encoding="utf-8").strip() == "ship-ran 42 --dry-run"


def test_delegator_env_var_wins_over_env_file(tmp_path):
    # an explicit AGENT_TOOLS_ROOT in the environment must SHADOW the env file (point a shell at
    # another checkout without re-running rig apply).
    import subprocess as _sp

    file_marker = tmp_path / "file_ran.txt"
    env_marker = tmp_path / "env_ran.txt"
    file_root = tmp_path / "at-file"
    env_root = tmp_path / "at-env"
    for root, marker in ((file_root, file_marker), (env_root, env_marker)):
        ship = root / "ci" / "ship" / "ship.sh"
        ship.parent.mkdir(parents=True, exist_ok=True)
        ship.write_text(f'#!/usr/bin/env bash\necho x > {marker}\n', encoding="utf-8")
        ship.chmod(0o755)
    _write_env_file(file_root / "ci" / "ship" / "ship.sh")
    script = tmp_path / "pr-ship.sh"
    script.write_text(ship_delegator_content(), encoding="utf-8")
    script.chmod(0o755)
    rundir = tmp_path / "run"
    rundir.mkdir()
    res = _sp.run([str(script)], cwd=rundir, capture_output=True, text=True,
                  env=_script_env(AGENT_TOOLS_ROOT=str(env_root)))
    assert res.returncode == 0, res.stderr
    assert env_marker.exists() and not file_marker.exists(), "env var must win over the env file"


def test_delegator_works_in_sanitized_env_without_home(tmp_path):
    # env -i style: no HOME, no XDG_CONFIG_HOME, only an explicit AGENT_TOOLS_ROOT. Under `set -u`
    # a bare $HOME in the env_file expansion would abort with "unbound variable"; ${HOME:-} must
    # keep the explicit root working.
    import subprocess as _sp

    out_marker = tmp_path / "ran.txt"
    root = tmp_path / "agent-tools"
    ship = root / "ci" / "ship" / "ship.sh"
    ship.parent.mkdir(parents=True, exist_ok=True)
    ship.write_text(f'#!/usr/bin/env bash\necho ok > {out_marker}\n', encoding="utf-8")
    ship.chmod(0o755)
    script = tmp_path / "pr-ship.sh"
    script.write_text(ship_delegator_content(), encoding="utf-8")
    script.chmod(0o755)
    rundir = tmp_path / "run"
    rundir.mkdir()
    res = _sp.run(
        [str(script)], cwd=rundir, capture_output=True, text=True,
        env={"PATH": os.environ["PATH"], "AGENT_TOOLS_ROOT": str(root)},  # no HOME, no XDG
    )
    assert res.returncode == 0, res.stderr
    assert out_marker.read_text(encoding="utf-8").strip() == "ok"


def test_delegator_exits_127_with_diagnostic_when_unresolvable(tmp_path):
    # no repo-local ship, no AGENT_TOOLS_ROOT, no env file → a clear diagnostic + exit 127.
    import subprocess as _sp

    script = tmp_path / "pr-ship.sh"
    script.write_text(ship_delegator_content(), encoding="utf-8")
    script.chmod(0o755)
    rundir = tmp_path / "run"
    rundir.mkdir()
    res = _sp.run([str(script)], cwd=rundir, capture_output=True, text=True, env=_script_env())
    assert res.returncode == 127
    assert "AGENT_TOOLS_ROOT=<unset>" in res.stderr
    assert "rig apply" in res.stderr


def test_delegator_prefers_repo_local_ship_over_canonical(tmp_path):
    # the PRIMARY branch: a repo-local ci/ship/ship.sh must win over the canonical (this is how
    # agent-tools self-hosts). Prove it execs the repo-local, NOT the canonical, even with
    # AGENT_TOOLS_ROOT set AND the env file present.
    import subprocess as _sp

    canonical_marker = tmp_path / "canon_ran.txt"
    local_marker = tmp_path / "local_ran.txt"
    canonical = tmp_path / "agent-tools" / "ci" / "ship" / "ship.sh"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(f'#!/usr/bin/env bash\necho x > {canonical_marker}\n', encoding="utf-8")
    canonical.chmod(0o755)
    _write_env_file(canonical)
    # a git repo that CARRIES its own ci/ship/ship.sh
    repo = _git_repo(tmp_path / "repo")
    repo_ship = repo / "ci" / "ship" / "ship.sh"
    repo_ship.parent.mkdir(parents=True, exist_ok=True)
    repo_ship.write_text(f'#!/usr/bin/env bash\necho local > {local_marker}\n', encoding="utf-8")
    repo_ship.chmod(0o755)
    script = repo / "pr-ship.sh"
    script.write_text(ship_delegator_content(), encoding="utf-8")
    script.chmod(0o755)
    # run from INSIDE the repo → git rev-parse finds the toplevel → repo-local ship wins
    res = _sp.run([str(script)], cwd=repo, capture_output=True, text=True,
                  env=_script_env(AGENT_TOOLS_ROOT=str(tmp_path / "agent-tools")))
    assert res.returncode == 0, res.stderr
    assert local_marker.exists() and not canonical_marker.exists(), "repo-local ship must shadow canonical"


def test_delegator_handles_canonical_path_with_spaces(tmp_path):
    # the common real-world case: agent_tools_source under a dir with a space. shlex.quote in the
    # env file handles it; prove the round-trip (path → env file → source → exec) runs the canonical.
    out_marker = tmp_path / "ran.txt"
    canonical = tmp_path / "agent tools dir" / "ci" / "ship" / "ship.sh"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(f'#!/usr/bin/env bash\necho ok > {out_marker}\n', encoding="utf-8")
    canonical.chmod(0o755)
    _write_env_file(canonical)
    script = tmp_path / "pr-ship.sh"
    script.write_text(ship_delegator_content(), encoding="utf-8")
    script.chmod(0o755)
    import subprocess as _sp

    rundir = tmp_path / "run"
    rundir.mkdir()
    res = _sp.run([str(script)], cwd=rundir, capture_output=True, text=True, env=_script_env())
    assert res.returncode == 0, res.stderr
    assert out_marker.read_text(encoding="utf-8").strip() == "ok"


# ── the machine env file: written on apply, idempotent, stale-repaired ──────────────
def test_apply_writes_env_file_idempotently(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    p = ship_env_file_path()
    assert p.is_file()
    assert p.read_text(encoding="utf-8") == ship_env_file_content(canonical)
    # second apply: no rewrite (mtime unchanged), action reads as a no-op
    m = p.stat().st_mtime_ns
    res2 = _apply(repo, canonical)
    assert res2.status == "skipped"
    assert p.stat().st_mtime_ns == m
    # a STALE rig-owned env file (header present, wrong root) is repaired in place, and the
    # action reports a change (the headerless user-file branches are covered separately below)
    from riglib.actions.ship_delegator import _SHIP_ENV_HEADER

    p.write_text(f"{_SHIP_ENV_HEADER}: machine-level pointer.\nAGENT_TOOLS_ROOT='/stale/root'\n",
                 encoding="utf-8")
    res3 = _apply(repo, canonical)
    assert res3.status == "updated"
    assert p.read_text(encoding="utf-8") == ship_env_file_content(canonical)
    assert list(p.parent.glob("env.rig-bak-*")) == []  # rig-owned repair keeps no backup


def test_user_env_file_without_rig_header_is_backed_up(tmp_path):
    # a pre-existing agent-tools/env rig did NOT write (no ownership header) is USER content:
    # replace it only WITH a backup, never a silent clobber. A rig-owned stale file (header
    # present) is rewritten in place with no backup (covered above).
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    p = ship_env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# my hand-rolled env\nAGENT_TOOLS_ROOT=/my/own\nEXTRA=1\n", encoding="utf-8")
    _apply(repo, canonical)
    assert p.read_text(encoding="utf-8") == ship_env_file_content(canonical)
    backups = list(p.parent.glob("env.rig-bak-*"))
    assert len(backups) == 1
    assert "hand-rolled" in backups[0].read_text(encoding="utf-8")


def test_user_env_file_honors_skip_and_overwrite(tmp_path):
    # a USER-owned env file (no rig header) follows on_conflict: `skip` leaves it untouched
    # (and the action is still a success), `overwrite` replaces it with NO backup.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    p = ship_env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    user_content = "# my hand-rolled env\nAGENT_TOOLS_ROOT=/my/own\n"
    p.write_text(user_content, encoding="utf-8")
    res = _apply(repo, canonical, on_conflict="skip")
    # the delegator itself WAS written, but the unresolved user env file must not vanish into a
    # "created" success — status keeps flagging env drift, so the action reports "skipped"
    # (aligned with the delegator-already-ok branch), with the write named in the detail.
    assert res.status == "skipped"
    assert "left user env file" in res.detail  # the unreconciled skip is surfaced, never silent
    assert _delegator_path(repo).is_file()  # the delegator write itself still happened
    assert p.read_text(encoding="utf-8") == user_content  # left as-is
    assert list(p.parent.glob("env.rig-bak-*")) == []
    # and the skip is NOT clean: status keeps flagging the stale env file
    report = detect(_plan_with_action(repo, canonical))
    env_items = [i for i in report.items if i.category == "ship_env" and i.item == "env-file"]
    assert env_items and env_items[0].direction == "modified"
    res = _apply(repo, canonical, on_conflict="overwrite")
    assert res.status != "error"
    assert p.read_text(encoding="utf-8") == ship_env_file_content(canonical)
    assert list(p.parent.glob("env.rig-bak-*")) == []  # overwrite keeps no backup


def test_drift_reports_directory_at_env_path_as_modified(tmp_path):
    # a DIRECTORY at the env-file path makes apply ERROR, so drift must say `modified` with the
    # real failure — not a misleading "missing (apply rewrites it)" (status/apply parity).
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    p = ship_env_file_path()
    p.unlink()
    p.mkdir()
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_env" and i.item == "env-file"]
    assert items and items[0].direction == "modified"
    assert "non-file" in items[0].detail
    res = _apply(repo, canonical)
    assert res.status == "error"


def test_drift_reports_file_at_env_parent_as_modified(tmp_path):
    # a FILE where the PARENT dir should be (~/.config/agent-tools is a file) blocks apply's
    # mkdir(parents=True) — apply ERRORS, it does NOT "rewrite" anything. drift must classify
    # this `modified` naming the blocker, not a misleading "missing (apply rewrites it)"
    # (status/apply parity — the codex P2 on PR #112).
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    p = ship_env_file_path()
    p.parent.parent.mkdir(parents=True, exist_ok=True)
    p.parent.write_text("not a directory\n", encoding="utf-8")  # agent-tools/ as a file
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_env" and i.item == "env-file"]
    assert items and items[0].direction == "modified"
    assert "non-directory" in items[0].detail
    res = _apply(repo, canonical)
    assert res.status == "error"  # parity: apply really does error, not rewrite


def test_non_git_status_env_check_still_runs_for_dropped_action(tmp_path):
    # `rig status` outside git drops repo-scoped actions before drift detection — but apply does
    # NOT drop them and still reconciles the MACHINE env file there. The dropped ship_delegator
    # action's GLOBAL ship_env check must therefore still run (the codex P2 on PR #112): a
    # missing/stale env file must surface even from a non-git cwd.
    nongit = tmp_path / "plain-dir"
    nongit.mkdir()
    canonical = _canonical_ship(tmp_path)
    items = _dropped_action_env_items(_action(nongit, canonical))
    assert items and items[0].direction == "missing"
    # stale content is flagged too
    p = _write_env_file(canonical)
    p.write_text("AGENT_TOOLS_ROOT='/somewhere/else'\n", encoding="utf-8")
    items2 = _dropped_action_env_items(_action(nongit, canonical))
    assert items2 and items2[0].direction == "modified"
    # an up-to-date env file is clean
    _write_env_file(canonical)
    assert not _dropped_action_env_items(_action(nongit, canonical), item="")
    # a malformed action (no canonical_ship) fails CLOSED — apply errors on it, so status must
    # flag it too, under the GLOBAL ship_env category (renderable outside git)
    bad = _action(nongit, canonical)
    bad.options = {"canonical_ship": ""}
    bad_items = _dropped_action_env_items(bad, item="")
    assert bad_items and bad_items[0].direction == "modified"
    assert "malformed plan" in bad_items[0].detail


def test_dangling_symlink_at_env_path_is_refused_not_replaced(tmp_path):
    # a DANGLING symlink at the env path is a non-file: `exists()` is False (it follows the
    # link), so a naive check would classify it "absent" and clobber it outside the conflict
    # policy. Both apply (refuses, errors) and drift (`modified`, never `missing`) must use
    # lexists and agree.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    p = ship_env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.symlink_to(tmp_path / "nowhere")
    res = _apply(repo, canonical)
    assert res.status == "error"
    assert "non-file" in res.detail or "env file" in res.detail
    assert p.is_symlink()  # NOT silently replaced
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_env" and i.item == "env-file"]
    assert items and items[0].direction == "modified"


def test_symlink_to_regular_file_at_env_path_is_refused(tmp_path):
    # even a symlink RESOLVING to a regular file is refused: the rig-owned rewrite goes through
    # os.replace, which would swap the SYMLINK for a real file — silently breaking a centrally
    # managed (dotfiles-repo) symlink instead of updating its target. apply refuses; drift agrees.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    real = tmp_path / "real-env"
    real.write_text("AGENT_TOOLS_ROOT='/somewhere'\n", encoding="utf-8")
    p = ship_env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.symlink_to(real)
    res = _apply(repo, canonical)
    assert res.status == "error"
    assert p.is_symlink()  # the symlink survives
    assert real.read_text(encoding="utf-8") == "AGENT_TOOLS_ROOT='/somewhere'\n"  # target untouched
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_env" and i.item == "env-file"]
    assert items and items[0].direction == "modified"


def test_rig_owned_stale_env_file_rewritten_in_place_without_backup(tmp_path):
    # a differing env file that CARRIES the rig ownership header is rig-OWNED: rewritten in
    # place, no *.rig-bak-* backup, regardless of on_conflict (the authoritative value is
    # agent_tools_source in config) — unlike a user file (no header), which gets the conflict
    # policy. The distinct branch must be pinned directly, not only via the user-file tests.
    from riglib.actions.ship_delegator import _SHIP_ENV_HEADER

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    p = ship_env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{_SHIP_ENV_HEADER}: machine-level pointer.\nAGENT_TOOLS_ROOT='/stale/root'\n",
                 encoding="utf-8")
    res = _apply(repo, canonical, on_conflict="skip")  # even `skip` rewrites a rig-owned file
    assert res.status in ("created", "updated")
    assert p.read_text(encoding="utf-8") == ship_env_file_content(canonical)
    assert list(p.parent.glob("env.rig-bak-*")) == []


def test_selfhosting_repo_skips_env_file_drift(tmp_path):
    # a repo carrying its own ci/ship/ship.sh never reads the env file (repo-local wins), and
    # apply degrades an unwritable env path to non-fatal there — so drift must NOT flag a
    # missing env file for it (status/apply parity: no eternal drift apply never treats as
    # fatal). A plain repo still gets the check.
    selfhost = _git_repo(tmp_path / "selfhost")
    ship = selfhost / "ci" / "ship" / "ship.sh"
    ship.parent.mkdir(parents=True, exist_ok=True)
    ship.write_text("#!/usr/bin/env bash\necho local\n", encoding="utf-8")
    canonical = _canonical_ship(tmp_path)
    assert not ship_env_file_path().exists()
    report = detect(_plan_with_action(selfhost, canonical))
    assert not [i for i in report.items if i.category == "ship_env"]
    plain = _git_repo(tmp_path / "plain")
    report2 = detect(_plan_with_action(plain, canonical))
    env_items = [i for i in report2.items if i.category == "ship_env"]
    assert env_items and env_items[0].direction == "missing"


def test_old_rig_delegator_upgrade_rewrites_in_place_no_backup_clean_tree(tmp_path):
    # the 0.8→0.9 upgrade scenario: EVERY managed repo carries the previous rig-rendered
    # delegator (rig provenance header, baked machine path — different bytes). The first apply
    # after the upgrade must rewrite it IN PLACE, with no .rig-bak-* sibling and a clean
    # `git status` — a backup would dirty the worktree in every repo at once and `gh ship`
    # (which refuses a dirty tree) would be broken by its own provisioning.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    old = (
        "#!/usr/bin/env bash\n"
        "# Provisioned by rig (ship_delegator). The global `gh ship` alias runs\n"
        "# <repo>/.claude/scripts/pr-ship.sh.\n"
        "set -euo pipefail\n"
        "_rig_default=/some/old/machine/path/agent-tools\n"
        'AGENT_TOOLS_ROOT="${AGENT_TOOLS_ROOT:-$_rig_default}"\n'
        'exec "$AGENT_TOOLS_ROOT/ci/ship/ship.sh" "$@"\n'
    )
    deleg.write_text(old, encoding="utf-8")
    deleg.chmod(0o755)
    res = _apply(repo, canonical)  # default on_conflict=backup — must NOT back up rig's own file
    assert res.status in ("created", "updated")
    assert deleg.read_text(encoding="utf-8") == ship_delegator_content()
    assert list(deleg.parent.glob("*.rig-bak-*")) == []
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", ".claude/scripts/"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    assert status.stdout.strip() == "", f"upgrade dirtied the worktree: {status.stdout!r}"


def test_old_rig_delegator_is_rewritten_even_under_skip_policy(tmp_path):
    # on_conflict protects USER content; a rig-headered delegator is rig's own prior output.
    # Even `skip` must not leave the stale generation in place (the upgrade would silently
    # never land in a defaults.on_conflict: skip setup).
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    deleg.write_text(
        "#!/usr/bin/env bash\n# Provisioned by rig (ship_delegator). old generation\nexit 1\n",
        encoding="utf-8",
    )
    res = _apply(repo, canonical, on_conflict="skip")
    assert res.status in ("created", "updated")
    assert deleg.read_text(encoding="utf-8") == ship_delegator_content()
    assert list(deleg.parent.glob("*.rig-bak-*")) == []


def test_non_utf8_env_file_is_reported_not_crashed(tmp_path):
    # UnicodeDecodeError is a ValueError, not an OSError: one non-UTF-8 byte in the env file
    # must yield a readable apply error + a drift item, never a traceback out of detect().
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    p = ship_env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"AGENT_TOOLS_ROOT='/caf\xe9'\n")  # latin-1 byte, invalid UTF-8
    res = _apply(repo, canonical)
    assert res.status == "error"
    assert "could not read env file" in res.detail
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_env" and i.item == "env-file"]
    assert items and items[0].direction == "modified"
    assert "unreadable" in items[0].detail


def test_exclude_block_covers_rig_bak_siblings(tmp_path):
    # a USER-owned (headerless) delegator DOES get displaced to a .rig-bak-* sibling under the
    # default policy — the exclude block must cover that sibling too, or the backup itself
    # dirties the worktree and breaks `gh ship`.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    deleg.write_text("#!/usr/bin/env bash\necho my hand-rolled shim\n", encoding="utf-8")
    res = _apply(repo, canonical)
    assert res.status == "backed_up" and res.backup is not None
    assert res.backup.name.startswith("pr-ship.sh.rig-bak-")
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", ".claude/scripts/"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    assert status.stdout.strip() == "", f"the .rig-bak backup dirties the worktree: {status.stdout!r}"


def test_delegator_refuses_symlinked_env_file(tmp_path):
    # apply/drift REFUSE a symlink at the env path; the delegator must draw the same line at
    # runtime — never source (execute) a symlink target rig itself refuses to manage.
    import shlex
    import subprocess as _sp

    out_marker = tmp_path / "ran.txt"
    canonical = _canonical_ship(tmp_path)
    real_env = tmp_path / "real-env"
    real_env.write_text(
        f"AGENT_TOOLS_ROOT={shlex.quote(str(tmp_path / 'agent-tools'))}\n"
        f"echo pwned > {shlex.quote(str(out_marker))}\n",
        encoding="utf-8",
    )
    env_path = ship_env_file_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.symlink_to(real_env)
    script = tmp_path / "pr-ship.sh"
    script.write_text(ship_delegator_content(), encoding="utf-8")
    script.chmod(0o755)
    rundir = tmp_path / "run"  # non-git dir → the repo-local branch is skipped
    rundir.mkdir()
    res = _sp.run([str(script)], cwd=rundir, capture_output=True, text=True, env=_script_env())
    assert res.returncode == 127, (res.returncode, res.stdout, res.stderr)
    assert not out_marker.exists(), "the symlinked env file was sourced"


def test_corrupt_git_dir_with_local_ship_is_not_selfhosting(tmp_path):
    # `repo_self_hosts_ship` must run the SAME probe as the delegator's runtime
    # (`git rev-parse --show-toplevel`), not approximate with `.git`-exists: a corrupt/fake
    # .git passes the filesystem check but fails the real probe — the runtime would skip the
    # repo-local branch and need the env file, so apply must write it.
    from riglib.actions.ship_delegator import repo_self_hosts_ship

    broken = tmp_path / "broken"
    (broken / "ci" / "ship").mkdir(parents=True)
    (broken / "ci" / "ship" / "ship.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (broken / ".git").write_text("not a gitdir pointer\n", encoding="utf-8")  # fake/corrupt
    assert not repo_self_hosts_ship(broken)
    real = _git_repo(tmp_path / "real")
    (real / "ci" / "ship").mkdir(parents=True)
    (real / "ci" / "ship" / "ship.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    assert repo_self_hosts_ship(real)


def test_non_git_dir_with_local_ship_still_gets_the_env_file(tmp_path):
    # a PLAIN (non-git) dir carrying a stray ci/ship/ship.sh is NOT self-hosting: the delegator's
    # repo-local branch resolves via `git rev-parse`, which fails there, so at runtime the env
    # file IS needed. apply must write it and drift must check it — the self-hosting skip keys on
    # the RUNTIME contract (git repo + script), not the filesystem alone (a pre-fix regression:
    # the old baked default covered this dir; the env file must now do that job).
    plain = tmp_path / "plain"
    (plain / "ci" / "ship").mkdir(parents=True)
    (plain / "ci" / "ship" / "ship.sh").write_text("#!/usr/bin/env bash\necho local\n",
                                                   encoding="utf-8")
    canonical = _canonical_ship(tmp_path)
    report = detect(_plan_with_action(plain, canonical))
    env_items = [i for i in report.items if i.category == "ship_env"]
    assert env_items and env_items[0].direction == "missing"
    res = _apply(plain, canonical)
    assert res.status != "error"
    p = ship_env_file_path()
    assert p.is_file() and p.read_text(encoding="utf-8") == ship_env_file_content(canonical)
