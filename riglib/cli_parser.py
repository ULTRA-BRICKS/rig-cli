"""argparse builders for the config/profile/env/stats/daily/report/usage subcommands.

Context: `build_parser` in `cli.py` composes these; each `add_*_parser` function owns one subcommand's
flags. The `lint` parser lives in `lint_parser.py` next to its dispatch.
"""
from __future__ import annotations

import argparse


def add_config_parser(sub: "argparse._SubParsersAction") -> None:
    """`rig config get|set <dot.path>` — read/edit ONE nested key, then reconcile.

    The recommended way to change a single setting without hand-editing YAML. `get` reads a
    nested key by dotted path from the single target file (./rig.yaml, or the global config with
    --global) — NOT the cascade; `set` coerces the value conservatively, writes it, and then
    runs the SAME apply engine `rig apply` uses so the change takes effect immediately (with a
    full rollback if the write or the catalog-backed plan build fails). --global targets
    ~/.config/rig/config.yaml; --json (get) emits the JSON value; --no-apply (set) writes the
    key and prints the plan only. The dot-path engine lives in riglib.config.
    """
    cp = sub.add_parser("config", help="get/set one config key; set previews by default")
    cp.set_defaults(_config_parser=cp)
    csub = cp.add_subparsers(dest="config_command", metavar="<get|set>")

    def _add_config_target_args(parser: argparse.ArgumentParser) -> None:
        # -C/--cwd and --global are identical on get and set; declare them once so the two
        # never drift apart (same help text, same dest).
        parser.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")
        parser.add_argument(
            "--global", dest="is_global", action="store_true",
            help="target the global config (~/.config/rig/config.yaml) instead of ./rig.yaml",
        )

    cg = csub.add_parser("get", help="read a nested config key (e.g. harness.auto_mode)")
    cg.add_argument("path", help="dot path into the config tree (e.g. ci.items.secret-scan.tier)")
    _add_config_target_args(cg)
    cg.add_argument("--json", action="store_true", help="emit the value as JSON")

    cs = csub.add_parser("set", help="preview a nested config change; --commit writes + reconciles")
    cs.add_argument("path", help="dot path into the config tree (e.g. harness.auto_mode)")
    cs.add_argument("value", help="value to set (coerced: true/false/int/float/null, else string)")
    _add_config_target_args(cs)
    set_mode = cs.add_mutually_exclusive_group()
    set_mode.add_argument("--commit", action="store_true",
                          help="write the validated config change and execute the resulting reconcile")
    set_mode.add_argument("--no-apply", action="store_true",
                          help="write the validated config change but do not reconcile (legacy write-only mode)")
    cs.add_argument(
        "--plan", action="store_true",
        help="list every planned action (default: a per-carrier summary for a large plan)",
    )


def profile_file_ids() -> tuple[str, ...]:
    """The allow-listed file ids (``rig profile sync --only``)."""
    from .profile_files import file_ids

    return file_ids()


