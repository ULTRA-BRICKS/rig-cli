"""Validators for the ``scripts`` map and the ``dev`` block (agent-tools ``dev`` CLI metadata).

Both feed the same consumer (dev-cli), so they live together and apart from the general blocks.
"""

from __future__ import annotations

from typing import Any

from .config_checks import (
    check_bool,
    check_port,
    check_port_list,
    check_str,
    check_str_list,
    reject_unknown_keys,
)
from .config_errors import ConfigError

SCRIPT_KEYS = {"cmd"}


def _check_str_keys(spec: dict[Any, Any], path: str) -> None:
    """Every key of a mapping entry must be a string."""
    non_str = [k for k in spec if not isinstance(k, str)]
    if non_str:
        raise ConfigError(f"{path} keys must be strings (got {non_str[0]!r})", schema_path=path)


def _validate_script_entry(name: str, spec: Any) -> None:
    """Validate one ``scripts.<name>`` value: a command string or ``{cmd: ...}``."""
    path = f"scripts.{name}"
    if isinstance(spec, str):
        # dev-cli itself strips and rejects an empty/whitespace-only command string (a
        # non-empty command is its actual contract) — reject it here too, so a malformed
        # config is caught at `rig apply`/`status` time instead of surfacing later as a
        # confusing dev-cli runtime error.
        if not spec.strip():
            raise ConfigError(
                f"{path} must be a non-empty command string, got {spec!r}", schema_path=path
            )
        return
    if not isinstance(spec, dict):
        raise ConfigError(
            f"{path} must be a string or a mapping with cmd",
            schema_path=path,
        )
    _check_str_keys(spec, path)
    unknown = sorted(set(spec) - SCRIPT_KEYS)
    if unknown:
        bad = unknown[0]
        raise ConfigError(
            f"unknown {path} key: {bad}",
            why=f"{bad} is not a known scripts entry key",
            fix="use cmd, or make the script value a string",
            schema_path=f"{path}.{bad}",
        )
    cmd = spec.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        raise ConfigError(
            f"{path} requires a non-empty cmd string",
            schema_path=f"{path}.cmd",
        )


def validate_scripts(scripts: Any) -> None:
    """Validate the top-level ``scripts`` map consumed by agent-tools ``dev run <name>``."""
    if not isinstance(scripts, dict):
        raise ConfigError("scripts must be a mapping", schema_path="scripts")
    for name, spec in scripts.items():
        if not isinstance(name, str):
            raise ConfigError(f"scripts keys must be strings (got {name!r})", schema_path="scripts")
        _validate_script_entry(name, spec)


DEV_KEYS = {"server", "e2e"}
# ``port`` (singular) is dev-cli's own documented fallback for a single-port server (it reads
# ``ports`` first, then falls back to ``[port]`` if ``ports`` is absent — see dev-cli's
# ``_entry_ports``). rig's own validator must accept the same alias or an existing config using
# ``port:`` starts failing ``rig apply``/``rig status`` the moment this stricter key-set landed
# (a real regression found in review, not a hypothetical).
DEV_SERVER_KEYS = {"script", "url", "ready_url", "port", "ports", "process_matchers", "logs_root"}
DEV_E2E_KEYS = {"script", "requires_server", "artifacts_root", "logs_root", "jobs"}
DEV_E2E_JOB_KEYS = {"script", "requires_server", "artifacts_root", "logs_root"}


def _validate_dev_server(server: Any) -> None:
    if server is None:
        return
    if not isinstance(server, dict):
        raise ConfigError("dev.server must be a mapping", schema_path="dev.server")
    reject_unknown_keys(server, "dev.server")
    check_str(server, "script", "dev.server.script")
    check_str(server, "url", "dev.server.url")
    check_str(server, "ready_url", "dev.server.ready_url")
    check_port(server, "port", "dev.server.port")
    check_port_list(server, "ports", "dev.server.ports")
    # dev-cli reads `ports` first and only falls back to `port` when `ports` is ABSENT — so a
    # config declaring both has one silently-dead key (Fable review). Every other check here
    # is strict about ambiguity; reject the combination instead of accepting a config where
    # one of the two values is quietly ignored.
    if server.get("port") is not None and server.get("ports") is not None:
        raise ConfigError(
            "dev.server must not declare both port and ports",
            why="dev-cli reads ports first and only falls back to port when ports is absent, "
            "so declaring both leaves one value silently ignored",
            fix="declare only one of dev.server.port or dev.server.ports",
            schema_path="dev.server.port",
        )
    check_str_list(server, "process_matchers", "dev.server.process_matchers")
    check_str(server, "logs_root", "dev.server.logs_root")


def _validate_e2e_job(name: str, job: Any) -> None:
    path = f"dev.e2e.jobs.{name}"
    if not isinstance(job, dict):
        raise ConfigError(f"{path} must be a mapping", schema_path=path)
    _check_str_keys(job, path)
    unknown = sorted(set(job) - DEV_E2E_JOB_KEYS)
    if unknown:
        bad = unknown[0]
        raise ConfigError(
            f"unknown {path} key: {bad}",
            why=f"{bad} is not a known dev e2e job key",
            fix="use script, requires_server, artifacts_root, or logs_root",
            schema_path=f"{path}.{bad}",
        )
    check_str(job, "script", f"{path}.script")
    check_bool(job, "requires_server", f"{path}.requires_server")
    check_str(job, "artifacts_root", f"{path}.artifacts_root")
    check_str(job, "logs_root", f"{path}.logs_root")


def _validate_e2e_jobs(jobs: Any) -> None:
    if jobs is None:
        return
    if not isinstance(jobs, dict):
        raise ConfigError("dev.e2e.jobs must be a mapping", schema_path="dev.e2e.jobs")
    for name, job in jobs.items():
        if not isinstance(name, str):
            raise ConfigError(
                f"dev.e2e.jobs keys must be strings (got {name!r})", schema_path="dev.e2e.jobs"
            )
        _validate_e2e_job(name, job)


def _validate_dev_e2e(e2e: Any) -> None:
    if e2e is None:
        return
    if not isinstance(e2e, dict):
        raise ConfigError("dev.e2e must be a mapping", schema_path="dev.e2e")
    reject_unknown_keys(e2e, "dev.e2e")
    check_str(e2e, "script", "dev.e2e.script")
    check_bool(e2e, "requires_server", "dev.e2e.requires_server")
    check_str(e2e, "artifacts_root", "dev.e2e.artifacts_root")
    check_str(e2e, "logs_root", "dev.e2e.logs_root")
    _validate_e2e_jobs(e2e.get("jobs"))


def validate_dev(dev: Any) -> None:
    """Validate repo-level metadata consumed by the agent-tools ``dev`` CLI."""
    if not isinstance(dev, dict):
        raise ConfigError("dev must be a mapping", schema_path="dev")
    reject_unknown_keys(dev, "dev")
    _validate_dev_server(dev.get("server"))
    _validate_dev_e2e(dev.get("e2e"))
