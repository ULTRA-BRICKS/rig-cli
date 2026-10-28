"""Line-oriented TOML read/edit helpers for files Rig must not reformat (codex config)."""

from __future__ import annotations

import json


_TOML_UNVERIFIABLE_REMEDY = (
    "no TOML parser on this Python (3.10 has no tomllib) — install the tomli backport "
    "(`pip install tomli`) so rig can verify the file before and after the write"
)


def _toml_parser():
    """The TOML parser module, or ``None`` when this Python has none (3.10 without the ``tomli``
    backport). Lazy import: ``tomllib`` is stdlib but absent on 3.10, and never needed for
    ``rig --help``."""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return None
    return tomllib


def _toml_parses(text: str) -> bool | None:
    """True/False when ``text`` is valid/invalid TOML; ``None`` when it CANNOT be checked (no
    parser, see :func:`_toml_parser`) — the mode writer then fails closed (never edits or creates
    a file it cannot verify) instead of handing codex a config it may not load."""
    tomllib = _toml_parser()
    if tomllib is None:
        return None
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    return True


def _toml_single_string_step(text: str, i: int, single: str) -> tuple[int, str | None]:
    """Consume one char (or one escape pair) inside a single-line string → (next index, open
    quote or None once the string closed)."""
    if text[i] == "\\" and single == '"':
        return i + 2, single
    if text[i] == single:
        return i + 1, None
    return i + 1, single


def _toml_skip_comment(text: str, i: int) -> int:
    """Index of the first char after the ``#`` comment starting at ``i``."""
    nl = text.find("\n", i)
    return len(text) if nl == -1 else nl + 1


def _toml_skip_triple_quoted(text: str, i: int) -> tuple[bool, int]:
    """Skip the triple-quoted string opening at ``i`` → (spans a newline, index after it)."""
    marker = text[i:i + 3]
    close = text.find(marker, i + 3)
    end = close if close != -1 else len(text)
    spans_newline = "\n" in text[i:end]
    return spans_newline, (end + 3 if close != -1 else len(text))


def _toml_has_multiline_string(text: str) -> bool:
    """True when ``text`` contains a genuine LINE-SPANNING TOML triple-quoted string — as
    opposed to a ``\"\"\"``/``'''`` substring inside a ``#`` comment, inside an ordinary
    single-line string, or a triple-quoted value balanced on ONE line (``x = \"\"\"abc\"\"\"``,
    harmless to a line-based scanner). Only a delimiter whose match spans a newline is a real
    risk: :func:`_toml_table_bounds` is line-based and has no triple-quote awareness at all, so a
    ``[table]``-looking line trapped inside a genuine multiline string would be misread as a real
    header. One left-to-right scan tracking comment/single-line-string state (the same state
    :func:`_toml_code_part` tracks per line, extended across the whole file and to triple-quote
    delimiters) — best-effort like the sibling TOML helpers, not a full parser.
    """
    i, n = 0, len(text)
    single: str | None = None
    while i < n:
        if single is not None:
            i, single = _toml_single_string_step(text, i, single)
            continue
        if text.startswith('"""', i) or text.startswith("'''", i):
            spans_newline, i = _toml_skip_triple_quoted(text, i)
            if spans_newline:
                return True
            continue
        ch = text[i]
        if ch == "#":
            i = _toml_skip_comment(text, i)
            continue
        single = ch if ch in ("'", '"') else None
        i += 1
    return False


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _toml_inline(value) -> str:
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_inline(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k} = {_toml_inline(v)}" for k, v in value.items()) + "}"
    raise TypeError(f"unsupported TOML value: {value!r}")


def _toml_value_delta(text: str) -> int:
    """Return bracket/brace nesting delta for one TOML value line, ignoring quoted strings."""
    delta = 0
    quote: str | None = None
    escaped = False
    for ch in text:
        if quote is not None:
            if escaped:
                escaped = False
            elif quote == '"' and ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "#":
            break
        elif ch in "[{":
            delta += 1
        elif ch in "]}":
            delta -= 1
    return delta


def _toml_code_part(text: str) -> str:
    """Strip a TOML comment marker unless it appears inside a quoted string."""
    quote: str | None = None
    escaped = False
    for idx, ch in enumerate(text):
        if quote is not None:
            if escaped:
                escaped = False
            elif quote == '"' and ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "#":
            return text[:idx]
    return text


def _toml_quote_step(ch: str, quote: str, escaped: bool) -> tuple[str | None, bool]:
    """Advance quoted-string state by one character → (open quote or None, escape pending)."""
    if escaped:
        return quote, False
    if quote == '"' and ch == "\\":
        return quote, True
    if ch == quote:
        return None, False
    return quote, False


def _toml_key_parts(key: str) -> list[str]:
    """Best-effort TOML dotted-key splitter with quoted key support."""
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in key:
        if quote is not None:
            current.append(ch)
            quote, escaped = _toml_quote_step(ch, quote, escaped)
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
        elif ch == ".":
            parts.append(_toml_key_part("".join(current).strip()))
            current = []
        else:
            current.append(ch)
    parts.append(_toml_key_part("".join(current).strip()))
    return [part for part in parts if part]


def _toml_key_part(part: str) -> str:
    if len(part) >= 2 and part[0] == part[-1] == "'":
        return part[1:-1]
    if len(part) >= 2 and part[0] == part[-1] == '"':
        try:
            loaded = json.loads(part)
            return loaded if isinstance(loaded, str) else part
        except json.JSONDecodeError:
            return part
    return part


