"""Tmux, env, gitignore, spotlight, fj, bun, git, profile and ship-delegator blocks.

Split out of ``config_schema`` to keep each module small; the registry order is assembled there.
"""

from __future__ import annotations

from .gitconfig import GIT_CONFIG_KEY_JSON_PATTERN
from .config_schema_model import Block, Leaf
from .config_schema_patterns import GITIGNORE_ENTRY_JSON_PATTERN
from .config_schema_patterns import TMUX_PANE_TITLES_FORMAT_UNSAFE_PATTERN


_TMUX_BLOCK = Block(
    doc="rig-managed tmux configuration (persistence across reboots, plugin-init ordering).",
    leaves={
        "enabled": Leaf("boolean", "provision the rig-managed tmux config (opt-in)", default=False),
        "apply": Leaf("string", "apply mechanism", enum=("import", "block"), default="import"),
        "conf_path": Leaf("string", "the user's tmux config rig migrates", default="~/.tmux.conf"),
        "generated_dir": Leaf("string", "where rig writes its files", default="~/.config/rig/tmux"),
    },
    nested={
        "resurrect": Block(
            doc="tmux-resurrect knobs.",
            leaves={
                "processes": Leaf("array", "@resurrect-processes", items_type="string"),
                "capture_pane_contents": Leaf("boolean", "@resurrect-capture-pane-contents", default=True),
            },
        ),
        "continuum": Block(
            doc="tmux-continuum knobs.",
            leaves={
                "restore": Leaf("boolean", "@continuum-restore on", default=True),
                "boot": Leaf("boolean", "no-op (rig owns boot via launchd)", default=True),
                "save_interval": Leaf("integer", "@continuum-save-interval (minutes)", default=15, minimum=1),
            },
        ),
        "moshi": Block(doc="opt-in Moshi status-line tweaks.", leaves={"enabled": Leaf("boolean", "enable Moshi tweaks", default=False)}),
        "cc_restore": Block(doc="per-window Claude Code resume by session id.", leaves={"enabled": Leaf("boolean", "wire cc-save/cc-restore", default=True)}),
        "omp_restore": Block(doc="per-window omp (oh-my-pi) resume by session file.", leaves={"enabled": Leaf("boolean", "wire omp-save/omp-restore", default=True)}),
        "codex_restore": Block(doc="per-window Codex CLI resume by session id.", leaves={"enabled": Leaf("boolean", "wire codex-save/codex-restore", default=True)}),
        "anti_sprawl": Block(
            doc="one canonical session (attach-or-create).",
            leaves={"enabled": Leaf("boolean", "install the attach-or-create entry", default=True), "session": Leaf("string", "the canonical session name", default="main")},
        ),
        "boot": Block(
            doc="a launchd agent that brings tmux up after a reboot.",
            leaves={"enabled": Leaf("boolean", "write + load the boot agent", default=True), "label": Leaf("string", "the launchd agent label", default="ai.hyperide.tmux-boot")},
        ),
        "recovery": Block(
            doc="identity-checked server/socket recovery on the independent autosave cadence (macOS).",
            leaves={"enabled": Leaf("boolean", "recover a previously recorded server; never replace an unknown live process", default=False)},
        ),
        "autosave": Block(
            doc="an INDEPENDENT launchd saver (decoupled from continuum's status-right hook); when on, continuum's own autosave is disabled so there is one authoritative saver.",
            leaves={
                "enabled": Leaf("boolean", "provision the independent tmux-autosave launchd agent (StartInterval = continuum.save_interval minutes)", default=True),
                "label": Leaf("string", "the autosave launchd agent label", default="ai.hyperide.tmux-autosave"),
                "stale_after": Leaf("integer", "minutes: rig doctor/status flags a mature live server whose newest save is older than this", default=45, minimum=1),
            },
        ),
        "login_shell": Block(
            doc="restored panes are login shells (source ~/.zprofile/PATH).",
            leaves={"enabled": Leaf("boolean", "set a login-shell default-command", default=True), "shell": Leaf("string", "login shell path ('' → resolve $SHELL)", default="")},
        ),
        "pane_titles": Block(
            doc="pane-border-status titles (position + format), and separately dropping tmux's default clock+date from status-right.",
            leaves={
                "enabled": Leaf("boolean", "provision pane-border-status (the pane title itself)", default=True),
                "position": Leaf("string", "pane-border-status placement", enum=("top", "bottom"), default="top"),
                "format": Leaf(
                    "string",
                    "pane-border-format value (no date/time token by default; must not contain "
                    "'\"', '\\\\', '$', '#(' (a shell-exec token), or a non-printable character "
                    "other than a plain tab)",
                    default="#{session_name} #{window_index}:#{window_name}#{window_flags}",
                    not_pattern=TMUX_PANE_TITLES_FORMAT_UNSAFE_PATTERN,
                ),
                "clear_status_right": Leaf("boolean", "when `enabled` is on, also clear tmux's default clock+date status-right; a SEPARATE toggle (nested under `enabled`) so status-right can be left alone while keeping the border title", default=True),
            },
        ),
        "focus_events": Block(
            doc="report terminal focus in/out events to programs running inside panes (tmux's `focus-events` option) — needed by editors/tools that react to focus.",
            leaves={"enabled": Leaf("boolean", "tmux's focus-events option (set -g focus-events on/off)", default=True)},
        ),
        "clipboard": Block(
            doc="copy-mode clipboard integration: pipe a copy-mode selection (keyboard `prefix [`, `v`, `Enter`/`y`, or a mouse-drag release) through a resolved system-clipboard command, since tmux's own built-in copy-pipe-and-cancel bindings run with no pipe command by default (tmux paste buffer only, not the system clipboard). See `copy_mode.mode_keys` for the (independent) copy-mode key table.",
            leaves={
                "enabled": Leaf("boolean", "provision the copy-mode clipboard key bindings", default=True),
                "command": Leaf(
                    "string",
                    "explicit clipboard-write command override ('' → auto-detect: macOS pbcopy; "
                    "Linux wl-copy/xclip/xsel); must not contain '\"', '\\\\', '$', '#(' (a "
                    "shell-exec token), or a non-printable character other than a plain tab",
                    default="",
                    not_pattern=TMUX_PANE_TITLES_FORMAT_UNSAFE_PATTERN,
                ),
            },
        ),
        "copy_mode": Block(
            doc="the copy-mode key table (tmux's `mode-keys` option). tmux's own default is `emacs`, "
            "under which `Space` pages the view (not begin-selection) and a bare `v` is unbound, so a "
            "vim-style copy-mode workflow (`prefix [` then `v`/`Space` to select) silently does "
            "nothing. Rendered unconditionally, independent of `clipboard` settings.",
            leaves={"mode_keys": Leaf("string", "tmux's mode-keys option (setw -g mode-keys)", enum=("vi", "emacs"), default="vi")},
        ),
    },
)

