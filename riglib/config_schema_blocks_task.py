"""The ``task`` block and the tg-ctl / tcc-watch / tgctl-reaper daemon blocks.

Split out of ``config_schema`` to keep each module small; the registry order is assembled there.
"""

from __future__ import annotations

from .config_schema_model import Block, Leaf


_TASK_BLOCK = Block(
    doc=(
        "task-cli ticket-tracker routing for THIS repo (CTO decision #4136.4: hyperide -> "
        "Linear/HYP, every other repo -> GitHub Issues by default). task-cli reads this block "
        "directly (tasklib/config.py's rig_task_overlay) with LOWER precedence than a repo's own "
        "native task.yaml, so a repo that already keeps one is unaffected. Flat shorthands "
        "(team/project/repo/attachment_mode) and the nested github:/linear: sections may be used "
        "together in the same block; task-cli itself rejects a genuine collision between the two "
        "forms (e.g. both `team: X` and `linear: {team: Y}` set to different values) — this "
        "schema only checks the KEY SET, not that kind of cross-field conflict. `code_prefix` is "
        "a SEPARATE, unrelated concern folded into the same top-level `task:` block (rig-cli#217): "
        "a repo-unique task-code prefix consumed by ship.sh's review-quorum gate, reconciled by "
        "`rig apply` into `.ship-config` — see the leaf's own doc below. UB-23: `backend: tasks` "
        "selects the native Tasks-app backend (HTTPS REST), configured via the nested `tasks:` "
        "block (`url`/`team`/`attachment_mode`), mirroring `github:`/`linear:` below. UB-25: "
        "`backend: forgejo` selects the native Forgejo backend (self-hosted git, HTTPS REST), "
        "configured via the nested `forgejo:` block (`url`/`repo`/`attachment_mode`); all three "
        "cascade from an ancestor like every other routing key, so an ancestor should set "
        "`repo: auto` and leave an explicit owner/name to each repo's own rig.yaml."
    ),
    # review finding (tg#11652, P2): `_validate_task_enum` returns early on `None` (so
    # `backend`/`attachment_mode`/`linear.attachment_mode` tolerate null) and the flat/nested
    # string guards below are all `is not None` (so `team`/`project`/`repo`/`github.repo`/
    # `linear.team`/`linear.project` tolerate it too). All nine get the same `("string", "null")`
    # treatment as `permissions.kind` above; the three enum leaves (`backend`, `attachment_mode`,
    # `linear.attachment_mode`) also add `None` to their `enum` tuple (same house pattern).
    leaves={
        "code_prefix": Leaf(
            "string",
            "set when this repo's task-cli backend is GitHub Issues (bare #NNN, no ticket-code "
            "convention of its own). `rig apply` writes it into .ship-config's "
            "SHIP_TASK_CODE_PREFIX line, which ship.sh synthesizes into <PREFIX>-NNN from a bare "
            "#NNN in the branch name/PR body — never the bare number itself (review-cli's "
            "review-quorum store is a single global file keyed only by the code string, with no "
            "per-repo scoping, so two repos both deriving bare \"#346\" would collide). Must be "
            "1-40 uppercase letters/digits — ship.sh ignores the whole .ship-config file "
            "otherwise. Unset = bare issue numbers are never picked up as task codes.",
            # Reject a non-uppercase-alnum character ANYWHERE, or 41+ characters — an empty
            # string is deliberately NOT rejected here (it means "unset", same as omitting the
            # key entirely; see _build_ship_task_prefix's `if not prefix` guard in plan.py,
            # which treats "" identically to absent — the schema and the runtime check must
            # agree on that or a user who writes `code_prefix: ""` to mean "off" gets an editor
            # error the actual `rig apply` never raises). `not_pattern` is a single "reject if
            # this matches anywhere" regex (see Leaf's docstring), so the length bound is folded
            # in as an extra alternative rather than a separate maxLength knob (Leaf has none for
            # strings). NOTE: this constraint is emitted into schema/rig.schema.json for editor
            # tooling only — `riglib.config.validate()` does not currently re-derive leaf-level
            # not_pattern/enum constraints from this registry for ANY block (a pre-existing gap,
            # not introduced here). The actual runtime enforcement for `rig apply` is
            # `_build_ship_task_prefix` (plan.py), which re-checks the character-class and
            # length bound (though not "emptiness", which is the unset sentinel, not an error)
            # in Python before ever emitting the write action.
            not_pattern=r"[^A-Z0-9]|^.{41,}$",
        ),
        "backend": Leaf(
            ("string", "null"),
            "the tracker backend",
            enum=("github-issues", "linear", "tasks", "forgejo", None),
        ),
        "team": Leaf(("string", "null"), "flat shorthand for linear.team (e.g. HYP)"),
        "project": Leaf(("string", "null"), "flat shorthand for linear.project"),
        "repo": Leaf(("string", "null"), "flat shorthand for github.repo (owner/name)"),
        "attachment_mode": Leaf(
            ("string", "null"),
            "how a screenshot/file gets attached to a Linear ticket: 'native' uploads it through "
            "Linear's own API as a real attachment (the default); 'link' never uploads a local "
            "file, only registers an already-hosted URL as-is (hyperide's default, HYP-1248 — "
            "raw uploads.linear.app image links 401 for a non-browser viewer)",
            enum=("link", "native", None),
        ),
        # task-cli's own deep shape for these four is intentionally NOT modeled here — this
        # schema only gates the KEY SET reaching task-cli's config cascade; task-cli's own
        # `tasklib.config.validate` is the runtime authority on their contents (enforce/classify/
        # session all have their own nested defaults task-cli already applies underneath).
        # review finding: `projects` is a LIST of project entries in task-cli's own shape
        # (tasklib/projects.py:projects_from_config), NOT a mapping — modeling it as `object`
        # would make `rig status`/`apply` reject every valid `task.projects` value. Deliberately
        # no `items_type` (review finding, 2nd round): task-cli's own `projects_from_config`
        # SKIPS a malformed (non-mapping) entry rather than erroring — the published JSON
        # schema shouldn't be STRICTER than the Python validator it's meant to mirror, so this
        # stays "a list of anything", matching `_validate_task`'s equally shallow list-only check.
        # review finding (tg#11652, P2): `_validate_task` accepts an explicit `null` for all four
        # of these via `is not None` guards (a user clearing `task.projects: null` or
        # `task.enforce: null`) — the schema must model that same union, or editors/schema-based
        # CI reject a config `rig status`/`rig apply` happily accept. `("array", "null")` /
        # `("object", "null")` emit `"type": [..., "null"]` (see `Leaf.type`'s tuple form, already
        # used by `permissions.kind` above).
        "projects": Leaf(
            ("array", "null"), "the cross-project registry (task-cli's own shape, not deeply modeled here)"
        ),
        "enforce": Leaf(("object", "null"), "ticket-discipline gate overrides (task-cli's own shape)"),
        "classify": Leaf(("object", "null"), "inbound-message classifier config (task-cli's own shape)"),
        "session": Leaf(("object", "null"), "session-detection config (task-cli's own shape)"),
    },
    nested={
        # `nullable=True` on both (same tg#11652 finding): `_validate_task` normalizes an
        # explicit `github: null` / `linear: null` to `{}` (a common YAML placeholder shape),
        # tolerating it exactly like the passthrough leaves above — the schema must too.
        "github": Block(
            doc="GitHub Issues coordinates (used when backend: github-issues).",
            leaves={
                "repo": Leaf(
                    ("string", "null"), "owner/name, or 'auto' to detect from the git remote"
                )
            },
            nullable=True,
        ),
        "linear": Block(
            doc="Linear coordinates + write behavior (used when backend: linear).",
            leaves={
                "team": Leaf(("string", "null"), "the Linear team key (e.g. HYP)"),
                "project": Leaf(
                    ("string", "null"), "an optional Linear project id to scope create/list/search to"
                ),
                "attachment_mode": Leaf(
                    ("string", "null"),
                    "same as the flat `attachment_mode` shorthand above, nested form",
                    enum=("link", "native", None),
                ),
            },
            nullable=True,
        ),
        "tasks": Block(
            doc="Tasks-app (native HTTPS REST) coordinates (used when backend: tasks).",
            leaves={
                "url": Leaf(
                    ("string", "null"),
                    "the Tasks-app base URL, e.g. https://tasks-ultrabricks.vercel.app; a bare https origin "
                    "(no userinfo, query, fragment or path — task-cli appends /api/v1 itself)",
                ),
                "team": Leaf(("string", "null"), "the Tasks-app team/workspace key (e.g. UB, HYP)"),
                "attachment_mode": Leaf(
                    ("string", "null"),
                    "how a screenshot/file gets attached to a Tasks-app ticket: 'native' uploads it "
                    "through the Tasks-app API, 'link' registers an already-hosted URL as-is",
                    enum=("link", "native", None),
                ),
            },
            nullable=True,
        ),
        "forgejo": Block(
            doc=(
                "Forgejo (self-hosted git.hyperide.ai) coordinates (used when backend: forgejo, "
                "UB-25). The API base (`<url>/api/v1`) is DERIVED at runtime — `url` itself must "
                "NOT carry the `/api/v1` suffix. Every leaf cascades from an ancestor rig.yaml "
                "(same as task-cli's loader); an ancestor should set `repo: auto` and leave an "
                "explicit owner/name to each repo's own rig.yaml."
            ),
            leaves={
                "url": Leaf(
                    ("string", "null"),
                    "the Forgejo instance base URL, e.g. https://git.hyperide.ai (must NOT end in "
                    "/api/v1 — that is appended at runtime)",
                ),
                "repo": Leaf(
                    ("string", "null"),
                    "owner/name on the Forgejo instance, or 'auto' to detect from the git remote",
                ),
                "attachment_mode": Leaf(
                    ("string", "null"),
                    "how a screenshot/file gets attached to a Forgejo issue: 'native' uploads it "
                    "through the Forgejo API, 'link' registers an already-hosted URL as-is",
                    enum=("link", "native", None),
                ),
            },
            nullable=True,
        ),
    },
)