def _toml_header_table_name(header: str) -> str | None:
    """Dotted table name of a ``[table]`` header line; ``None`` for an ``[[array]]`` header,
    which ends the current table but never names one."""
    if header.startswith("[[") and header.endswith("]]"):
        return None
    return ".".join(_toml_key_parts(header[1:-1].strip()))


def _toml_table_bounds(text: str, table: str) -> tuple[int, int] | None:
    lines = text.splitlines(keepends=True)
    offset = 0
    start = None
    value_depth = 0
    for line in lines:
        header = _toml_code_part(line).strip()
        if value_depth > 0:
            value_depth = max(0, value_depth + _toml_value_delta(header))
        elif header.startswith("[") and header.endswith("]"):
            if start is not None:
                return start, offset
            if _toml_header_table_name(header) == table:
                start = offset
        elif "=" in header:
            value_depth = max(0, _toml_value_delta(header.split("=", 1)[1]))
        offset += len(line)
    if start is None:
        return None
    return start, len(text)


def _toml_root_key_line(text: str, key: str) -> tuple[int, int, str] | None:
    """Locate ``key = value`` at the TOML ROOT (before any table header) → (start, end, value_code)."""
    offset = 0
    value_depth = 0
    for line in text.splitlines(keepends=True):
        code = _toml_code_part(line)
        body = code.strip()
        if value_depth > 0:
            value_depth = max(0, value_depth + _toml_value_delta(body))
        elif body.startswith("[") and body.endswith("]"):
            return None  # root section ended
        elif "=" in body:
            k, v = body.split("=", 1)
            if _toml_key_parts(k.strip()) == [key]:
                return offset, offset + len(line), v.strip()
            value_depth = max(0, _toml_value_delta(v))
        offset += len(line)
    return None


def _toml_scalar_string(value_code: str) -> str | None:
    """The str a basic/literal single-line TOML string denotes, else None (not a plain string).

    ``json.loads`` decodes the basic-string escapes — a deliberate shortcut (same one
    :func:`_toml_key_part` takes): the values here are the plain identifiers rig writes and reads
    back, not general TOML."""
    if len(value_code) >= 2 and value_code[0] == value_code[-1] == "'":
        return value_code[1:-1]
    if len(value_code) >= 2 and value_code[0] == value_code[-1] == '"':
        try:
            loaded = json.loads(value_code)
        except json.JSONDecodeError:
            return None
        return loaded if isinstance(loaded, str) else None
    return None


def read_toml_root_key(text: str, key: str) -> tuple[str, object]:
    """READ-ONLY probe of a root-level string key: ``("missing"|"present"|"conflict", value)``.

    ``conflict`` carries the raw value code when the key holds something rig must not rewrite as
    a string (an inline table, array, number, multiline string)."""
    # only the ROOT section is parsed here — a multiline string inside a later [table] is
    # irrelevant and must not turn into permanent false drift for the root key.
    table_offset = _toml_first_table_offset(text)
    root = text if table_offset is None else text[:table_offset]
    root_code = "".join(_toml_code_part(line) for line in root.splitlines(keepends=True))
    if '"""' in root_code or "'''" in root_code:
        return "conflict", "TOML multiline strings in the root section are unsupported by the root-key merge"
    found = _toml_root_key_line(text, key)
    if found is None:
        return "missing", None
    _, _, value_code = found
    scalar = _toml_scalar_string(value_code)
    if scalar is None:
        return "conflict", f"{key} = {value_code}"
    return "present", scalar


def upsert_toml_root_key(text: str, key: str, value: str) -> tuple[str, str | None]:
    """Set ``key = "value"`` in the ROOT section, preserving every other byte of the file.

    An existing root key is rewritten in place (its trailing comment kept); an absent one is
    appended to the root section — BEFORE the first table header, since a root key placed after
    a header would belong to that table. Returns ``(merged, conflict)``; on a conflict the text
    comes back unchanged."""
    state, current = read_toml_root_key(text, key)
    if state == "conflict":
        return text, str(current)
    rendered = f"{key} = {_toml_string(value)}"
    if state == "present":
        start, end, _ = _toml_root_key_line(text, key)  # type: ignore[misc]
        line = text[start:end]
        code = _toml_code_part(line)
        return text[:start] + rendered + line[len(code.rstrip()):] + text[end:], None
    table_offset = _toml_first_table_offset(text)
    if table_offset is None:
        prefix = text.rstrip("\n")
        return (prefix + "\n" if prefix else "") + rendered + "\n", None
    prefix = text[:table_offset].rstrip("\n")
    suffix = text[table_offset:]
    return (prefix + "\n" if prefix else "") + rendered + "\n\n" + suffix, None


def _toml_first_table_offset(text: str) -> int | None:
    """Character offset of the first TOML table header outside a multiline inline value."""
    offset = 0
    value_depth = 0
    for line in text.splitlines(keepends=True):
        body = _toml_code_part(line).strip()
        if value_depth > 0:
            value_depth = max(0, value_depth + _toml_value_delta(body))
        elif body.startswith("[") and body.endswith("]"):
            return offset
        elif "=" in body:
            value_depth = max(0, _toml_value_delta(body.split("=", 1)[1]))
        offset += len(line)
    return None
