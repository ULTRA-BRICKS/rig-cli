"""Codex hook-bridge TOML block rendering, conflict detection and registration handler."""

from __future__ import annotations

import shutil

from . import fsutil
from ..plan import Action
from .hook_bridge import (
    _CODEX_BRIDGE_BEGIN,
    _CODEX_BRIDGE_END,
    _CODEX_HOOK_EVENTS,
    _bridge_block,
    hook_bridge_entries,
    hook_bridge_settings_file,
)
from .result import ActionResult
from .shared import _should_backup
from .toml_edit import (
    _toml_code_part,
    _toml_first_table_offset,
    _toml_inline,
    _toml_key_parts,
    _toml_table_bounds,
    _toml_value_delta,
)


def codex_hook_bridge_block(
    action: Action, *, include_table_header: bool, dotted_keys: bool = False
) -> str:
    """Render the rig-managed Codex TOML hook block."""
    lines = [_CODEX_BRIDGE_BEGIN]
    if include_table_header:
        lines.append("[hooks]")
    key_prefix = "hooks." if dotted_keys else ""
    for event, pairs in hook_bridge_entries(action).items():
        blocks = [_bridge_block(matcher, command) for matcher, command in pairs]
        lines.append(f"{key_prefix}{event} = {_toml_inline(blocks)}")
    lines.append(_CODEX_BRIDGE_END)
    return "\n".join(lines) + "\n"


def codex_hook_bridge_block_bounds(text: str) -> tuple[int, int] | None:
    offset = 0
    start = None
    for line in text.splitlines(keepends=True):
        if line.strip() == _CODEX_BRIDGE_BEGIN:
            start = offset
        elif line.strip() == _CODEX_BRIDGE_END and start is not None:
            return start, offset + len(line)
        offset += len(line)
    return None


def codex_hook_bridge_block_malformed(text: str) -> bool:
    has_begin = any(line.strip() == _CODEX_BRIDGE_BEGIN for line in text.splitlines())
    has_end = any(line.strip() == _CODEX_BRIDGE_END for line in text.splitlines())
    return (has_begin or has_end) and codex_hook_bridge_block_bounds(text) is None


def codex_hook_bridge_block_has_table_header(block: str) -> bool:
    """True when the managed block owns the ``[hooks]`` table header."""
    for line in block.splitlines():
        stripped = line.strip()
        if stripped in (_CODEX_BRIDGE_BEGIN, _CODEX_BRIDGE_END) or not stripped:
            continue
        return stripped == "[hooks]"
    return False


def codex_hook_bridge_block_uses_dotted_keys(block: str) -> bool:
    """True when the managed block writes top-level ``hooks.<event>`` dotted keys."""
    for line in block.splitlines():
        stripped = line.strip()
        if stripped in (_CODEX_BRIDGE_BEGIN, _CODEX_BRIDGE_END) or not stripped:
            continue
        return stripped.startswith("hooks.")
    return False


def codex_hook_bridge_needs_table_header(text: str) -> bool:
    return _toml_table_bounds(text, "hooks") is None


def _toml_assignments(text: str, *, with_index: bool = False):
    """Yield ``(line, code, table, key, value)`` for each top-of-value ``key = value`` line,
    tracking the current table and skipping continuation lines of multi-line values. With
    ``with_index`` the first element is the line index instead of the line text."""
    table: str | None = None
    depth = 0
    for index, line in enumerate(text.splitlines(keepends=True)):
        code = _toml_code_part(line)
        body = code.strip()
        if not body:
            continue
        if depth > 0:
            depth = max(0, depth + _toml_value_delta(body))
            continue
        header = _codex_header_parts(body)
        if header is not None:
            table = ".".join(header[1])
            continue
        if "=" not in body:
            continue
        key, value = body.split("=", 1)
        depth = max(0, _toml_value_delta(value))
        yield (index if with_index else line), code, table, key, value


def _codex_hook_feature_flag(current_table: str | None, key_parts: list[str]) -> str | None:
    if current_table == "features" and key_parts == ["hooks"]:
        return "features.hooks"
    if current_table is None and key_parts == ["features", "hooks"]:
        return "features.hooks"
    if current_table is None and key_parts == ["codex_hooks"]:
        return "codex_hooks"
    return None


def codex_hook_bridge_disabled_features(text: str) -> tuple[str, ...]:
    """Return Codex feature flags that disable hooks globally."""
    disabled: list[str] = []
    for _line, _code, table, key, value in _toml_assignments(text):
        flag = _codex_hook_feature_flag(table, _toml_key_parts(key.strip()))
        if flag and value.strip() == "false":
            disabled.append(flag)
    return tuple(disabled)