_TG_CTL_BLOCK = Block(
    doc="the tg-ctl inbound daemon auto-started as a per-machine boot LaunchAgent (macOS).",
    leaves={
        "enabled": Leaf("boolean", "provision the tg-ctl LaunchAgent", default=True),
        "boot": Leaf("boolean", "write + load the boot agent", default=True),
        "codex_usage_hook": Leaf(
            # nullable: `_validate_tg_ctl` accepts an explicit `codex_usage_hook: null` as
            # default-on (matching how `enabled`/`codex_usage_hook` are both checked with
            # `is False`, not truthiness) — the schema must accept the same value the runtime
            # validator does, or an editor/schema-validator would reject a config `rig apply`
            # itself accepts.
            ("boolean", "null"),
            "fold tg-ctl's Codex Stop usage-telemetry hook into rig's own codex hook bridge "
            "in ~/.codex/config.toml instead of tg-ctl writing its own ~/.codex/hooks.json "
            "(tg-cli#308)",
            default=True,
        ),
        "label": Leaf("string", "launchd Label / plist filename stem", default="ai.hyperide.tg-ctl"),
        "bun_path": Leaf("string", "the bun binary"),
        "tg_ctl_path": Leaf("string", "the tg-ctl Bun script", default="~/.files/bin/tg-ctl"),
        "config_dir": Leaf("string", "tg-cli config + launchd logs dir", default="~/.config/tg-cli"),
    },
)

