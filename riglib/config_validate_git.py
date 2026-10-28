"""Validators for the ``git``, ``profile`` and ``fj`` config blocks.

These three blocks are the machine-level (GLOBAL layer) git/forge settings: global ``git config``
entries, the profile-sync repo, and Forgejo access. They are grouped in one module because they
share the same "present block opts in, fail closed on shape" conventions and no other block
validator depends on their helpers. Heavy collaborators (:mod:`riglib.gitconfig`,
:mod:`riglib.fj`, :mod:`riglib.profile_files`) are imported lazily, as before the split, so loading
config stays cheap and import cycles are avoided.
"""

from __future__ import annotations

from typing import Any

from .config_checks import check_bool, reject_unknown_keys
from .config_errors import ConfigError
from .config_paths import global_config_path

_LINE_BREAK_CHARS = ("\r", "\n", "\x00")


# --- git -------------------------------------------------------------------------------------


def _reject_nested_mapping(key: Any) -> None:
    raise ConfigError(
        f"git.config.{key} is a nested mapping",
        why="git.config keys are FLAT dotted git keys, not nested YAML sections",
        fix=f"write `{key}.<name>: <value>` (e.g. `user.name: Alex`, not `user: {{name: Alex}}`), "
        "or `rig config set --global git.config.user.name Alex`",
        schema_path="git.config",
    )


def _reject_invalid_key(key: Any) -> None:
    raise ConfigError(
        f"git.config key {key!r} is not a valid git config key",
        why="a git key is section.name or section.subsection.name; section and name start "
        "with a letter and hold only letters, digits and '-'; no line breaks anywhere",
        fix="e.g. user.name, pull.rebase, init.defaultBranch, url.https://host/.insteadOf",
        schema_path="git.config",
    )


def _check_config_key(key: Any, seen: dict[str, str]) -> None:
    """Validate one ``git.config`` key's shape, ownership and uniqueness; record it in ``seen``."""
    from .gitconfig import RESERVED_KEYS, canonical_key, parse_key

    if parse_key(key) is None:
        _reject_invalid_key(key)
    canon = canonical_key(key)
    if canon in RESERVED_KEYS:
        raise ConfigError(
            f"git.config.{key} is managed by another rig block",
            why=RESERVED_KEYS[canon],
            fix=f"remove {key} from git.config and configure it in its owning block",
            schema_path="git.config",
        )
    if canon in seen:
        raise ConfigError(
            f"git.config declares {seen[canon]!r} and {key!r}, which git treats as the same key",
            why="git compares the section and name of a key case-insensitively",
            fix="keep one spelling",
            schema_path="git.config",
        )
    seen[canon] = key


def _check_config_value(key: Any, value: Any) -> None:
    if isinstance(value, (bool, int)):
        return
    if isinstance(value, str):
        if any(ch in value for ch in _LINE_BREAK_CHARS):
            raise ConfigError(
                f"git.config.{key} must be a single line, got {value!r}",
                why="each value is one `git config --global <key> <value>` call; drift compares "
                "it line-for-line",
                schema_path="git.config",
            )
        return
    kind = repr(value) if not isinstance(value, list) else "a list (multi-valued keys are not supported)"
    raise ConfigError(
        f"git.config.{key} must be a string, bool or int, got {kind}",
        fix='quote the value to pass it through verbatim (e.g. core.compression: "9")',
        schema_path="git.config",
    )


def _check_config_entries(cfg: dict[Any, Any]) -> None:
    seen: dict[str, str] = {}
    for key, value in cfg.items():
        if isinstance(value, dict):
            _reject_nested_mapping(key)
        _check_config_key(key, seen)
        _check_config_value(key, value)


def validate_git(g: dict[str, Any]) -> None:
    """Validate the ``git`` block — global git settings written with ``git config --global``.

    GLOBAL-only (the loader rejects it from any other layer). A PRESENT block opts in (like
    ``env``); only an ABSENT block or ``enabled: false`` is a no-op. Fail-closed on: a non-mapping
    block / ``config``, an unknown key, a non-bool ``enabled``, a key that is not git's
    ``section.name`` / ``section.subsection.name`` shape (:func:`riglib.gitconfig.parse_key`), two
    keys git would treat as the same key (section/name are case-insensitive), a key another rig
    area owns (``core.hooksPath`` / ``core.excludesfile``), and a value that is not a single-line
    string, a bool or an int (a nested mapping gets a "use dotted keys" fix; a list — a
    multi-valued key — is out of scope).
    """
    if not isinstance(g, dict):
        raise ConfigError("git must be a mapping", schema_path="git")
    if not g:
        return
    reject_unknown_keys(g, "git")
    check_bool(g, "enabled", "git.enabled")
    cfg = g.get("config")
    if cfg is None:
        return
    if not isinstance(cfg, dict):
        raise ConfigError(
            f"git.config must be a mapping of dotted git keys to values, got {cfg!r}",
            fix="e.g. `git: {config: {user.name: Alex, pull.rebase: true}}`",
            schema_path="git.config",
        )
    _check_config_entries(cfg)


