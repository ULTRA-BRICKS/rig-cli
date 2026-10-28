"""The built-in provider catalog: one registered :class:`ProviderSpec` per provider id.

Importing this module registers everything (``riglib.lint_providers`` does so). The Rig default tools carry
a :class:`BootstrapSpec`; shell/workflow tools and the Rust components carry the ``system`` / ``rustup``
specs that ``bootstrap_needs`` turns into needs only when the binary is actually missing.
"""

from __future__ import annotations

from collections.abc import Callable

from .lint_discovery_model import LANG_GO, LANG_JS, LANG_PY, LANG_RUST, LANG_SHELL, LANG_TS, TRAIT_GH_WORKFLOWS
from .lint_providers_js import (
    plan_biome_format,
    plan_biome_lint,
    plan_eslint,
    plan_oxfmt,
    plan_oxlint,
    plan_prettier,
    plan_tsc,
)
from .lint_providers_model import BootstrapSpec, PlanContext, PlannedCheck, ProviderSpec
from .lint_providers_python import (
    plan_black,
    plan_flake8,
    plan_mypy,
    plan_pylint,
    plan_pyright,
    plan_ruff_check,
    plan_ruff_format,
)
from .lint_providers_registry import register
from .lint_providers_scripts import plan_scripts
from .lint_providers_system import (
    plan_actionlint,
    plan_cargo_fmt,
    plan_clippy,
    plan_go_vet,
    plan_gofmt,
    plan_rig_check,
    plan_shellcheck,
    plan_shfmt,
)
from .lint_run_config import Category

_JS = (LANG_JS, LANG_TS)
Plan = Callable[[PlanContext], tuple[PlannedCheck, ...]]


def _spec(
    provider_id: str, tool: str, category: Category, languages: tuple[str, ...], plan: Plan,
    fix_modes: tuple[str, ...] = (), bootstrap: BootstrapSpec | None = None,
) -> None:
    register(ProviderSpec(provider_id, tool, category, languages, None, bootstrap, fix_modes, plan))


def _register_js() -> None:
    _spec("oxlint", "oxlint", "lint", _JS, plan_oxlint, ("oxlint --fix (safe fixes only)",),
          BootstrapSpec("js", "lint", ("oxlint",)))
    _spec("oxfmt", "oxfmt", "format", _JS, plan_oxfmt, ("oxfmt (rewrites in place)",),
          BootstrapSpec("js", "format", ("oxfmt",)))
    _spec("eslint", "eslint", "lint", _JS, plan_eslint, ("eslint --fix",))
    _spec("biome-lint", "biome", "lint", _JS, plan_biome_lint, ("biome lint --write (never --unsafe)",))
    _spec("biome-format", "biome", "format", _JS, plan_biome_format, ("biome format --write",))
    _spec("prettier", "prettier", "format", _JS, plan_prettier, ("prettier --write",))
    _spec("tsc", "tsc", "typecheck", _JS, plan_tsc)


def _register_python() -> None:
    _spec("ruff-check", "ruff", "lint", (LANG_PY,), plan_ruff_check, ("ruff check --fix (safe fixes only)",),
          BootstrapSpec("python", "lint", ("ruff",)))
    _spec("ruff-format", "ruff", "format", (LANG_PY,), plan_ruff_format, ("ruff format",),
          BootstrapSpec("python", "format", ("ruff",)))
    _spec("flake8", "flake8", "lint", (LANG_PY,), plan_flake8)
    _spec("pylint", "pylint", "lint", (LANG_PY,), plan_pylint)
    _spec("black", "black", "format", (LANG_PY,), plan_black, ("black .",))
    _spec("mypy", "mypy", "typecheck", (LANG_PY,), plan_mypy)
    _spec("pyright", "pyright", "typecheck", (LANG_PY,), plan_pyright)


def _register_system() -> None:
    _spec("shellcheck", "shellcheck", "lint", (LANG_SHELL,), plan_shellcheck, (),
          BootstrapSpec("system", "lint", ()))
    _spec("shfmt", "shfmt", "format", (LANG_SHELL,), plan_shfmt, ("shfmt -w",),
          BootstrapSpec("system", "format", ()))
    _spec("actionlint", "actionlint", "lint", (TRAIT_GH_WORKFLOWS,), plan_actionlint, (),
          BootstrapSpec("system", "lint", ()))
    _spec("cargo-clippy", "cargo", "lint", (LANG_RUST,), plan_clippy, (), BootstrapSpec("rustup", "lint", ()))
    _spec("cargo-fmt", "cargo", "format", (LANG_RUST,), plan_cargo_fmt, ("cargo fmt --all",),
          BootstrapSpec("rustup", "format", ()))
    _spec("go-vet", "go", "lint", (LANG_GO,), plan_go_vet)
    _spec("gofmt", "gofmt", "format", (LANG_GO,), plan_gofmt, ("gofmt -w",))
    _spec("rig-check", "rig", "limits", (), plan_rig_check, ("rig lint check --fix (safe explicit fixes)",))
    _spec("script", "script", "scripts", (), plan_scripts)


def register_catalog() -> None:
    _register_js()
    _register_python()
    _register_system()
