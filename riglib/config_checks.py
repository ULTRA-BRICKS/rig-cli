"""Shared scalar/shape guards for the per-block config validators.

Why a separate module: every ``config_validate_*`` module needs the same fail-closed primitives (unknown
keys, bool/str/int/port checks) and none of them may import ``config.py`` (it imports them).
"""

from __future__ import annotations

from typing import Any

from .config_errors import ConfigError

def reject_unknown_keys(block: dict[str, Any], block_path: str) -> None:
    """Reject any key in ``block`` that the schema does not declare for ``block_path``.

    The single unknown-key gate, sourced from :mod:`riglib.config_schema` (the SAME registry the
    published JSON schema is emitted from), so the validator and ``schema/rig.schema.json`` can
    never disagree on what is a typo. Raises a 3-part :class:`ConfigError` whose schema path points
    at the FIRST offending key and whose fix lists the keys that block DOES accept. ``block_path``
    is the dotted path of the parent block (``""`` for the document root, ``github.ruleset`` for a
    nested block).

    A block carrying an OPEN ``items``/``fragments`` map (ci, mcp, agent_hooks, skills.by_type,
    git_hooks.dispatcher) keeps that map key as valid — only OTHER unknown keys are rejected.
    """
    from . import config_schema

    if block_path:
        valid = config_schema.block_child_keys(block_path)
    else:
        valid = set(config_schema.TOP_LEVEL_KEYS) | {"scope"}  # tolerate the legacy top key
    if valid is None:
        return  # not a registry-known block (defensive — caller passes a known path)
    unknown = sorted(set(block) - valid)
    if not unknown:
        return
    bad = unknown[0]
    # "top-level" at the document root, else the dotted block path ("github.ruleset").
    where = block_path or "top-level"
    key_path = f"{block_path}.{bad}" if block_path else bad
    plural = "key" if len(unknown) == 1 else "keys"
    accepted = ", ".join(sorted(valid - {"scope"}))
    raise ConfigError(
        f"unknown {where} {plural}: {', '.join(unknown)}",
        why=f"{', '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} not a known "
        f"{where} key — likely a typo (validated against {config_schema.SCHEMA_REL_PATH})",
        fix=f"remove it, or use one of: {accepted}",
        schema_path=key_path,
    )


def check_bool(block: dict[str, Any], key: str, path: str) -> None:
    """Fail-closed if ``block[key]`` is present and not a bool, naming the schema ``path``.

    A tiny shared guard so every block's bool knob (``enabled``/``all``/…) rejects a typo'd value
    with the schema path attached — the runtime and the JSON schema then agree on the type. NB:
    ``bool`` is an int subclass, so this also rejects an int posing as a bool only implicitly; use
    a dedicated check where an int and a bool must be told apart (e.g. ``required_reviews``).
    """
    value = block.get(key)
    if value is not None and not isinstance(value, bool):
        raise ConfigError(f"{path} must be a bool, got {value!r}", schema_path=path)


def check_str(block: dict[str, Any], key: str, path: str) -> None:
    """Fail-closed if ``block[key]`` is present and not a string, naming the schema ``path``."""
    value = block.get(key)
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"{path} must be a string, got {value!r}", schema_path=path)


def check_int_min(block: dict[str, Any], key: str, path: str, minimum: int) -> None:
    value = block.get(key)
    if key in block and (isinstance(value, bool) or not isinstance(value, int) or value < minimum):
        raise ConfigError(f"{path} must be an int >= {minimum}, got {value!r}", schema_path=path)


def check_str_list(block: dict[str, Any], key: str, path: str) -> None:
    value = block.get(key)
    if value is not None and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
        raise ConfigError(f"{path} must be a list of strings, got {value!r}", schema_path=path)


PORT_MIN, PORT_MAX = 1, 65535



def is_valid_port(value: Any) -> bool:
    """True iff ``value`` is a plain (non-bool) int TCP port in [1, 65535]."""
    return isinstance(value, int) and not isinstance(value, bool) and PORT_MIN <= value <= PORT_MAX


def check_port(block: dict[str, Any], key: str, path: str) -> None:
    """Validate a single-port ``port:`` value (dev-cli's fallback alias for ``ports: [port]``)."""
    value = block.get(key)
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path} must be an int, got {value!r}", schema_path=path)
    if not is_valid_port(value):
        raise ConfigError(
            f"{path} must be an int from {PORT_MIN} to {PORT_MAX}, got {value!r}", schema_path=path
        )


def check_port_list(block: dict[str, Any], key: str, path: str) -> None:
    value = block.get(key)
    if value is None:
        return
    if not isinstance(value, list) or any(isinstance(v, bool) or not isinstance(v, int) for v in value):
        raise ConfigError(f"{path} must be a list of ints, got {value!r}", schema_path=path)
    if any(not is_valid_port(v) for v in value):
        raise ConfigError(
            f"{path} entries must be ints from {PORT_MIN} to {PORT_MAX}, got {value!r}", schema_path=path
        )


def validate_string_list(value: Any, path: str) -> None:
    """Fail-closed unless ``value`` is a list of strings."""
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ConfigError(f"{path} must be a list of strings", schema_path=path)


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
