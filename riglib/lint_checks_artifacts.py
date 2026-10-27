"""The two files rig renders for the ``lint_checks`` activation: the git-hook fragment and the CI workflow.

Why this module exists
----------------------
``rig lint check`` (:mod:`riglib.lint_check`) only gates anything once it runs on every commit and on
every PR. Both activation surfaces are rig-OWNED rendered files rather than agent-tools catalog
content (the catalog has no ``lint-check`` hook fragment or CI slot; see
``docs/specs/2026-10-26-cccc-lint-check.md`` for the upstream follow-up). Plan
(:func:`riglib.plan._build_lint_checks`), apply (:mod:`riglib.actions.lint_checks`) and drift
(:func:`riglib.drift._check_lint_check_hook`) all call the SAME renderers here, so what ``rig status``
compares is byte-for-byte what ``rig apply`` writes.

Stdlib-only; no I/O.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import cccc_install, lint_checks_ci, lint_checks_config
from .paths import expand_path

# Dispatcher drop-in: ``<dispatcher dir>/<event>/<name>``. Sorted after the catalog's 10-/20- fragments.
HOOK_EVENT = "pre-commit"
HOOK_NAME = "30-lint-check"
# The capability id the dispatcher matches for ``hooks.skipGlobal`` opt-outs.
HOOK_ID = "lint-check"

DEFAULT_DISPATCHER_DIR = "~/.config/git/global-hooks.d"

WORKFLOW_REL = ".github/workflows/rig-lint-check.yml"

# actions/checkout v4.2.2 — the same immutable commit the rest of rig's workflows pin.
_CHECKOUT_SHA = "11bd71901bbe5b1630ceea73d27597364c9af683"


def render_hook_fragment(rig_bin: str) -> str:
    """The pre-commit fragment: ``rig lint check --staged --json`` via the absolute rig path pinned at plan time.

    Fail closed: a missing ``rig`` blocks the commit (a silently skipped gate is the failure mode this
    exists to prevent) and every non-zero exit of the check — findings (1), config error (2),
    incomplete analysis (3), missing/mismatched analyzer (127) — blocks.

    ``global-hook-protected: true`` makes the dispatcher ignore the opt-out sources a CLONED repo can
    forge: a tracked ``.githooks-skip`` line and the ``GLOBAL_HOOKS_SKIP`` env var (a repo's lefthook
    ``env:`` block can inject it). Only the user's own ``git config hooks.skipGlobal`` still opts out.
    """
    rig = shlex.quote(rig_bin)
    return f"""#!/bin/sh
# global-hook-id: {HOOK_ID}
# global-hook-protected: true
# Managed by Rig. Class: generated. Source of truth: global Rig config (lint_checks) + rig.yaml.
# Do not edit: `rig apply` rewrites this file. Opt a repo out with `git config hooks.skipGlobal {HOOK_ID}`
# (a tracked .githooks-skip or GLOBAL_HOOKS_SKIP cannot: the gate is protected) or
# `lint_checks.enabled: false` in its rig.yaml (the check then reports "disabled" and passes).
#
# Inspects the STAGED (index) content only; any non-zero exit blocks the commit.
RIG={rig}
if ! command -v "$RIG" >/dev/null 2>&1; then
  echo "lint-check: rig not found at $RIG - blocking the commit (fail closed). Reinstall rig or run: rig apply commit" >&2
  exit 1
fi
"$RIG" lint check --staged --json >&2
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "lint-check: commit blocked (exit $rc: 1 findings, 2 config error, 3 incomplete analysis, 127 analyzer missing - run: rig apply commit)" >&2
fi
exit "$rc"
"""


def _analyzer_step() -> str:
    pin = cccc_install.pin_for(("linux", "x86_64"))
    version = cccc_install.PINNED_VERSION
    return f"""      - name: Install pinned cccc v{version} (digest-verified)
        run: |
          set -euo pipefail
          tmp="$(mktemp -d)"
          curl -fsSL --retry 3 -o "$tmp/cccc.tar.gz" {pin.url}
          echo "{pin.tarball_sha256}  $tmp/cccc.tar.gz" | sha256sum -c -
          tar -xzf "$tmp/cccc.tar.gz" -C "$tmp" {cccc_install.BINARY_MEMBER}
          echo "{pin.binary_sha256}  $tmp/cccc" | sha256sum -c -
          # the same lookup rig itself uses: ${{XDG_DATA_HOME:-~/.local/share}}/rig/cccc/v<version>/cccc
          dest="${{XDG_DATA_HOME:-$HOME/.local/share}}/rig/cccc/v{version}"
          mkdir -p "$dest"
          install -m 0755 "$tmp/cccc" "$dest/cccc"