def add_profile_parser(sub: "argparse._SubParsersAction") -> None:
    """`rig profile init|sync|status` — sync the GLOBAL config and portable files through a private git repo.

    The engine (three-way compare per file, clone/seed, push/pull, visibility + secret guards) lives
    in :mod:`riglib.profile`, the allow-list and transforms in :mod:`riglib.profile_files`; this
    only declares the surface.
    """
    pp = sub.add_parser(
        "profile",
        help="sync config.yaml + tmux/omp/sysmon config through a PRIVATE git repo, both ways (init/sync/status)",
        description="Carry the GLOBAL rig config and a declared allow-list of portable files "
        "(~/.tmux.conf user region, omp config.yml, omp mcp.json with secrets masked, sysmon.json) "
        "between machines through a private git repo (<forge>/<login>/.rig-profile). `init` clones "
        "it (or seeds an empty one), `sync` moves changes both ways per file and refuses a file "
        "when both sides changed, `status` says which side changed without writing. Secrets never "
        "travel: env.vars, tokens and credential databases stay on the machine, and a "
        "secret-looking value in a payload refuses the push. Machine-specific values stay per "
        "machine (machine/<host>.yaml in the repo).",
    )
    pp.set_defaults(_profile_parser=pp)
    psub = pp.add_subparsers(dest="profile_command", metavar="<init|sync|status>")

    pi = psub.add_parser(
        "init",
        help="clone the profile repo (or seed an empty one with the local config) and record it",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="exit codes:\n  0 synced\n  1 git/network failure or a refused push (public repo)\n"
        "  2 no URL / invalid config / no git identity\n  3 local and remote differ — pick a side "
        "with `rig profile sync --take local|remote`",
    )
    pi.add_argument(
        "url", nargs="?",
        help="profile repo URL (default: profile.repo from the global config, else "
        "https://<host>/<fj login>/.rig-profile when `fj whoami` knows the login)",
    )
    pi.add_argument("--host", help="forge host for the derived default URL (default: fj's host, "
                    "$FJ_FALLBACK_HOST, else git.hyperide.ai)")
    pi.add_argument(
        "--auto-sync", dest="auto_sync", action=argparse.BooleanOptionalAction, default=None,
        help="--no-auto-sync records profile.auto_sync: false (by default `rig apply commit` syncs first)",
    )

    ps = psub.add_parser(
        "sync",
        help="two-way sync: push a local change, pull a remote change, refuse when both changed",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="exit codes:\n  0 in sync / pushed / pulled\n  1 git/network failure or a refused push\n"
        "  2 not initialized / invalid config / no git identity\n  3 conflict (nothing written)",
    )
    ps.add_argument(
        "--take", choices=("local", "remote"),
        help="resolve a conflict: local = push this machine's file; remote = adopt the repo's "
        "(the local file is backed up first)",
    )
    ps.add_argument(
        "--only", action="append", metavar="ID", choices=profile_file_ids(),
        help="limit the run to one file (repeatable): " + ", ".join(profile_file_ids()),
    )

    pst = psub.add_parser("status", help="show which side changed since the last sync (writes nothing)")
    pst.add_argument("--fetch", action="store_true",
                     help="fetch the remote first (default: compare against the last-fetched state)")


def add_env_parser(sub: "argparse._SubParsersAction") -> None:
    """`rig env set|list|del` — manage global ``env.vars`` keys.

    Thin wrapper over the ``config set --global env.vars.<KEY>`` mutate path. Always
    targets ``~/.config/rig/config.yaml``. Values stay strings (no scalar coercion —
    ``true`` must remain the string ``true``). ``set``/``del`` write + reconcile by
    default (unlike ``config set``'s preview default) because an env var that never
    reaches ``rig.env.sh`` is useless. ``list`` masks values.
    """
    ep = sub.add_parser(
        "env",
        help="set/list/del global env.vars keys (values masked; files are plaintext)",
    )
    ep.set_defaults(_env_parser=ep)
    esub = ep.add_subparsers(dest="env_command", metavar="<list|set|del>")

    def _add_env_cwd(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")

    def _add_env_mutate_flags(parser: argparse.ArgumentParser) -> None:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--preview",
            action="store_true",
            help="show the plan, write nothing",
        )
        mode.add_argument(
            "--commit",
            action="store_true",
            help="write the change and reconcile (default)",
        )
        mode.add_argument(
            "--no-apply",
            action="store_true",
            help="write the change but do not reconcile",
        )
        parser.add_argument(
            "--plan",
            action="store_true",
            help="list every planned action (default: a per-carrier summary for a large plan)",
        )

    el = esub.add_parser(
        "list", help="list env.vars keys (values masked unless --show)"
    )
    _add_env_cwd(el)
    el.add_argument(
        "--json", action="store_true", help="emit a JSON object of key → (masked) value"
    )
    el.add_argument(
        "--show",
        action="store_true",
        help="print full values (config.yaml and rig.env.sh are plaintext)",
    )

    es = esub.add_parser(
        "set",
        help="set env.vars.KEY (string; writes + reconciles; omit VALUE to read stdin)",
    )
    es.add_argument("key", help="POSIX identifier (e.g. BRAVE_API_KEY)")
    es.add_argument(
        "value",
        nargs="?",
        help="value to store as a string; omit to read stdin (keeps it out of argv/history)",
    )
    _add_env_cwd(es)
    es.add_argument(
        "--stdin",
        action="store_true",
        help="read the value from stdin (no echo on a TTY)",
    )
    _add_env_mutate_flags(es)

    ed = esub.add_parser("del", help="delete env.vars.KEY, then reconcile")
    ed.add_argument("key", help="POSIX identifier to remove")
    _add_env_cwd(ed)
    _add_env_mutate_flags(ed)