def enable_codex_hook_bridge_features(text: str) -> tuple[str, bool]:
    """Enable Codex hook feature flags that would make the managed bridge inert."""
    # Only the ``false`` literal inside a feature-flag assignment line is rewritten; every
    # other byte (comments, ordering, CRLF) is carried through untouched.
    lines = text.splitlines(keepends=True)
    replaced: dict[int, str] = {}
    for index, code, table, key, value in _toml_assignments(text, with_index=True):
        flag = _codex_hook_feature_flag(table, _toml_key_parts(key.strip()))
        if not flag or value.strip() != "false":
            continue
        false_at = code.rfind("false")
        if false_at >= 0:
            line = lines[index]
            replaced[index] = code[:false_at] + "true" + code[false_at + len("false"):] + line[len(code):]
    for index, line in replaced.items():
        lines[index] = line
    return "".join(lines), bool(replaced)


def _codex_header_parts(body: str) -> tuple[bool, list[str]] | None:
    """(is array-of-tables, dotted key parts) of a ``[table]``/``[[array]]`` line, else None."""
    if body.startswith("[[") and body.endswith("]]"):
        return True, _toml_key_parts(body[2:-2].strip())
    if body.startswith("[") and body.endswith("]"):
        return False, _toml_key_parts(body[1:-1].strip())
    return None


def _codex_header_conflict(is_array: bool, parts: list[str]) -> str | None:
    name = ".".join(parts)
    if is_array:
        if parts and parts[0] == "hooks":
            return f"unmanaged [[{name}]] array-of-tables already exists"
        return None
    # [hooks.state] is Codex trust metadata (trusted_hash per hook path), not an event hook
    # table managed by the bridge.
    if parts[:1] == ["hooks"] and len(parts) > 1 and parts[:2] != ["hooks", "state"]:
        return f"unmanaged [{name}] table already exists"
    return None


def _codex_dotted_hooks_conflict(key_parts: list[str]) -> str | None:
    if key_parts[:2] == ["hooks", "state"]:
        return None
    if len(key_parts) > 1 and key_parts[1] in _CODEX_HOOK_EVENTS:
        return f"unmanaged hooks.{key_parts[1]} already exists"
    return f"unmanaged {'.'.join(key_parts)} uses dotted hooks TOML; cannot safely add a [hooks] table"


def _codex_key_conflict(body: str, current_table: str | None, in_hooks: bool) -> str | None:
    key = body.split("=", 1)[0].strip() if "=" in body else ""
    key_parts = _toml_key_parts(key)
    if current_table is None and key_parts == ["hooks"]:
        return "unmanaged hooks inline table already exists"
    if in_hooks and key_parts and key_parts[0] in _CODEX_HOOK_EVENTS:
        return f"unmanaged hooks.{key_parts[0]} already exists"
    if current_table is None and key_parts[:1] == ["hooks"]:
        return _codex_dotted_hooks_conflict(key_parts)
    return None


def _codex_conflict_body(line: str) -> str | None:
    """Code part of a line, or None for blank/comment-only lines."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    return _toml_code_part(stripped).strip()


def _codex_opened_value_depth(body: str) -> int:
    """Bracket depth a ``key = value`` line leaves open (0 when it has no assignment)."""
    if "=" not in body:
        return 0
    return max(0, _toml_value_delta(body.split("=", 1)[1]))


def codex_hook_bridge_conflict(text: str) -> str | None:
    """Return why Codex hooks cannot be merged without clobbering user TOML, else None."""
    if '"""' in text or "'''" in text:
        return "TOML multiline strings are unsupported by the Codex hook bridge merge"
    in_hooks = False
    current_table: str | None = None
    value_depth = 0
    for line in text.splitlines():
        body = _codex_conflict_body(line)
        if body is None:
            continue
        if value_depth > 0:
            value_depth = max(0, value_depth + _toml_value_delta(body))
            continue
        header = _codex_header_parts(body)
        if header is not None:
            conflict = _codex_header_conflict(*header)
            if conflict:
                return conflict
            current_table = ".".join(header[1])
            in_hooks = not header[0] and header[1] == ["hooks"]
            continue
        conflict = _codex_key_conflict(body, current_table, in_hooks)
        if conflict:
            return conflict
        value_depth = _codex_opened_value_depth(body)
    return None