"""


def render_workflow(rig_source: str, with_analyzer: bool) -> str:
    """The CI workflow: whole-repo ``rig lint check --json`` with the SHA-pinned analyzer and a pinned rig.

    ``rig_source`` is the validated ``lint_checks.ci.rig_source`` (``git+https://…@<commit sha>``,
    see :mod:`riglib.lint_checks_ci`): the bytes depend on it alone, never on the rig version that
    rendered them. ``with_analyzer`` is false when ``lint_checks.complexity.enabled`` is false (the
    file-line check needs no analyzer, so none is downloaded).

    Security shape: ``pull_request`` (never ``pull_request_target``), read-only token, no credentials
    persisted by checkout, the analyzer verified against BOTH pinned digests before it is placed where
    ``rig`` looks for it, and rig itself installed from an immutable commit.
    """
    if not lint_checks_ci.is_immutable_source(rig_source):
        raise ValueError(f"refusing to render the CI workflow with a non-immutable rig source {rig_source!r}")
    analyzer = _analyzer_step() if with_analyzer else ""
    return f"""# Managed by Rig. Class: generated. Source of truth: global Rig config (lint_checks) + rig.yaml.
# Do not edit directly: `rig apply` reconciles the whole file (rig is installed from lint_checks.ci.rig_source).
name: rig-lint-check

on:
  pull_request:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  lint-check:
    name: lint-check
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@{_CHECKOUT_SHA} # v4.2.2
        with:
          persist-credentials: false
{analyzer}      - name: Install rig from the pinned commit
        run: |
          set -euo pipefail
          python3 -m venv "$RUNNER_TEMP/rig-venv"
          "$RUNNER_TEMP/rig-venv/bin/pip" install --quiet "{rig_source}"
          echo "$RUNNER_TEMP/rig-venv/bin" >> "$GITHUB_PATH"
      - name: rig lint check
        run: rig lint check --json
"""


def hook_fragment_disabled(disp_cfg: Any) -> bool:
    """``git_hooks.dispatcher.fragments.lint-check.enabled: false`` — the one explicit per-fragment opt-out."""
    fragments = disp_cfg.get("fragments", {}) if isinstance(disp_cfg, dict) else {}
    fragment_cfg = fragments.get(HOOK_ID) if isinstance(fragments, dict) else None
    return isinstance(fragment_cfg, dict) and fragment_cfg.get("enabled") is False


def resolve_dispatcher_dir(disp_cfg: Mapping[str, Any], repo_root: Path) -> Path:
    """The dispatcher directory plan and drift both look in (config ``dir`` or the XDG-aware default)."""
    return expand_path(str(disp_cfg.get("dir", DEFAULT_DISPATCHER_DIR)), repo_root)


def hook_fragment_path(dispatcher_dir: Path) -> Path:
    return dispatcher_dir / HOOK_EVENT / HOOK_NAME


@dataclass(frozen=True)
class LintChecksIntent:
    """Who needs what: the hook follows the GLOBAL switch, the analyzer any layer that runs complexity.

    The analyzer and the pre-commit hook live in one place per machine and serve every repo, so a
    single repo's ``rig.yaml`` must not decide whether the HOOK exists: ``repo.enabled: false`` or
    ``repo.complexity_enabled: false`` only changes what ``rig lint check`` and the workflow do in
    THAT repo, and only the global layer's ``enabled: false`` (or the fragment switch) removes the hook.

    ``install_analyzer``: the pinned analyzer is per machine and harmless, so it is installed whenever
    ANY layer will run complexity: the global layer (enabled + complexity) or the repo being applied
    (enabled + complexity). A repo that enables the check against a global opt-out therefore gets the
    analyzer its own ``rig lint check`` will demand, from the ``rig apply commit`` the 127 message names.

    ``needs_analyzer`` (the hook's gate): the hook exists (global switch on) and the analyzer is
    installed, so the hook is written only once that analyzer verified. A repo that enables complexity
    against a global opt-out, without having applied itself, gets exit 127 with the ``rig apply commit``
    fix message until its own apply installs the analyzer (loud, never a silent pass).

    ``workflow_with_analyzer``: CI never reads the global config, it sees built-in defaults + the repo
    layer. The analyzer step is omitted only when the REPO itself turned complexity off (merged
    complexity is off while the global layer's own is on); a global-only opt-out is invisible to CI,
    where complexity runs by default, so the step must stay.
    """

    repo: lint_checks_config.LintChecksConfig
    machine_enabled: bool
    install_analyzer: bool
    needs_analyzer: bool
    workflow_with_analyzer: bool


def lint_checks_intent(loaded: Any) -> LintChecksIntent:
    """The ONE derivation plan and drift share (``loaded``: ``.data`` merged, ``.global_data`` global layer only).

    Only two booleans are read from the global block (already validated at load): resolving it alone
    would apply the cross-key limit check to a layer that a repo may legitimately complete.
    """
    repo = lint_checks_config.resolve(loaded.data)
    defaults = lint_checks_config.DEFAULTS
    block = loaded.global_data.get("lint_checks") or {}
    machine_enabled = block.get("enabled", defaults.enabled)
    machine_complexity = block.get("complexity", {}).get("enabled", defaults.complexity_enabled)
    machine_runs = machine_enabled and machine_complexity
    repo_runs = repo.enabled and repo.complexity_enabled
    install_analyzer = machine_runs or repo_runs
    needs_analyzer = machine_enabled and install_analyzer
    workflow_with_analyzer = repo.complexity_enabled or not machine_complexity
    return LintChecksIntent(repo, machine_enabled, install_analyzer, needs_analyzer, workflow_with_analyzer)
