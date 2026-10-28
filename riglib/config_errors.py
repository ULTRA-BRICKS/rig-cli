"""``ConfigError`` and its renderer, split from ``config.py`` so every config module can import them without a cycle."""

from __future__ import annotations

class ConfigError(ValueError):
    """Raised on a malformed/invalid config (fail-closed before any write).

    A 3-part, renderable error (WHAT / WHY / FIX) that also names the SCHEMA PATH of the
    offending key. The roadmap (§5 "ENFORCED JSON schema") asks every rejection to say what is
    wrong, where (the dotted schema path, e.g. ``harness.auto_mode``), and how to fix it — so a
    malformed config fails LOUDLY, not silently.

    Subclass of :class:`ValueError` for backward-compat: every existing ``except ConfigError``
    (and ``except (ConfigError, …)``) keeps catching it. ``str(err)`` stays the one-line WHAT, so
    legacy ``error: {exc}`` renderers are unchanged; the richer renderer (:func:`render_config_error`)
    shows the why/fix/schema path when present.

    ``schema_path`` is the dotted path into the config tree the JSON schema indexes
    (``defaults.on_conflict``, ``github.ruleset.required_reviews``) — the SAME path an editor would
    show from ``schema/rig.schema.json``. ``why``/``fix`` carry the root cause and a concrete remedy.
    A plain ``ConfigError("msg")`` (no kwargs) still works — why/fix/schema_path default empty.
    """

    def __init__(
        self,
        what: str,
        *,
        why: str = "",
        fix: str = "",
        schema_path: str = "",
    ) -> None:
        super().__init__(what)
        self.what = what
        self.why = why
        self.fix = fix
        self.schema_path = schema_path

    def __str__(self) -> str:
        return self.what


def render_config_error(err: ConfigError, *, color: bool = True) -> str:
    """Render a :class:`ConfigError` as the consistent 3-part block (what / why / fix + path).

    Always shows the WHAT (prefixed ``error:``). Shows the SCHEMA PATH on the ``why`` line and the
    FIX line only when populated, so a terse error doesn't print empty labels. Mirrors
    :func:`riglib.errors.render` (the structured-error renderer) so config errors and catalog/plan
    errors read identically. The path is shown as ``schema/rig.schema.json#/<json-pointer>`` so the
    user can jump straight to the offending node in the published schema.
    """

    def _col(code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if color else s

    lines = [_col("31", f"error: {err.what}")]
    why = err.why
    if err.schema_path:
        from . import config_schema

        # The pointer is resolved through the registry so it stops at an open `items`/`fragments`
        # map (item names aren't schema nodes) and never dangles. A path that can't resolve at all
        # shows the dotted key alone — better than a pointer to a node that doesn't exist.
        pointer = config_schema.schema_pointer_for(err.schema_path)
        if pointer is not None:
            loc = f"schema path: {err.schema_path}  ({config_schema.SCHEMA_REL_PATH}#{pointer})"
        else:
            loc = f"schema path: {err.schema_path}"
        why = f"{why}; {loc}" if why else loc
    if why:
        lines.append(_col("2", "  why: ") + why)
    if err.fix:
        lines.append(_col("32", "  fix: ") + err.fix)
    return "\n".join(lines)
