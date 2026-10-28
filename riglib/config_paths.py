"""Config file locations and the dot-path get/set/del + scalar coercion behind `rig config get|set`.

Why a separate module: these helpers need only ``os``/``pathlib`` and ``ConfigError``, so the
validators and the cascade loader can both import them without pulling in each other. A dot path
indexes the YAML mapping tree (``harness.mode`` -> ``data["harness"]["mode"]``); it is used ONLY by
the targeted edit/read commands, never by the cascade loader, which always merges whole layers.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .config_constants import CONFIG_FILENAME
from .config_errors import ConfigError


def global_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "rig" / "config.yaml"


def repo_config_path(repo_root: Path) -> Path:
    return repo_root / CONFIG_FILENAME


# Maps whose KEYS are themselves dotted names — git's own ``section.name`` keys. A path that goes
# past one keeps everything after it as ONE key, so `rig config set --global git.config.user.name
# Alex` writes ``git: {config: {user.name: Alex}}`` (what the validator requires), not a nesting.
FLAT_KEY_MAPS: tuple[tuple[str, ...], ...] = (("git", "config"),)


def flat_key_index(parts: list[str]) -> int | None:
    """Index of the single flat-map key in ``parts`` when the path goes past a flat-key map."""
    for prefix in FLAT_KEY_MAPS:
        if len(parts) > len(prefix) and tuple(parts[: len(prefix)]) == prefix:
            return len(prefix)
    return None


class _SegmentScanner:
    """Character-at-a-time state for :func:`split_segments` (quote open, quoted segment closed)."""

    def __init__(self, dotted: str) -> None:
        self.dotted = dotted
        self.segments: list[str] = []
        self.buf: list[str] = []
        self.quote: str | None = None
        self.closed = False  # a quoted segment just ended: only spaces, a dot or the end may follow

    def feed(self, ch: str) -> None:
        if self.quote:
            self._feed_quoted(ch)
        elif ch == ".":
            self.segments.append("".join(self.buf).strip())
            self.buf = []
            self.closed = False
        elif self.closed:
            self._feed_after_quoted(ch)
        elif ch in "\"'" and not "".join(self.buf).strip():
            self.quote = ch
            self.buf = []
        else:
            self.buf.append(ch)

    def _feed_quoted(self, ch: str) -> None:
        if ch == self.quote:
            self.quote = None
            self.closed = True
        else:
            self.buf.append(ch)

    def _feed_after_quoted(self, ch: str) -> None:
        if not ch.isspace():
            raise ConfigError(f"invalid config path {self.dotted!r}: {ch!r} after a quoted segment")

    def finish(self) -> list[str]:
        if self.quote:
            raise ConfigError(f"invalid config path {self.dotted!r}: unterminated {self.quote}")
        self.segments.append("".join(self.buf).strip())
        return self.segments


def split_segments(dotted: str) -> list[str]:
    """Split on dots OUTSIDE quotes: ``a."b.c".d`` → ``["a", "b.c", "d"]`` (``'…'`` works too).

    A quote opens a quoted segment only as the segment's first character (spaces aside); anywhere
    else it is an ordinary character, as it was before quoting existed — ``my"ser"ver`` stays one
    literal segment instead of silently swallowing the dots up to a later quote. A quoted segment
    must end at a dot or the end of the path.
    """
    scanner = _SegmentScanner(dotted)
    for ch in dotted:
        scanner.feed(ch)
    return scanner.finish()


def split_path(dotted: str) -> list[str]:
    """Split and normalize a dot path into trimmed segments, rejecting empty segments.

    A segment in quotes may contain dots (``mcp.items."my.server".command``), and everything past
    a flat-key map is one key (``git.config.user.name`` → ``git``, ``config``, ``user.name``).
    Fail-closed: a malformed path is a :class:`ConfigError`, not a silent no-op — a typo in
    ``rig config set`` must abort before it writes a key the schema can't reach.
    """
    if not dotted or not dotted.strip():
        raise ConfigError("empty config path")
    parts = split_segments(dotted)
    if any(p == "" for p in parts):
        # empty segment ("a..b", a leading/trailing dot, a whitespace-only "a. .b", or `a.""`)
        raise ConfigError(f"invalid config path {dotted!r}: empty segment")
    flat = flat_key_index(parts)
    if flat is not None:
        parts = parts[:flat] + [".".join(parts[flat:])]
    return parts


def _render_segment(dotted: str, segment: str, is_flat_key: bool) -> str:
    """One segment in dot-path form: quoted with a quote it does not contain when it needs one."""
    if is_flat_key or not any(c in segment for c in ".\"'"):
        return segment
    q = '"' if '"' not in segment else "'" if "'" not in segment else None
    if q is None:
        raise ConfigError(f"invalid config path {dotted!r}: segment {segment!r} holds both quote characters")
    return f"{q}{segment}{q}"


def canonical_dot_path(dotted: str) -> str:
    """Return the canonical form of a config dot path — one that :func:`split_path` maps back to
    the same segments. A segment holding a dot or a quote is quoted (unless it is a flat-key-map
    key), with whichever quote it does not contain; one holding both quotes has no dot-path form."""
    parts = split_path(dotted)
    flat = flat_key_index(parts)
    return ".".join(_render_segment(dotted, p, i == flat) for i, p in enumerate(parts))


def _missing_segment_error(dotted: str, seg: str, walked: list[str]) -> ConfigError:
    """The shared "the path does not exist" error, naming the segment that broke and where."""
    where = ".".join(walked) or "<root>"
    return ConfigError(f"config path {dotted!r} not found (no {seg!r} under {where})")


def get_path(data: dict[str, Any], dotted: str) -> Any:
    """Read a nested value by dot path. Raises :class:`ConfigError` if the path is absent.

    Traversal fails closed when an intermediate segment is missing OR is a non-mapping
    (``a.b`` where ``a`` is a scalar/list) — both are "the path does not exist", reported
    with the exact segment that broke so the caller can fix the typo.
    """
    node: Any = data
    walked: list[str] = []
    for seg in split_path(dotted):
        if not isinstance(node, dict) or seg not in node:
            raise _missing_segment_error(dotted, seg, walked)
        node = node[seg]
        walked.append(seg)
    return node


def set_path(data: dict[str, Any], dotted: str, value: Any) -> None:
    """Set a nested value by dot path, creating intermediate mappings in place.

    Fail-closed if an existing intermediate segment is a non-mapping: overwriting
    ``ci.items`` (a dict) by setting ``ci.items.x`` is fine, but setting ``a.b`` when ``a``
    is already a scalar would silently clobber it — refuse instead, naming the segment.
    """
    parts = split_path(dotted)
    node = data
    for seg in parts[:-1]:
        if seg not in node:
            node[seg] = {}
        elif not isinstance(node[seg], dict):
            raise ConfigError(f"cannot set {dotted!r}: {seg!r} is a {type(node[seg]).__name__}, not a mapping")
        node = node[seg]
    node[parts[-1]] = value


def del_path(data: dict[str, Any], dotted: str) -> None:
    """Delete a nested value by dot path. Raises :class:`ConfigError` if the path is absent.

    Does not prune empty parent mappings: deleting the last ``env.vars.FOO`` leaves
    ``env.vars: {}``. Fail-closed on a missing segment or a non-mapping intermediate —
    same "the path does not exist" wording as :func:`get_path`, so ``rig env del`` of a
    key that was never set is an error, not a silent no-op.
    """
    parts = split_path(dotted)
    node: Any = data
    walked: list[str] = []
    for seg in parts[:-1]:
        if not isinstance(node, dict) or seg not in node:
            raise _missing_segment_error(dotted, seg, walked)
        node = node[seg]
        walked.append(seg)
    last = parts[-1]
    if not isinstance(node, dict) or last not in node:
        raise _missing_segment_error(dotted, last, walked)
    del node[last]


def _number_or_none(kind: type[int] | type[float], raw: str) -> int | float | None:
    """``kind(raw)``, or ``None`` when it refuses (e.g. CPython's 4300-digit int limit) so
    coercion NEVER raises: the caller falls back to the plain string."""
    try:
        return kind(raw)
    except ValueError:
        return None


def _parse_plain_number(raw: str) -> int | float | None:
    """A plain optionally-signed decimal int or float, else ``None`` (the value stays a string).

    Requires ASCII so ``str.isdigit()``'s Unicode digits (superscripts like '²') — which int()/
    float() then choke on — can't sneak past as a number. Rejects nan/inf/1e3/``_`` extras.
    """
    if not raw.isascii():
        return None
    body = raw[1:] if raw[:1] in ("+", "-") else raw  # allow a single leading sign
    if body.isdigit():
        # A leading zero ("0644", "007") stays a string — it's almost always a file mode /
        # zero-padded id / version the author means literally, and int() would drop the zero.
        return None if len(body) > 1 and body[0] == "0" else _number_or_none(int, raw)
    if body.count(".") == 1 and body.replace(".", "", 1).isdigit():
        return _number_or_none(float, raw)
    return None


def coerce_scalar(raw: str) -> Any:
    """Coerce a CLI string to the obvious scalar type: bool, int, float, null, else string.

    The shell hands every value to us as text; ``tier=block`` must stay a string while
    ``auto_mode=true`` must become a real bool (the schema validators reject a string there).
    Quote-wrap (``'"true"'`` → the string ``true``) forces a literal string for the rare case
    a stringly-typed value collides with a keyword.

    Coercion is deliberately CONSERVATIVE — only the unambiguous forms convert, everything
    else stays a string. So ``int``/``float`` accept a plain optionally-signed number but
    NOT Python's surprising extras (``1_000``, ``nan``, ``inf``, whitespace), which a config
    author would never mean to type and which would otherwise smuggle a NaN/underscore-int
    into the tree.
    """
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]  # explicit string escape: "true" / '12' stay strings
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none", "~"):
        return None
    number = _parse_plain_number(raw)
    return raw if number is None else number
