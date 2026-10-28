"""Regex patterns and shared enums the published JSON Schema pins, kept apart from the block data.

Stdlib-only at import time (the AGENTS.md hard rule).
"""

from __future__ import annotations

from .harness_skills import (
    HARNESS_INSTRUCTION_FILES,
    HARNESS_NATIVE_SKILLS,
    HARNESS_SKILL_DIR_KINDS,
)

# Every harness kind rig provisions skill/instruction discovery for, listed skills-dir kinds first
# (claude-code, codex) then native-discovery (opencode, omp) then instruction-file kinds (pi,
# commandcode) — a stable, readable order for the published JSON-schema enum. Sourced from
# :mod:`riglib.harness_skills` so the schema enum can never drift from what ``config.validate``
# accepts.
_HARNESS_KIND_ENUM: tuple[str, ...] = tuple(
    dict.fromkeys((*HARNESS_SKILL_DIR_KINDS, *HARNESS_NATIVE_SKILLS, *HARNESS_INSTRUCTION_FILES))
)

PERMISSION_RULE_JSON_PATTERN = r"^(?!.*[\r\n])[A-Za-z0-9_*.-]+(\(.+\))?$"
# Mirrors ``riglib.config``'s ``_validate_gitignore`` entry check EXACTLY: non-empty after
# stripping, and no line separator ``str.splitlines()`` recognizes (LF/CR plus VT/FF/FS/GS/RS/
# NEL/LINE SEPARATOR/PARAGRAPH SEPARATOR) — so a config that validates offline against the
# published schema never surprises `rig apply` with a rejection the schema didn't warn about.
GITIGNORE_ENTRY_JSON_PATTERN = r"^(?!\s*$)(?!.*[\r\n\x0b\x0c\x1c-\x1e\x85\u2028\u2029]).+$"
# Mirrors riglib.tmux's `_pane_titles_format_is_safe` char/substring blocklist EXACTLY (a test —
# `test_pane_titles_format_python_check_matches_json_schema_pattern` — runs a shared corpus
# through both and asserts identical verdicts, so the two can't independently drift): `"`, `\`,
# `$`, `#(` (tmux's shell-exec format token), true Unicode-category-Cc control characters (ASCII
# 0x00-0x1F/0x7F + the C1 range 0x80-0x9F) except a bare tab, and U+2028/U+2029
# (LINE/PARAGRAPH SEPARATOR). Deliberately narrower than "any non-printable character" — that
# would also reject ordinary Powerline/Nerd-Font glyphs (category Co/Cf) that are common in a
# real pane-border-format and can't corrupt a double-quoted tmux value. Kept as a regex here
# (rather than importing tmux.py's Python check) so the published JSON schema can express the
# SAME rule for editors/CI validating rig.yaml offline, without a runtime import.
TMUX_PANE_TITLES_FORMAT_UNSAFE_PATTERN = r'["\\$]|#\(|[\x00-\x08\x0a-\x1f\x7f-\x9f  ]'