_ENV_BLOCK = Block(
    doc=(
        "rig-managed shell environment variables (GLOBAL, machine-wide): a generated file "
        "(`<generated_dir>/rig.env.sh`, one `export KEY=value` per `vars` entry) sourced by ONE "
        "spliced line in `rc_path`. Default `rc_path` is `~/.zshenv`, sourced by EVERY zsh "
        "invocation (login or not, interactive or not) so a var reaches non-interactive shells too."
    ),
    leaves={
        # No `default=` here (deliberately, review): the effective default isn't a fixed
        # boolean — it depends on BLOCK PRESENCE. An ABSENT `env:` block is a no-op; a
        # PRESENT block with `enabled` unset opts in (true); only an explicit `enabled: false`
        # opts out. That three-way behavior can't be represented as one scalar default without
        # misleading whichever reading is wrong — see the prose above and in
        # `docs/config-schema.md#env` for the actual rule.
        "enabled": Leaf("boolean", "provision the rig-managed shell env vars — see the block's own doc for the opt-in rule (block PRESENCE, not this key's default, decides)"),
        "rc_path": Leaf("string", "the shell startup file rig sources the generated env file from", default="~/.zshenv"),
        "generated_dir": Leaf("string", "where rig writes rig.env.sh", default="~/.config/rig/env"),
        "vars": Leaf("object", "exported KEY=value pairs", additional_properties_type="string"),
    },
)

_GITIGNORE_BLOCK = Block(
    doc="a rig-managed block in git's global core.excludesfile (ignores harness worktrees + the Spotlight sentinel machine-wide).",
    leaves={
        "enabled": Leaf("boolean", "provision the managed block", default=True),
        "entries": Leaf(
            "array", "the ignored paths inside the managed block",
            items_type="string", items_pattern=GITIGNORE_ENTRY_JSON_PATTERN,
        ),
        "excludesfile": Leaf("string", "force a specific file instead of honoring core.excludesfile"),
    },
)

_SPOTLIGHT_BLOCK = Block(
    doc="macOS Spotlight-exclude: drop .metadata_never_index into dependency/build dirs + a launchd re-sweep agent.",
    leaves={
        "enabled": Leaf("boolean", "provision the Spotlight-exclude sweep + agent (opt-in, macOS)", default=False),
        "roots": Leaf("array", "dev roots to sweep (default ~/work, ~/xp)", items_type="string"),
        "deny": Leaf("array", "dependency/build dir basenames to exclude (REPLACES the default set)", items_type="string"),
        "extra": Leaf("array", "extra dir basenames to exclude (ADDS to the default set)", items_type="string"),
        "label": Leaf("string", "the launchd agent label", default="ai.hyperide.spotlight-exclude"),
        "max_depth": Leaf("integer", "walk-depth cap below each root", default=8, minimum=1),
    },
)