def codex_hook_bridge_has_implicit_hooks_table(text: str) -> bool:
    """True when top-level dotted ``hooks.*`` keys already implicitly declare ``hooks``."""
    for _line, _code, table, key, _value in _toml_assignments(text):
        if table is None and _toml_key_parts(key.strip())[:1] == ["hooks"]:
            return True
    return False


def _insert_codex_hook_bridge_block(text: str, block: str, *, before_first_table: bool) -> str:
    """Insert a managed Codex hook block into top-level TOML text."""
    if before_first_table:
        table_offset = _toml_first_table_offset(text)
        if table_offset is not None:
            prefix = text[:table_offset].rstrip()
            suffix = text[table_offset:].lstrip("\n")
            merged = (prefix + "\n\n" if prefix else "") + block
            if suffix:
                merged += "\n" + suffix
            return merged
    prefix = text.rstrip()
    return (prefix + "\n\n" if prefix else "") + block


def merge_codex_hook_bridge_toml(existing: str, action: Action) -> tuple[str, str | None]:
    """Merge the Codex bridge block, preserving unrelated TOML text."""
    if codex_hook_bridge_block_malformed(existing):
        return existing, "managed Codex hook bridge markers are incomplete"
    enabled, _ = enable_codex_hook_bridge_features(existing)
    bounds = codex_hook_bridge_block_bounds(enabled)
    stripped = enabled
    if bounds is not None:
        start, end = bounds
        stripped = enabled[:start] + enabled[end:]
    conflict = codex_hook_bridge_conflict(stripped)
    if conflict:
        return existing, conflict
    if bounds is not None:
        start, end = bounds
        current = enabled[start:end]
        implicit_hooks = codex_hook_bridge_has_implicit_hooks_table(stripped)
        block = codex_hook_bridge_block(
            action,
            include_table_header=(
                codex_hook_bridge_block_has_table_header(current) and not implicit_hooks
            ),
            dotted_keys=implicit_hooks or codex_hook_bridge_block_uses_dotted_keys(current),
        )
        if implicit_hooks:
            return _insert_codex_hook_bridge_block(
                stripped, block, before_first_table=True
            ), None
        return enabled[:start] + block + enabled[end:], None
    hooks_bounds = _toml_table_bounds(stripped, "hooks")
    if hooks_bounds is not None:
        _, table_end = hooks_bounds
        block = codex_hook_bridge_block(action, include_table_header=False)
        prefix = stripped[:table_end].rstrip()
        suffix = stripped[table_end:].lstrip("\n")
        merged = prefix + "\n" + block
        if suffix:
            merged += "\n" + suffix
        return merged, None
    implicit_hooks = codex_hook_bridge_has_implicit_hooks_table(stripped)
    block = codex_hook_bridge_block(
        action,
        include_table_header=not implicit_hooks,
        dotted_keys=implicit_hooks,
    )
    return _insert_codex_hook_bridge_block(
        stripped, block, before_first_table=implicit_hooks
    ), None


def _do_register_codex_hook_bridge(action: Action, on_conflict: str) -> ActionResult:
    """Register codex_hook_bridge in ``config.toml`` with a managed TOML block."""
    config_file = hook_bridge_settings_file(action)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    existed = config_file.is_file()
    existing = ""
    if existed:
        try:
            existing = config_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return ActionResult(action, "error", f"hook_bridge/{action.item}: cannot read {config_file}: {exc}")
    had_managed_block = codex_hook_bridge_block_bounds(existing) is not None
    desired, conflict = merge_codex_hook_bridge_toml(existing, action)
    if conflict:
        detail = f"hook_bridge/{action.item}: {conflict} in {config_file}"
        if on_conflict == "skip":
            return ActionResult(action, "skipped", detail + " (on_conflict=skip), left untouched")
        return ActionResult(action, "error", detail)
    if desired == existing:
        return ActionResult(action, "skipped", f"hook_bridge/{action.item}: dispatcher already wired in {config_file}")
    if had_managed_block and on_conflict == "skip":
        return ActionResult(
            action,
            "skipped",
            f"hook_bridge/{action.item}: managed Codex hook bridge block drifted but left untouched "
            f"(on_conflict=skip) in {config_file}",
        )
    backup_note = ""
    if _should_backup(on_conflict) and existed:
        bak = fsutil.backup_path(config_file)
        shutil.copy2(str(config_file), str(bak))
        backup_note = f" (backed up prior → {bak})"
    config_file.write_text(desired, encoding="utf-8")
    status = "backed_up" if backup_note else ("updated" if existed else "created")
    return ActionResult(
        action,
        status,
        f"hook_bridge/{action.item}: wired Codex dispatcher hooks in {config_file}{backup_note}",
    )
