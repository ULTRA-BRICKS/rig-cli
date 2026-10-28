"""Config cascade loader + schema validation for ``rig.yaml``.

Two layers, cascaded by **location** (no scope flag):

1. **Global** — ``~/.config/rig/config.yaml`` (or ``$XDG_CONFIG_HOME/rig/config.yaml``).
   Machine-wide defaults a developer carries across repos.
2. **Per-repo** — ``rig.yaml`` at the repo root. Committed by default; it is the
   reproducible source of truth and **overrides** the global layer.

The merge is a deep dict merge: per-repo keys win, dicts merge recursively, scalars and
lists replace wholesale (a list in rig.yaml fully replaces the global list — lists are
treated as atomic decisions, not appended, to keep the result predictable).

``yaml`` is imported lazily so ``rig --help``/``doctor`` work even if PyYAML is missing.
Validation is **fail-closed**: unknown top-level keys, unknown categories, and invalid
enum values abort before anything touches disk.

This module is the stable import surface: the implementation lives in the ``config_*`` modules
(``config_load`` cascade, ``config_validate*`` per-block validators, ``config_paths`` dotted paths,
``config_constants`` allow-lists) and every historical name is re-exported here unchanged.
"""

from __future__ import annotations

from .config_ancestors import (
    ancestor_task_overlay as _ancestor_task_overlay,
    is_within as _is_within,
    load_task_block_from_layer as _load_task_block_from_layer,
    read_ancestor_yaml as _read_ancestor_yaml,
    recombine_task_layers as _recombine_task_layers,
    task_inheritance_ancestor_dirs as _task_inheritance_ancestor_dirs,
    trusted_ancestor_config as _trusted_ancestor_config,
)
from .config_checks import (
    PORT_MAX as _PORT_MAX,
    PORT_MIN as _PORT_MIN,
    check_bool as _check_bool,
    check_int_min as _check_int_min,
    check_port as _check_port,
    check_port_list as _check_port_list,
    check_str as _check_str,
    check_str_list as _check_str_list,
    is_int as _is_int,
    is_valid_port as _is_valid_port,
    reject_unknown_keys as _reject_unknown_keys,
    validate_string_list as _validate_string_list,
)
from .config_constants import (
    CONFIG_FILENAME as CONFIG_FILENAME,
    DEPRECATED_HARNESS_KINDS as _DEPRECATED_HARNESS_KINDS,
    FORGEJO_REPO_AUTO as _FORGEJO_REPO_AUTO,
    FORGEJO_REPO_RE as _FORGEJO_REPO_RE,
    FORGEJO_URL_FIX as _FORGEJO_URL_FIX,
    GITHUB_OWNER_RE as _GITHUB_OWNER_RE,
    GLOBAL_LAYER_ONLY_BLOCKS as _GLOBAL_LAYER_ONLY_BLOCKS,
    MCP_ITEM_KEYS as _MCP_ITEM_KEYS,
    REPO_LAYER_FJ_KEYS as _REPO_LAYER_FJ_KEYS,
    RESERVED_HARNESS_KINDS as _RESERVED_HARNESS_KINDS,
    SHIP_POLICY_KEYS as _SHIP_POLICY_KEYS,
    SHIP_POLICY_OWNER_KEYS as _SHIP_POLICY_OWNER_KEYS,
    VALID_AUTONOMOUS_UNTIL as _VALID_AUTONOMOUS_UNTIL,
    VALID_CATEGORIES as _VALID_CATEGORIES,
    VALID_HARNESS_KINDS as _VALID_HARNESS_KINDS,
    VALID_LEGACY_AGENT_HOOK_TARGET_KINDS as _VALID_LEGACY_AGENT_HOOK_TARGET_KINDS,
    VALID_MODE_NAMES as _VALID_MODE_NAMES,
    VALID_ON_CONFLICT as _VALID_ON_CONFLICT,
    VALID_ON_ERROR as _VALID_ON_ERROR,
    VALID_TASK_ATTACHMENT_MODES as _VALID_TASK_ATTACHMENT_MODES,
    VALID_TASK_BACKENDS as _VALID_TASK_BACKENDS,
    VALID_TIERS as _VALID_TIERS,
    VALID_TMUX_APPLY as _VALID_TMUX_APPLY,
    VALID_TOP_KEYS as _VALID_TOP_KEYS,
)
from .config_errors import (
    ConfigError as ConfigError,
    render_config_error as render_config_error,
)
from .config_harness_fanout import (
    YAML_FALSE as _YAML_FALSE,
    YAML_TRUE as _YAML_TRUE,
    fan_out_keys_without_yaml as _fan_out_keys_without_yaml,
    harness_block as _harness_block,
    indent as _indent,
    load_harness_fan_out as load_harness_fan_out,
    yaml_scalar as _yaml_scalar,
)
from .config_load import (
    LoadedConfig as LoadedConfig,
    load as load,
    resolve_init_stack as resolve_init_stack,
    stack_requirement_warning as stack_requirement_warning,
)
from .config_managed_constants import (
    GITIGNORE_BEGIN_MARKER as GITIGNORE_BEGIN_MARKER,
    GITIGNORE_BLOCK_COMMENT as GITIGNORE_BLOCK_COMMENT,
    GITIGNORE_DEFAULT_ENTRIES as GITIGNORE_DEFAULT_ENTRIES,
    GITIGNORE_DEFAULT_EXCLUDESFILE as GITIGNORE_DEFAULT_EXCLUDESFILE,
    GITIGNORE_END_MARKER as GITIGNORE_END_MARKER,
    OMP_HOOK_BRIDGE_EXTENSION_NAME as OMP_HOOK_BRIDGE_EXTENSION_NAME,
    OPENCODE_HOOK_BRIDGE_EXCLUDE_BEGIN_MARKER as OPENCODE_HOOK_BRIDGE_EXCLUDE_BEGIN_MARKER,
    OPENCODE_HOOK_BRIDGE_EXCLUDE_COMMENT as OPENCODE_HOOK_BRIDGE_EXCLUDE_COMMENT,
    OPENCODE_HOOK_BRIDGE_EXCLUDE_END_MARKER as OPENCODE_HOOK_BRIDGE_EXCLUDE_END_MARKER,
    OPENCODE_HOOK_BRIDGE_PLUGIN_NAME as OPENCODE_HOOK_BRIDGE_PLUGIN_NAME,
    SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER as SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER,
    SHIP_DELEGATOR_EXCLUDE_COMMENT as SHIP_DELEGATOR_EXCLUDE_COMMENT,
    SHIP_DELEGATOR_EXCLUDE_END_MARKER as SHIP_DELEGATOR_EXCLUDE_END_MARKER,
    SHIP_DELEGATOR_REL_PATH as SHIP_DELEGATOR_REL_PATH,
    WORKTREES_DIR_NAME as WORKTREES_DIR_NAME,
    WORKTREES_EXCLUDE_BEGIN_MARKER as WORKTREES_EXCLUDE_BEGIN_MARKER,
    WORKTREES_EXCLUDE_COMMENT as WORKTREES_EXCLUDE_COMMENT,
    WORKTREES_EXCLUDE_END_MARKER as WORKTREES_EXCLUDE_END_MARKER,
)
from .config_paths import (
    FLAT_KEY_MAPS as _FLAT_KEY_MAPS,
    canonical_dot_path as canonical_dot_path,
    coerce_scalar as coerce_scalar,
    del_path as del_path,
    flat_key_index as _flat_key_index,
    get_path as get_path,
    global_config_path as global_config_path,
    repo_config_path as repo_config_path,
    set_path as set_path,
    split_path as split_path,
    split_segments as _split_segments,
)
from .config_validate import (
    validate as validate,
    validate_stack as _validate_stack,
)
from .config_validate_ci import (
    validate_agent_hooks as _validate_agent_hooks,
    validate_ci as _validate_ci,
    validate_git_hooks as _validate_git_hooks,
    validate_mcp as _validate_mcp,
)
from .config_validate_dev import (
    DEV_E2E_JOB_KEYS as _DEV_E2E_JOB_KEYS,
    DEV_E2E_KEYS as _DEV_E2E_KEYS,
    DEV_KEYS as _DEV_KEYS,
    DEV_SERVER_KEYS as _DEV_SERVER_KEYS,
    SCRIPT_KEYS as _SCRIPT_KEYS,
    validate_dev as _validate_dev,
    validate_scripts as _validate_scripts,
)
from .config_validate_env import (
    validate_bun as _validate_bun,
    validate_env as _validate_env,
    validate_gitignore as _validate_gitignore,
    validate_spotlight as _validate_spotlight,
)
from .config_validate_git import (
    validate_fj as _validate_fj,
    validate_git as _validate_git,
    validate_profile as _validate_profile,
)
from .config_validate_github import (
    GITHUB_ACTIONS_BOOL_KNOBS as _GITHUB_ACTIONS_BOOL_KNOBS,
    GITHUB_ACTIONS_ENUMS as _GITHUB_ACTIONS_ENUMS,
    GITHUB_BROWSER_BOOL_KNOBS as _GITHUB_BROWSER_BOOL_KNOBS,
    GITHUB_GHAS_BOOL_KNOBS as _GITHUB_GHAS_BOOL_KNOBS,
    GITHUB_MERGE_BOOL_KNOBS as _GITHUB_MERGE_BOOL_KNOBS,
    GITHUB_RULESET_BOOL_KNOBS as _GITHUB_RULESET_BOOL_KNOBS,
    GITHUB_RULESET_KEYS as _GITHUB_RULESET_KEYS,
    validate_github as _validate_github,
    validate_github_subblock as _validate_github_subblock,
)
from .config_validate_harness import (
    validate_harness as _validate_harness,
    validate_harness_fan_out as _validate_harness_fan_out,
)
from .config_validate_internal import (
    validate_internal_dev as _validate_internal_dev,
    validate_ship_delegator as _validate_ship_delegator,
)
from .config_validate_linters import (
    LINTER_BUNDLE_KEYS as LINTER_BUNDLE_KEYS,
    LINTER_ITEM_KEYS as LINTER_ITEM_KEYS,
    VALID_LINTER_ROLES as _VALID_LINTER_ROLES,
    linter_path_escapes_repo as linter_path_escapes_repo,
    validate_linters as _validate_linters,
)
from .config_validate_mode import (
    check_mode_bool as _check_mode_bool,
    check_mode_str as _check_mode_str,
    mode_mapping as _mode_mapping,
    validate_mode as _validate_mode,
)
from .config_validate_models import (
    DEFAULT_SCHEDULE_TIME as _DEFAULT_SCHEDULE_TIME,
    parse_hhmm as parse_hhmm,
    validate_agents_md as _validate_agents_md,
    validate_models as _validate_models,
)
from .config_validate_permissions import (
    NA_PERMISSIONS_KINDS as _NA_PERMISSIONS_KINDS,
    PERMISSIONS_KEYS as _PERMISSIONS_KEYS,
    PERMISSION_RULE_RE as _PERMISSION_RULE_RE,
    PERMISSION_TOOL_RE as _PERMISSION_TOOL_RE,
    VALID_PERMISSIONS_KINDS as _VALID_PERMISSIONS_KINDS,
    validate_permission_rule_lists as _validate_permission_rule_lists,
    validate_permissions as _validate_permissions,
)
from .config_validate_project_tools import (
    validate_ecosystem_readme as _validate_ecosystem_readme,
    validate_project_tools as _validate_project_tools,
)
from .config_validate_skills import (
    validate_by_stack_items as _validate_by_stack_items,
    validate_skills as _validate_skills,
    validate_skills_plugins as _validate_skills_plugins,
)
from .config_validate_sysmon import (
    SYSMON_BOOLS as _SYSMON_BOOLS,
    SYSMON_INTS as _SYSMON_INTS,
    SYSMON_LOWER_IS_WORSE as _SYSMON_LOWER_IS_WORSE,
    SYSMON_STRS as _SYSMON_STRS,
    SYSMON_STR_LISTS as _SYSMON_STR_LISTS,
    sysmon_fail as _sysmon_fail,
    validate_sysmon as _validate_sysmon,
)
from .config_validate_task import (
    forgejo_url_problem as _forgejo_url_problem,
    https_base_problem as _https_base_problem,
    tasks_url_problem as _tasks_url_problem,
    validate_forgejo_repo as _validate_forgejo_repo,
    validate_forgejo_url as _validate_forgejo_url,
    validate_task as _validate_task,
    validate_task_enum as _validate_task_enum,
)
from .config_validate_tmux import (
    _TMUX_SUBKEYS as _TMUX_SUBKEYS,
    _TMUX_TOP_KEYS as _TMUX_TOP_KEYS,
    validate_tmux as _validate_tmux,
)
from .config_validate_tools import (
    _TCC_WATCH_KEYS as _TCC_WATCH_KEYS,
    _TGCTL_REAPER_KEYS as _TGCTL_REAPER_KEYS,
    _TG_CTL_KEYS as _TG_CTL_KEYS,
    _TOOLS_ITEM_KEYS as _TOOLS_ITEM_KEYS,
    validate_tcc_watch as _validate_tcc_watch,
    validate_tg_ctl as _validate_tg_ctl,
    validate_tgctl_reaper as _validate_tgctl_reaper,
    validate_tools as _validate_tools,
    validate_tools_item as _validate_tools_item,
)
from .config_validate_tracked_files import (
    TRACKED_FILES_LAND_MODES as _TRACKED_FILES_LAND_MODES,
    validate_tracked_files as _validate_tracked_files,
)
from .config_validate_wrappers import (
    validate_harness_wrappers as _validate_harness_wrappers,
)
from .config_yaml import (
    deep_merge as _deep_merge,
    read_yaml_file as read_yaml_file,
)
from .project_tools import (
    HAFT_KEYS as HAFT_KEYS,
    HAFT_WORKFLOW_KEYS as HAFT_WORKFLOW_KEYS,
    PROJECT_TOOLS_KEYS as PROJECT_TOOLS_KEYS,
    SERENA_KEYS as SERENA_KEYS,
    SVERKLO_KEYS as SVERKLO_KEYS,
)