def add_stats_parser(sub: "argparse._SubParsersAction") -> None:
    """`rig stats <action>` — analytics over agent-harness session logs.

    Nested sub-actions (currently just `show`) so the surface has room to grow
    (`rig stats export`, `rig stats compare`, …) without reshaping the top-level CLI.
    """
    sp = sub.add_parser("stats", help="tool-adoption and escape-hatch analytics over agent-harness session logs")
    # stash the subparser so `cmd_stats` can print ITS help on a bare `rig stats` (one source
    # of truth for the usage text — no hand-retyped duplicate).
    sp.set_defaults(_stats_parser=sp)
    sact = sp.add_subparsers(dest="stats_action", metavar="<action>")

    show = sact.add_parser("show", help="report tool-adoption and escape-hatch usage, by category/repo/harness/hook")
    show.add_argument(
        "--harness", action="append", metavar="NAME",
        # kept generic (not an exhaustive list) so it can't drift as parsers are added; the
        # authoritative set is data-driven from the registered sources at run time.
        help="limit to a harness (repeatable), e.g. claude-code / codex / gemini / omp / opencode",
    )
    show.add_argument(
        "--repo", action="append", metavar="PATH",
        help="limit to a repo/cwd absolute path (repeatable)",
    )
    show.add_argument("--since", metavar="YYYY-MM-DD", help="only invocations on/after this date")
    show.add_argument("--until", metavar="YYYY-MM-DD", help="only invocations on/before this date")
    show.add_argument(
        "--format", choices=("json", "tui", "web"), default="tui",
        help="output mode (default: tui — rich terminal UI)",
    )
    show.add_argument("--web-port", type=int, default=0, help="port for --format web (default: auto)")
    show.add_argument(
        "--baseline", action="store_true",
        help="narrow the report to the baseline-vs-ours buckets (drop external/other noise)",
    )
    # hidden seam: tests/scripts can point the whole pipeline at a sandbox HOME.
    show.add_argument("--home", help=argparse.SUPPRESS)


def add_daily_parser(sub: "argparse._SubParsersAction") -> None:
    """`rig daily` — a categorized "what shipped" report over merged PRs, ready to paste
    into a Slack Daily channel. `rig daily install-skill` registers the `daily` agent
    skill (same mechanism as `rig install-skill`, see `riglib/daily/skill.py`)."""
    from .daily.command import add_arguments as _add_daily_report_args

    dp = sub.add_parser(
        "daily",
        help="categorized 'what shipped' report over merged PRs (paste into Slack)",
        description="Report merged PRs since the last run (source of truth: `gh pr "
        "list --state merged`, never a ticket status). Grouped into Security / "
        "Infra-CI / Performance / Product-UX / Other; one plain-language fact per "
        "line, ticket/PR reference last in parentheses.",
    )
    dp.add_argument(
        "action", nargs="?", choices=("install-skill",), default=None,
        help="install-skill: register the `daily` agent skill with harnesses "
        "(default with no action: generate the report)",
    )
    _add_daily_report_args(dp)


def add_report_parser(sub: "argparse._SubParsersAction") -> None:
    """`rig report` — HyperIDE /result-report gather+HTML as a rig command.

    Writes a self-contained HTML completion report from git log/diff since the
    merge-base with main. Does not publish to GitHub Pages.
    """
    from .report.command import add_arguments as _add_report_command_args

    rp = sub.add_parser(
        "report",
        help="HTML completion report from git history since merge-base (HyperIDE /result-report)",
        description="Gather commits and file diffs since the merge-base with main "
        "(HyperIDE result-report.sh gather) and write a self-contained HTML file "
        "you can open. Does not publish to GitHub Pages and does not need "
        "components.js.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit codes:\n"
            "  0    wrote the HTML report\n"
            "  2    invalid task id (empty, `.`/`..`, or path-shaped)\n"
            "  6    not a git repository\n"
            "  127  git is not installed"
        ),
    )
    _add_report_command_args(rp)


def add_usage_parser(sub: "argparse._SubParsersAction") -> None:
    """`rig usage` — real Claude token/cost usage across the default account plus every
    claude-rotate account on this machine (`riglib/usage.py`). Bare = current week + current
    month. `--json` is the stable, versioned contract a scheduled Telegram push (built
    separately, in tg-cli) invokes unattended."""
    from .usage import add_arguments as _add_usage_report_args

    up = sub.add_parser(
        "usage",
        help="Claude token/cost usage across accounts (hypothetical cost at list price)",
        description="Aggregate real per-message token usage across ~/.claude/projects "
        "and every ~/.claude-accounts/account-*/projects, by model/account/token-type. "
        "Cost is a HYPOTHETICAL estimate at published API list prices — this is a "
        "Claude.ai subscription, not pay-per-token billing.",
    )
    _add_usage_report_args(up)
