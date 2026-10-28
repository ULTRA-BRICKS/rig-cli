"""Internal-dev, top-level scalar leaves, tracked-files and harness-wrappers blocks.

Split out of ``config_schema`` to keep each module small; the registry order is assembled there.
"""

from __future__ import annotations

from .harness_kinds import KINDS as _WRAPPER_KINDS
from .wrapper_provision import DEFAULT_DIR as _WRAPPER_DEFAULT_DIR
from .config_schema_model import Block, Leaf


_INTERNAL_DEV_BLOCK = Block(
    doc=(
        "internal development of the rig-ecosystem tools themselves (rig, tg-cli, review-cli, …): "
        "a per-repo COMMITTED opt-in that installs a post-commit git hook so a commit touching the "
        "daemon's source gracefully reloads the running daemon (tg-ctl restart) — zero manual restart."
    ),
    leaves={
        "auto_reload_on_commit": Leaf(
            "boolean",
            "install a post-commit hook that reloads the daemon when a commit touches a "
            "daemon-source path (opt-in)",
            default=False,
        ),
        "daemon_source_paths": Leaf(
            "array",
            "repo-relative shell-glob patterns (POSIX `case`, `*` spans `/`) whose change in a "
            "commit triggers the reload",
            items_type="string",
        ),
        "reload_command": Leaf(
            "string",
            "the graceful reload command the hook runs on a match",
            default="tg-ctl restart",
        ),
    },
)


# The top-level shape: the scalar top keys + every block, in the order config.py validates them.
_TOP_LEAVES: dict[str, Leaf] = {
    "version": Leaf("integer", "schema version (only 1 supported)", default=1, minimum=1),
    "agent_tools_source": Leaf("string", "the agent-tools checkout to apply FROM (default: auto-detect)"),
    # The STACK PRESET (distinct from the build-toolchain detect.Environment.stack). Shape is
    # `l1/lang[/framework]` — l1 is a closed six-enum (mobile/frontend/backend/desktop/embedded/
    # system), lang required, framework optional; lang/framework are OPEN vocabulary. Selects the
    # by-stack skills the repo inherits. Global = machine default (optional); per-repo = mandatory
    # by policy (soft-required — a missing value warns, a malformed value fails). The enum is NOT
    # pinned in JSON-schema because lang/framework are open; the six-enum spine is enforced by the
    # Python validator (config.validate).
    "stack": Leaf(
        "string",
        "the repo's stack preset `l1/lang[/framework]` (e.g. mobile/swift/swiftui, "
        "frontend/ts/react, backend/python). l1 in mobile|frontend|backend|desktop|embedded|"
        "system; lang required; framework optional. Selects by-stack skills. Global default is "
        "optional; a per-repo value is expected (soft-required).",
    ),
}

_TRACKED_FILES_BLOCK = Block(
    doc=(
        "land rig-owned COMMITTED repo files (rig.yaml, task.yaml, .ship-config) once on the "
        "default branch instead of writing them into every worktree: `rig apply` harvests an "
        "identical uncommitted edit found in several worktrees, holds rig's own reconcile of these "
        "files back from the checkout, opens ONE PR and runs the gated ship, then checks the files "
        "out again in worktrees whose copy equals the default branch. GLOBAL config only; block PRESENCE opts in."
    ),
    leaves={
        "enabled": Leaf("boolean", "land tracked-owned files (block presence opts in; false opts out)", default=True),
        "paths": Leaf(
            "array", "repo-relative files rig owns and lands (git pathspec globs: `*` stays in one directory, `**` crosses them)",
            default=["rig.yaml", "task.yaml", ".ship-config"], items_type="string",
        ),
        "land": Leaf(
            "string", "ship: open the PR and run the gated ship in the background; pr: open the PR only",
            enum=("ship", "pr"), default="ship",
        ),
        "harvest_min_worktrees": Leaf(
            "integer", "land an uncommitted edit only when at least this many worktrees carry it identically",
            default=2, minimum=1,
        ),
        "normalize_worktrees": Leaf(
            "boolean", "check the files out again in worktrees whose copy equals the default branch", default=True,
        ),
        "normalize_min_age_minutes": Leaf(
            "integer", "leave a worktree's copy alone while it changed within this many minutes", default=120, minimum=0,
        ),
    },
    nested={
        "ship": Block(
            doc="how the landing PR is shipped (`gh ship`, the gated merge path)",
            leaves={
                "task_code": Leaf("string", "ticket code for ship's review-quorum and acceptance gates (REVIEW_TASK_CODE)"),
                "task_config": Leaf("string", "task-cli config ship passes as --task-config for that ticket"),
                "args": Leaf("array", "extra `gh ship` arguments, verbatim (e.g. --no-version-bump-ok <reason>)",
                             items_type="string"),
                "command": Leaf("string", "the ship command", default="gh ship"),
            },
        ),
        "review": Block(
            doc="the review recorded on the staged landing diff before it is committed (review-cli)",
            leaves={
                "models": Leaf("string", "review-cli seats (`review diff -m`), default: review-cli's own pool"),
            },
        ),
    },
)

_HARNESS_WRAPPERS_BLOCK = Block(
    doc=(
        "opt-in wrapper scripts (`omp`, `claude`, `codex`, `opencode`) written by `rig apply commit` into "
        "`dir`, each running `rig harness exec <kind> -- \"$@\"`: an interactive launch is hosted in an abduco "
        "session under the crash supervisor and registered with tg-ctl; every other invocation execs the "
        "real binary untouched. rig never edits PATH — put `dir` BEFORE the real binaries. GLOBAL-only — "
        "rejected from a repo rig.yaml."
    ),
    leaves={
        "enabled": Leaf("boolean", "write the wrappers (opt-in)", default=False),
        "dir": Leaf(
            "string",
            "directory the wrapper scripts land in (absolute, `~` or `$VAR`); put it before the real binaries on PATH",
            default=_WRAPPER_DEFAULT_DIR,
        ),
        "kinds": Leaf(
            "array", "harnesses to write a wrapper for (a non-empty subset)",
            default=list(_WRAPPER_KINDS), items_type="string", items_enum=_WRAPPER_KINDS,
        ),
        "signal_scope": Leaf(
            "array",
            "Kinds whose launch through rig runs inside a Linux Landlock signal domain: the agent can signal "
            "only its own process tree. Linux only. Applies to every rig launch path, independent of `enabled`. "
            "`[]` turns it off; `RIG_SIGNAL_SCOPE=0` skips it for one launch.",
            default=["omp"], items_type="string", items_enum=_WRAPPER_KINDS,
        ),
        "real_bin": Leaf(
            "object",
            "kind -> absolute path of the real binary, for one that is not the first match on PATH "
            "(e.g. the omp fork checkout); recorded in ~/.config/rig/harness-real-bin.json",
            default={},
            additional_properties_type="string",
            property_names_pattern="^(" + "|".join(_WRAPPER_KINDS) + ")$",
        ),
    },
    schema_extra={"x-rig-global-only": True},
)
