"""The ``harness_wrappers`` block: settings, and the per-kind wrapper state apply/status/verify share.

What this is
------------
``rig apply commit`` provisions the opt-in PATH wrappers of ``rig harness install-wrapper`` (see
:mod:`riglib.harness_launcher`) for every harness kind the GLOBAL config asks for. The flow is the
usual reconciler chain:

* :func:`riglib.plan._build_harness_wrappers` -> ONE ``harness_wrapper`` action per kind;
* :func:`riglib.actions.runner._do_harness_wrapper` -> ``harness_launcher.install_wrapper``;
* :func:`riglib.drift._check_harness_wrapper` (``rig status``) and
  :func:`riglib.verify._verify_harness_wrapper` (post-apply) read the SAME :func:`resolve` state, so
  apply, status and verify cannot disagree about what "installed" means.

This module holds only what those callers share: the pure settings parse and the read-only state
resolver. The wrapper text, the atomic write and the real-binary record stay in ``harness_launcher``
(one writer); this module never writes.

Nothing here mutates ``PATH``: the wrapper directory must be put BEFORE the real binaries by the
user. Only interactive launches are hosted by the wrappers; everything else execs the real binary.

Stdlib-only at import time (the AGENTS.md hard rule); ``harness_launcher`` is imported lazily.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .harness_kinds import KINDS

DEFAULT_DIR = "~/.local/share/rig/bin"
#: kinds launched inside a Landlock signal domain when ``signal_scope`` is not configured (#704)
DEFAULT_SIGNAL_SCOPE: tuple[str, ...] = ("omp",)


# A path in the block is machine-wide, so it must not depend on where rig runs: absolute, ``~`` or
# ``$VAR`` (the same spellings the other global path keys take). Checked by the validator on the raw
# string and again on the expanded one by :func:`resolve`, because an unset ``$VAR`` expands to a
# relative path.
ABSOLUTE_PATH_STARTS = ("/", "~", "$")


@dataclass(frozen=True)
class Settings:
    enabled: bool
    dir: str
    kinds: tuple[str, ...]
    real_bin: dict[str, str]
    signal_scope: tuple[str, ...] = DEFAULT_SIGNAL_SCOPE


def settings_from_config(block: Any) -> Settings:
    """Parse the (already validated) ``harness_wrappers`` block. Opt-in: only ``enabled: true`` acts.

    ``kinds`` is returned in canonical :data:`~riglib.harness_kinds.KINDS` order with duplicates
    collapsed, so a re-ordered config yields an identical plan.
    """
    block = block if isinstance(block, dict) else {}
    configured = block.get("kinds")
    wanted = set(KINDS if configured is None else configured)
    real_bin = block.get("real_bin") or {}
    scope = block.get("signal_scope")
    scoped = set(DEFAULT_SIGNAL_SCOPE if not isinstance(scope, list) else scope)
    return Settings(
        enabled=block.get("enabled") is True,
        dir=block.get("dir") or DEFAULT_DIR,
        kinds=tuple(kind for kind in KINDS if kind in wanted),
        real_bin={kind: path for kind, path in real_bin.items() if kind in wanted},
        signal_scope=tuple(kind for kind in KINDS if kind in scoped),
    )


def signal_scope_enabled(
    kind: str, env: Mapping[str, str], *, platform: str = sys.platform, config_path: Path | None = None
) -> bool:
    """Whether a rig launch of ``kind`` goes through :mod:`riglib.signal_scope` (#704).

    Read straight from the GLOBAL config file (the key is GLOBAL-only) on every launch, and
    independent of ``enabled``. A missing, unreadable or malformed file must neither block the
    launch nor silently drop protection, so any read or shape error falls back to the default.
    """
    if not platform.startswith("linux") or env.get("RIG_SIGNAL_SCOPE") == "0":
        return False
    from .config import ConfigError, global_config_path, read_yaml_file

    try:
        data = read_yaml_file(config_path or global_config_path())
    except (ConfigError, OSError):
        data = {}
    block = data.get("harness_wrappers")
    if not isinstance(block, dict):
        block = {}
    scope = block.get("signal_scope")
    # Anything the validator would reject (not a list, a non-string, an unknown kind) reads as the
    # default: filtering out a typo would otherwise silently turn protection off. Only this key is
    # read, so a malformed unrelated key (kinds, real_bin) can never break a launch.
    if not isinstance(scope, list) or not all(isinstance(k, str) and k in KINDS for k in scope):
        scope = list(DEFAULT_SIGNAL_SCOPE)
    return kind in scope


@dataclass(frozen=True)
class WrapperState:
    """What is on disk for one kind, against what the action asks for. Read-only."""

    kind: str
    target: Path
    #: ``missing`` | ``current`` | ``stale`` (rig's, different text) | ``foreign`` (not rig's) |
    #: ``relative`` (the target dir is not absolute, nothing can be judged or written)
    wrapper: str
    #: the configured real binary as given (expanded, not resolved); ``None`` = not configured
    real_bin: str | None = None
    #: why the configured real binary cannot be recorded (``None`` = fine or not configured)
    real_bin_invalid: str | None = None
    #: what ``harness-real-bin.json`` holds for this kind, and what the config wants it to hold
    recorded: str | None = None
    wanted: str | None = None

    @property
    def real_bin_current(self) -> bool:
        return self.real_bin is None or (self.real_bin_invalid is None and self.recorded == self.wanted)

    @property
    def current(self) -> bool:
        return self.wrapper == "current" and self.real_bin_current


def resolve(action: Any) -> WrapperState:
    """The state of ONE ``harness_wrapper`` action's target (read-only; the apply/status/verify seam)."""
    from . import harness_launcher

    kind = action.options["kind"]
    target = Path(action.target)
    real_bin = action.options.get("real_bin")
    invalid = wanted = recorded = None
    if real_bin is not None:
        real = Path(real_bin)
        if real.is_absolute() and real.exists():
            wanted = str(real.resolve())
            recorded = harness_launcher.read_real_bins(harness_launcher.real_bin_config_path()).get(kind)
        else:
            invalid = f"real binary for {kind} must be an existing absolute path, got {real_bin}"
    wrapper = "relative" if not target.is_absolute() else harness_launcher.wrapper_state(kind, target)
    return WrapperState(kind, target, wrapper, real_bin, invalid, recorded, wanted)


def relative_detail(state: WrapperState) -> str:
    return (
        f"harness_wrappers.dir resolves to {state.target.parent}, which is not absolute "
        "(is the variable it names set?)"
    )


def foreign_detail(state: WrapperState) -> str:
    what = "a symlink" if state.target.is_symlink() else "a file"
    return f"{what} at {state.target} was not written by rig"


def real_bin_detail(state: WrapperState) -> str:
    """Why the recorded real binary disagrees with config (only meaningful when it does)."""
    if state.real_bin_invalid:
        return state.real_bin_invalid
    return (
        f"recorded real {state.kind} binary is {state.recorded or 'unset'}, config wants {state.wanted} "
        "(`rig apply commit` records it)"
    )