_FJ_BLOCK = Block(
    doc=(
        "Forgejo access (GLOBAL, per machine): install `fj` behind a thin wrapper that adds `fj ship` "
        "(the same ship gate as `gh ship`), one-click browser login through Forgejo's built-in OAuth "
        "app that git over HTTPS shares (rig's git credential helper hands git fj's token), and "
        "`FJ_FALLBACK_HOST` so `fj` never needs `--host`. "
        "A PRESENT block opts in; a repo rig.yaml may set `host` to log in to a different instance."
    ),
    leaves={
        "enabled": Leaf("boolean", "provision Forgejo access — block PRESENCE opts in; false opts out"),
        "host": Leaf("string", "the Forgejo host (`git.hyperide.ai`) — login target and FJ_FALLBACK_HOST"),
        "install": Leaf("boolean", "manage the `fj` command: the real fj (brew on macOS, pinned release binary in rig's store elsewhere) + the `fj ship` wrapper in bin_dir; false leaves fj entirely to you", default=True),
        "version": Leaf("string", "fj release to install where rig downloads it", default="0.6.0"),
        "bin_dir": Leaf("string", "where the `fj` wrapper goes (must be on PATH, ahead of any other fj)", default="~/.local/bin"),
        "git_credentials": Leaf("boolean", "git over HTTPS to the GLOBAL host uses fj's login: rig's `git-credential-fj` helper in bin_dir, set as credential.https://<host>.helper; false leaves git's credential config to you", default=True),
        "client_id": Leaf("string", "OAuth client ID fj logs in with (default: Forgejo's built-in `tea` app)"),
    },
)


_BUN_BLOCK = Block(
    doc=(
        "GLOBAL bunfig: splice [install] linker=isolated and globalStore=true. While on, "
        "`rig worktree create` installs a bun repo's deps (the repo's own bunfig decides the layout) "
        "and `rig status` flags broken worktree installs."
    ),
    leaves={
        "enabled": Leaf(
            "boolean",
            "splice isolated linker + globalStore into ~/.bunfig.toml (opt-in); also lets `rig worktree create` "
            "run `bun install` — and so the repo's own install scripts — in new worktrees (see worktree_install)",
            default=False,
        ),
        "worktree_install": Leaf(
            "boolean",
            "`rig worktree create` runs `bun install --frozen-lockfile` in a new worktree of a bun repo "
            "(only while bun.enabled; never symlinks node_modules; a repo rig.yaml may set false)",
            default=True,
        ),
    },
)

_GIT_BLOCK = Block(
    doc=(
        "GLOBAL git settings: each `config` entry is written with `git config --global <key> <value>` "
        "(bools as true/false), idempotently; keys set by hand are never unset. GLOBAL-only — "
        "rejected from a repo rig.yaml."
    ),
    leaves={
        # No `default=` (same reason as env.enabled): block PRESENCE opts in; only an explicit
        # `enabled: false` opts out, and an absent block is a no-op — one scalar default can't say it.
        "enabled": Leaf("boolean", "provision the global git settings — block PRESENCE opts in; only an explicit false opts out"),
        "config": Leaf(
            "object",
            "dotted git keys (section.name / section.subsection.name) → value (string, bool or int)",
            additional_properties_type=("string", "boolean", "integer"),
            property_names_pattern=GIT_CONFIG_KEY_JSON_PATTERN,
        ),
    },
    schema_extra={"x-rig-global-only": True},
)

_PROFILE_BLOCK = Block(
    doc=(
        "`rig profile`: the (PRIVATE) git repo ~/.config/rig/config.yaml is synced through, both "
        "ways. Written by `rig profile init`. GLOBAL-only — rejected from a repo rig.yaml."
    ),
    leaves={
        "repo": Leaf("string", "the profile repo URL, e.g. https://git.hyperide.ai/<login>/.rig-profile (must be private)"),
        "auto_sync": Leaf(
            "boolean",
            "`rig apply commit` runs `rig profile sync` first (a failure warns and applies the local file); "
            "false turns it off on every machine that shares this profile",
            default=True,
        ),
        "files": Leaf(
            "object",
            "per-file switches for what `rig profile` carries besides config.yaml: tmux, omp_config, "
            "omp_mcp, sysmon (each true by default; false stops syncing that file on this machine and "
            "every machine that shares the profile). config.yaml itself always syncs.",
            additional_properties_type="boolean",
            property_names_pattern="^(tmux|omp_config|omp_mcp|sysmon)$",
        ),
    },
    schema_extra={"x-rig-global-only": True},
)


_SHIP_DELEGATOR_BLOCK = Block(
    doc="a per-repo .claude/scripts/pr-ship.sh delegator so `gh ship` works in this repo (ignored in .git/info/exclude).",
    leaves={
        "enabled": Leaf("boolean", "provision the per-repo gh-ship delegator", default=True),
    },
)