# --- profile ---------------------------------------------------------------------------------


def _check_profile_files(files: Any) -> None:
    from .profile_files import file_ids

    switchable = [fid for fid in file_ids() if fid != "config"]
    if not isinstance(files, dict):
        raise ConfigError("profile.files must be a mapping of file id to bool", schema_path="profile.files")
    for fid, value in files.items():
        if fid not in switchable:
            raise ConfigError(
                f"unknown profile.files key {fid!r} (expected one of: {', '.join(switchable)})",
                schema_path="profile.files",
            )
        if not isinstance(value, bool):
            raise ConfigError(f"profile.files.{fid} must be a bool, got {value!r}", schema_path=f"profile.files.{fid}")


def _is_clone_safe_repo(repo: Any) -> bool:
    """True iff ``repo`` is a one-line, unpadded string that cannot read as a ``git clone`` option."""
    return (
        isinstance(repo, str)
        and bool(repo.strip())
        and repo.strip() == repo
        and not repo.startswith("-")
        and not any(ch in repo for ch in _LINE_BREAK_CHARS)
    )


def validate_profile(p: dict[str, Any]) -> None:
    """Validate the ``profile`` block — where ``rig profile sync`` keeps the global config.

    GLOBAL-only (the loader rejects it from any other layer). Fail-closed on a non-mapping block,
    an unknown key, a non-bool ``auto_sync``, and a ``repo`` that is not a non-empty single-line
    string or that starts with ``-`` (it is handed to ``git clone`` — an option-looking URL is
    refused outright rather than trusted to the ``--`` separator alone).
    """
    if not isinstance(p, dict):
        raise ConfigError("profile must be a mapping", schema_path="profile")
    if not p:
        return
    reject_unknown_keys(p, "profile")
    check_bool(p, "auto_sync", "profile.auto_sync")
    files = p.get("files")
    if files is not None:
        _check_profile_files(files)
    repo = p.get("repo")
    if repo is None:
        return
    if not _is_clone_safe_repo(repo):
        raise ConfigError(
            f"profile.repo must be a git URL (one line, no surrounding spaces), got {repo!r}",
            fix="e.g. https://git.hyperide.ai/<login>/.rig-profile",
            schema_path="profile.repo",
        )


# --- fj --------------------------------------------------------------------------------------


def _check_fj_version(version: Any) -> None:
    from . import fj

    if version is not None and str(version) not in fj.FJ_RELEASE_SHA256:
        raise ConfigError(
            f"fj.version {version!r} has no pinned checksum in this rig "
            f"(known: {', '.join(sorted(fj.FJ_RELEASE_SHA256))})",
            schema_path="fj.version",
        )


def _check_fj_text_keys(f: dict[str, Any]) -> None:
    for key in ("client_id", "bin_dir"):
        v = f.get(key)
        if v is not None and (not isinstance(v, str) or not v.strip() or "\n" in v):
            raise ConfigError(f"fj.{key} must be a non-empty single-line string", schema_path=f"fj.{key}")


def validate_fj(f: dict[str, Any]) -> None:
    """Validate the ``fj`` block — Forgejo access: install ``fj``, one-click OAuth login for fj
    and git, and a default host (``FJ_FALLBACK_HOST``). See :mod:`riglib.fj`.

    A PRESENT block opts in; ``host`` is then required. Fail-closed on a non-mapping block, an
    unknown key, a non-bool switch, a ``host`` that is not a bare host name (a scheme/trailing path
    is tolerated and stripped), a ``version`` rig has no pinned checksum for, and a blank client
    ID or ``bin_dir``.
    """
    from . import fj

    if not isinstance(f, dict):
        raise ConfigError("fj must be a mapping", schema_path="fj")
    if not f:
        return
    if "git_client_id" in f:
        raise ConfigError(
            "fj.git_client_id was removed",
            why="git no longer logs in through Git Credential Manager: it uses fj's own login via "
            "rig's git-credential-fj helper, so there is no second OAuth app to pick",
            fix=f"delete fj.git_client_id from {global_config_path()}",
            schema_path="fj.git_client_id",
        )
    reject_unknown_keys(f, "fj")
    for key in ("enabled", "install", "git_credentials"):
        check_bool(f, key, f"fj.{key}")
    if f.get("enabled") is False:
        return
    host = f.get("host")
    if not fj.is_valid_host(host):
        raise ConfigError(f"fj.host must be a host name like git.hyperide.ai, got {host!r}", schema_path="fj.host")
    _check_fj_version(f.get("version"))
    _check_fj_text_keys(f)