_TCC_WATCH_BLOCK = Block(
    doc="the TCC dialog-watcher daemon (Phase 1: detection-only), a per-machine boot LaunchAgent "
    "(macOS) that tails `log stream`'s com.apple.TCC subsystem and logs every permission-prompt-"
    "shaped event — defense-in-depth behind the eval-tool TCC deny-list (PR #488).",
    leaves={
        "enabled": Leaf("boolean", "provision the tcc-watch LaunchAgent", default=True),
        "label": Leaf("string", "launchd Label / plist filename stem", default="ai.hyperide.tcc-watch"),
        "config_dir": Leaf("string", "watcher script + tcc-watch.sqlite3 store + launchd logs dir", default="~/.config/rig/tcc-watch"),
        "python_bin": Leaf("string", "the python3 binary the daemon runs under"),
    },
)

_TGCTL_REAPER_BLOCK = Block(
    doc="the tgctl-reaper periodic sweep for orphaned tg-ctl/omp test-fixture helpers "
    "(agent-tools#697), a per-machine `StartInterval` LaunchAgent (macOS) that SIGKILLs "
    "provably-orphaned `omp acp` test fixtures every `interval_seconds`.",
    leaves={
        "enabled": Leaf("boolean", "provision the tgctl-reaper LaunchAgent", default=True),
        "interval_seconds": Leaf("integer", "seconds between sweeps", default=900, minimum=1),
        "label": Leaf(
            "string", "launchd Label / plist filename stem",
            default="com.hyperide.tgctl-omp-acp-reaper",
        ),
        "config_dir": Leaf(
            "string", "sweeper script + launchd logs dir", default="~/.config/rig/tgctl-reaper",
        ),
        "python_bin": Leaf("string", "the python3 binary the sweep runs under"),
    },
)
