"""``rig report`` orchestration -- wired from ``riglib/cli.py`` as ``cmd_report``.

Two distinct capabilities share this subcommand (design writeup: the worker report
that produced this scaffold, 2026-09-07):

  rig report init      Provision a NEW reports repo (``gh repo create`` + the shared
                        template: ``reports/shared/{styles.css,components.js}``,
                        ``_template/report.html``, ``scripts/new-report.sh``) or point
                        this machine's config at an EXISTING one. This is a
                        RECONCILE-shaped target -- the template would be agent-tools
                        catalog content, installed idempotently with backup-on-conflict,
                        drift surfaced by `rig status`, same shape as `ci: all`
                        installing workflow files. NOT YET IMPLEMENTED, and blocked on
                        TWO upstream dependencies, neither landed as of this writing
                        (2026-09-07): (1) the `result-report` skill itself was, at
                        scaffold time, still an UNMERGED agent-tools branch/PR (#440,
                        `skills/universal/result-report/SKILL.md`) -- confirm it has
                        actually merged to agent-tools main before relying on it; (2)
                        even once merged, that skill only *documents* the
                        reports/shared/* + _template/* template's shape (inferred from
                        HyperIDE's private `hyperide.github.io` reference impl) -- no
                        agent-tools catalog item ships the template's actual files yet.
                        Needs a SEPARATE follow-up agent-tools PR for that, same class of
                        gap as the worktree-isolation skill noted in this repo's
                        AGENTS.md ("Known gap (GH-329...)").

  rig report new        Generate + publish a single-task completion report -- wraps the
                        result-report skill's data-gathering (git log/diff since
                        merge-base, `gh pr view`, a ticket-tracker CLI lookup) into a
                        `riglib.report.model.ReportData`, then
                        `riglib.report.render.render_init_script` into the target
                        repo's `reports/<task-id>/index.html`. NOT YET IMPLEMENTED.

  rig report periodic   Generate + publish an ecosystem-wide status report (the
                        burn-chart / roadmap-highlights shape assembled by hand today)
                        over a set of repos and a time window. MUST reuse
                        `riglib.daily.github.fetch_merged_prs` and
                        `riglib.daily.config.load_repos` for its data -- never fork the
                        merged-PR fetch `rig daily` already owns. Does NOT touch
                        `riglib.daily.state` (no watermark advance; this is a read-only,
                        re-runnable report over an explicit `--since`/`--repo` window,
                        same as `rig daily --since ... --dry-run` but rendered as an
                        HTML page instead of Slack text). NOT YET IMPLEMENTED.

All three are one-shot generators, not reconciled config state (aside from `init`'s
template-provisioning half) -- `new` and `periodic` do NOT route through
`plan.build`/`actions.run_plan` (unlike `rig init`/`rig apply`).

Stub bodies below return exit code 2 ("not yet implemented") until the follow-up work
lands; :mod:`riglib.report.model` and :mod:`riglib.report.render` are already real, so
a later implementation has a data contract to build against rather than starting from
nothing.
"""

from __future__ import annotations

import argparse
import sys

# Exit code returned by every not-yet-implemented subcommand below. Distinct from the
# generic argparse usage-error code (also 2) is not needed here -- both are "you can't
# have this yet", and tests pin this constant so a later real implementation is forced
# to change it deliberately rather than by accident.
NOT_IMPLEMENTED_EXIT_CODE = 2


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="report_command", metavar="<init|new|periodic>")

    init_p = sub.add_parser(
        "init",
        help="provision a reports repo from the shared template (not yet implemented)",
        description="Scaffold a new GitHub Pages reports repo (or wire an existing "
        "one into config): reports/shared/{styles.css,components.js}, "
        "_template/report.html, scripts/new-report.sh. Not yet implemented -- the "
        "shared template is not yet an agent-tools catalog item.",
    )
    init_p.add_argument(
        "repo", nargs="?", help="owner/name for the reports repo (e.g. alex-mextner/rig-reports)"
    )
    init_p.add_argument(
        "--public", action="store_true", help="create the repo public (default: ask)"
    )

    new_p = sub.add_parser(
        "new",
        help="generate + publish a single-task completion report (not yet implemented)",
        description="Wrap the result-report skill's data-gathering (git log/diff "
        "since merge-base, `gh pr view`, ticket lookup) into a report page. Not yet "
        "implemented.",
    )
    new_p.add_argument("task_id", help="ticket/task id, e.g. HYP-1234 or rig-cli#123")
    new_p.add_argument(
        "--post",
        action="store_true",
        help="post the report link to the ticket and PR after publishing "
        "(default: print the link only)",
    )

    periodic_p = sub.add_parser(
        "periodic",
        help="generate + publish an ecosystem status report over a set of repos "
        "(not yet implemented)",
        description="Same merged-PR data source as `rig daily` (never a forked "
        "fetch), rendered as an HTML page instead of Slack text. Not yet implemented.",
    )
    periodic_p.add_argument(
        "--since",
        metavar="TIMESTAMP",
        help="report window start (ISO-8601 or relative, e.g. 7d) -- same parsing as "
        "`rig daily --since`",
    )
    periodic_p.add_argument(
        "--repo",
        action="append",
        metavar="OWNER/NAME",
        help="repo to include (repeatable); default: the configured `rig daily` repo list",
    )


def run(args: argparse.Namespace) -> int:
    command = getattr(args, "report_command", None)
    if command == "init":
        return _run_init(args)
    if command == "new":
        return _run_new(args)
    if command == "periodic":
        return _run_periodic(args)
    print("usage: rig report <init|new|periodic> ...", file=sys.stderr)
    return NOT_IMPLEMENTED_EXIT_CODE


def _run_init(args: argparse.Namespace) -> int:
    print(
        "rig report init: not yet implemented -- the shared report template "
        "(reports/shared/*, _template/report.html) is not yet an agent-tools catalog "
        "item. See riglib/report/command.py module docstring.",
        file=sys.stderr,
    )
    return NOT_IMPLEMENTED_EXIT_CODE


def _run_new(args: argparse.Namespace) -> int:
    print(
        "rig report new: not yet implemented. See riglib/report/command.py module "
        "docstring.",
        file=sys.stderr,
    )
    return NOT_IMPLEMENTED_EXIT_CODE


def _run_periodic(args: argparse.Namespace) -> int:
    print(
        "rig report periodic: not yet implemented. See riglib/report/command.py "
        "module docstring.",
        file=sys.stderr,
    )
    return NOT_IMPLEMENTED_EXIT_CODE
