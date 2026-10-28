"""Validation of the ``task`` config block and its tracker base-URL / repo-coordinate rules.

Separate from ``config.py`` because the block fans out into four nested backend sections
(github/linear/tasks/forgejo) whose URL checks share security-sensitive no-echo rules; keeping
them together makes that rule auditable in one place.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import SplitResult, urlsplit

from .config_checks import reject_unknown_keys
from .config_constants import (
    FORGEJO_REPO_AUTO,
    FORGEJO_REPO_RE,
    FORGEJO_URL_FIX,
    VALID_TASK_ATTACHMENT_MODES,
    VALID_TASK_BACKENDS,
)
from .config_errors import ConfigError


def https_base_problem(url: str, noun: str) -> tuple[str | None, SplitResult | None]:
    """The checks every tracker base URL shares: ``(problem, urlsplit-parts)``.

    ``noun`` ("URL" or "origin") only words the message. The returned text NEVER contains the
    value itself: a rejected base URL is exactly where a credential tends to hide
    (``user:token@``, ``?access_token=``, a truncated ``oauth2:token``), and the message is printed
    by ``rig status``/``config set``/config-web and ends up in logs. Order matters only for message
    quality — every branch is fail-closed.
    """
    if "@" in url or "%" in url:
        return "must not embed credentials or percent-escapes (user:token@host, %40)", None
    if any(c.isspace() for c in url):
        return "must not contain whitespace", None
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port  # raises ValueError on a non-numeric port, e.g. a truncated "oauth2:token"
    except ValueError:  # also an unterminated IPv6 netloc ("https://[::1")
        return f"must be an https {noun} with a valid host[:port]", None
    if parts.scheme != "https" or not host:
        return f"must be an https {noun} with a host", None
    if "?" in url or "#" in url:  # a query/fragment would also hide an /api/v1 suffix
        return "must not carry a query or fragment", None
    return None, parts


def forgejo_url_problem(url: str) -> str | None:
    """The first reason ``url`` is not a usable Forgejo BASE URL, or ``None`` when it is.

    :func:`https_base_problem`, then: no ``/api/v1`` suffix and no trailing ``/`` (task-cli
    derives the API base by appending ``/api/v1``). A sub-path is allowed (a Forgejo under a path).
    """
    problem, _parts = https_base_problem(url, "URL")
    if problem is not None:
        return problem
    if url.rstrip("/").lower().endswith("/api/v1"):
        return "must not include the /api/v1 suffix (task-cli appends it to derive the API base)"
    if url.endswith("/"):
        return "must not end with '/' (task-cli appends /api/v1, which would double the slash)"
    return None


def tasks_url_problem(url: str) -> str | None:
    """The first reason ``url`` is not a usable Tasks-app origin, or ``None`` when it is.

    Mirrors task-cli's tasks backend (``_validate_origin``), which sends its bearer token to this
    origin: :func:`https_base_problem`, then no path at all (task-cli appends ``/api/v1`` itself);
    a single trailing ``/`` is tolerated as task-cli does.
    """
    problem, parts = https_base_problem(url, "origin")
    if problem is not None:
        return problem
    if parts is None:  # unreachable today; stays fail-closed if https_base_problem ever changes
        return "must be an https origin with a valid host[:port]"
    if parts.path not in ("", "/"):
        return "must be a bare origin with no path (task-cli appends /api/v1 itself)"
    return None


def validate_forgejo_url(url: Any) -> None:
    """Validate ``task.forgejo.url`` (UB-25): a bare ``https://host[:port][/path]`` base URL.
    The Forgejo REST API base is DERIVED at runtime by appending ``/api/v1``, so a value that
    already carries it (or a trailing ``/``, query, or fragment) would produce a broken path.
    See :func:`forgejo_url_problem` for the full rule set and why the value is never echoed.
    """
    if url is None:
        return
    if not isinstance(url, str):
        raise ConfigError(
            f"task.forgejo.url must be a string, got {type(url).__name__}", schema_path="task.forgejo.url"
        )
    problem = forgejo_url_problem(url)
    if problem is not None:
        raise ConfigError(
            f"task.forgejo.url {problem}",
            why="the value is not echoed: a rejected base URL may carry a credential",
            fix=FORGEJO_URL_FIX,
            schema_path="task.forgejo.url",
        )


def validate_forgejo_repo(repo: Any, schema_path: str = "task.forgejo.repo") -> None:
    """Validate a Forgejo repo coordinate (UB-25): optional, but when given must be either the
    ``auto`` sentinel (resolve from the repository's own git remote at runtime — task-cli's
    default, same sentinel as ``github.repo``) or an ``owner/name`` shape: exactly two non-empty,
    whitespace-free segments separated by exactly one ``/``. ``schema_path`` names the key the
    user wrote — the nested ``task.forgejo.repo`` or the flat ``task.repo`` shorthand, which
    task-cli also fans out onto ``forgejo.repo``.
    """
    # Never echo the value (same rule as the url): a pasted token lands here too, and the
    # message is printed/logged.
    if repo is None:
        return
    if not isinstance(repo, str) or (repo != FORGEJO_REPO_AUTO and not FORGEJO_REPO_RE.fullmatch(repo)):
        raise ConfigError(
            f"{schema_path} must be 'auto' or an owner/name string",
            why="the value is not echoed: a rejected repo value may carry a pasted credential",
            fix="use a value like ultrabricks/rig-cli, or 'auto' to resolve from the git remote",
            schema_path=schema_path,
        )


def validate_task_enum(value: Any, valid: set[str], schema_path: str) -> None:
    """Shared enum check for the ``task`` block's five link/native + backend enums (review
    finding: isinstance-before-membership, so a non-hashable value fails closed with a clean
    ConfigError rather than a raw TypeError from the ``in`` check against ``valid``)."""
    if value is None:
        return
    if not isinstance(value, str) or value not in valid:
        raise ConfigError(
            f"{schema_path} must be one of {sorted(valid)}, got {value!r}",
            fix=f"use one of: {', '.join(sorted(valid))}",
            schema_path=schema_path,
        )


def _check_optional_str(block: dict[str, Any], key: str, path: str, *, echo_value: bool) -> None:
    """``block[key]`` must be a string when present. ``echo_value=False`` reports only the type
    name: values under forgejo/tasks coordinates may be pasted credentials."""
    value = block.get(key)
    if value is None or isinstance(value, str):
        return
    shown = repr(value) if echo_value else type(value).__name__
    raise ConfigError(f"{path}.{key} must be a string, got {shown}", schema_path=f"{path}.{key}")


def _nested_section(t: dict[str, Any], name: str) -> dict[str, Any]:
    """The nested ``task.<name>`` mapping, with null normalised to ``{}`` and unknown keys rejected.

    ``t.get(name, {})`` only falls back when the KEY is absent; an explicit ``github:`` header with
    nothing under it parses to ``None`` and must behave like absent (as ``enforce``/``classify``/
    ``session``/``projects`` already do). Only null->``{}`` is normalised, NOT every falsy value:
    ``github: false`` must still fail rather than be silently swallowed. (Top-level ``task: null``
    still rejects — the convention every other block follows, pinned by its own test.)
    """
    block = t.get(name)
    block = {} if block is None else block
    if not isinstance(block, dict):
        raise ConfigError(f"task.{name} must be a mapping", schema_path=f"task.{name}")
    reject_unknown_keys(block, f"task.{name}")
    return block


def _validate_flat_coordinates(t: dict[str, Any]) -> None:
    validate_task_enum(t.get("backend"), VALID_TASK_BACKENDS, "task.backend")
    for strkey in ("team", "project", "repo"):
        # type name only, never the value: under backend: forgejo the flat `repo` is the
        # forgejo.repo shorthand, which falls under the same no-echo rule.
        _check_optional_str(t, strkey, "task", echo_value=False)
    validate_task_enum(t.get("attachment_mode"), VALID_TASK_ATTACHMENT_MODES, "task.attachment_mode")
    if t.get("backend") == "forgejo":
        validate_forgejo_repo(t.get("repo"), "task.repo")


def _validate_github(t: dict[str, Any]) -> None:
    github = _nested_section(t, "github")
    _check_optional_str(github, "repo", "task.github", echo_value=True)


def _validate_linear(t: dict[str, Any]) -> None:
    linear = _nested_section(t, "linear")
    for strkey in ("team", "project"):
        _check_optional_str(linear, strkey, "task.linear", echo_value=True)
    validate_task_enum(linear.get("attachment_mode"), VALID_TASK_ATTACHMENT_MODES, "task.linear.attachment_mode")


def _validate_tasks_section(t: dict[str, Any]) -> None:
    tasks_block = _nested_section(t, "tasks")
    for strkey in ("url", "team"):
        _check_optional_str(tasks_block, strkey, "task.tasks", echo_value=False)
    tasks_url = tasks_block.get("url")
    # an empty string is no origin either (same as task.forgejo.url): only an absent key is unset
    problem = tasks_url_problem(tasks_url) if tasks_url is not None else None
    if problem is not None:
        raise ConfigError(
            f"task.tasks.url {problem}",
            why="the value is not echoed: a rejected base URL may carry a credential",
            fix="set task.tasks.url to the Tasks app's bare https origin, e.g. https://tasks.example.com",
            schema_path="task.tasks.url",
        )
    validate_task_enum(tasks_block.get("attachment_mode"), VALID_TASK_ATTACHMENT_MODES, "task.tasks.attachment_mode")


def _validate_forgejo_section(t: dict[str, Any]) -> None:
    forgejo_block = _nested_section(t, "forgejo")
    validate_forgejo_url(forgejo_block.get("url"))
    validate_forgejo_repo(forgejo_block.get("repo"))
    validate_task_enum(
        forgejo_block.get("attachment_mode"), VALID_TASK_ATTACHMENT_MODES, "task.forgejo.attachment_mode"
    )


def _validate_opaque_shapes(t: dict[str, Any]) -> None:
    projects = t.get("projects")
    if projects is not None and not isinstance(projects, list):
        raise ConfigError(f"task.projects must be a list, got {projects!r}", schema_path="task.projects")
    for objkey in ("enforce", "classify", "session"):
        value = t.get(objkey)
        if value is not None and not isinstance(value, dict):
            raise ConfigError(f"task.{objkey} must be a mapping, got {value!r}", schema_path=f"task.{objkey}")


def validate_task(t: dict[str, Any]) -> None:
    """Validate the ``task`` block — task-cli's per-repo ticket-tracker routing, PLUS the
    unrelated ``code_prefix`` leaf folded into the same top-level block (rig-cli#217).

    task-cli reads the routing keys DIRECTLY (``tasklib.config.rig_task_overlay``) at runtime —
    rig itself never materializes anything from them. This validator exists so a typo fails FAST,
    at `rig status`/`rig apply` time, with rig's own schema-pointer error format, rather than only
    surfacing later as task-cli's own (differently-formatted) `ConfigError`. ``code_prefix`` is
    different: `rig apply` DOES materialize it into `.ship-config`'s `SHIP_TASK_CODE_PREFIX` line
    (see `plan.py`'s `_build_ship_task_prefix`); its value contract is re-checked there at
    plan-build time, so here it is only accepted as a known key via ``reject_unknown_keys``.

    Deliberately shallow on ``projects``/``enforce``/``classify``/``session``: those are
    task-cli's own nested shapes — only their outer TYPE is checked. Fail-closed on: a non-mapping
    block, an unknown key, a bad ``backend``/``attachment_mode`` enum, non-string flat coordinates,
    non-mapping ``github``/``linear``/``tasks``/``forgejo`` sections, and unknown keys or a bad
    enum WITHIN them. ``tasks`` (UB-23) and ``forgejo`` (UB-25) also get URL/repo shape checks.

    Every enum check does an ``isinstance(value, str)`` check BEFORE the set-membership test:
    ``in`` on a set needs a HASHABLE left operand, so ``backend: []`` would otherwise raise a raw
    ``TypeError`` instead of the promised fail-closed ``ConfigError``.
    """
    if not isinstance(t, dict):
        raise ConfigError("task must be a mapping", schema_path="task")
    if not t:
        return
    reject_unknown_keys(t, "task")
    _validate_flat_coordinates(t)
    _validate_github(t)
    _validate_linear(t)
    _validate_tasks_section(t)
    _validate_forgejo_section(t)
    _validate_opaque_shapes(t)
