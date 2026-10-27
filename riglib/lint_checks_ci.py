"""Where the rendered ``rig-lint-check.yml`` workflow installs ``rig`` from (``lint_checks.ci.rig_source``).

Why this module exists
----------------------
The CI workflow has to ``pip install`` a ``rig`` that has ``rig lint check``. ``hyper-rig==<version>``
on PyPI is not that source (only an old release is published, and rig's own dev version is never
uploaded), and rig-cli publishes no release tags, so there is NO immutable reference rig can pin
on its own: the commit that carries this feature does not exist on the forge until it is merged.
Rendering a pin that does not resolve would hand every repo a permanently red job.

So the source is explicit configuration, and the workflow is only rendered when it is set::

    lint_checks:
      ci:
        rig_source: git+https://git.hyperide.ai/ultrabricks/rig-cli@<40-hex commit sha>

Only an IMMUTABLE form is accepted — ``git+https://<host>/<path>@<full lowercase commit sha>`` — so a
branch, a tag, a PyPI range or a plain URL (all mutable) can never become the CI gate's rig. The
value is also restricted to a charset that is safe to embed in the workflow's ``run:`` step. The
rendered bytes depend on this setting alone, never on the rig version that rendered them, so a
rig upgrade does not churn every repo's workflow.

Stdlib-only at import time apart from :class:`riglib.config.ConfigError` (imported lazily).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

KEY = "rig_source"
EXAMPLE = "git+https://git.hyperide.ai/ultrabricks/rig-cli@0123456789abcdef0123456789abcdef01234567"
# A canonical decimal TCP port (1..65535), shared by runtime and emitted JSON schema.
_PORT = r"(?:[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])"
_BODY = rf"git\+https://[a-z0-9][a-z0-9.-]*(?::{_PORT})?/[A-Za-z0-9._~/-]+@[0-9a-f]{{40}}"
SCHEMA_PATTERN = f"^(?!.*\\.\\.){_BODY}$"  # JSON-schema form of the same shape, incl. the no-`..` rule (``\Z`` is Python-only)
_SOURCE_RE = re.compile(rf"\A{_BODY}\Z")
_CI_KEYS = (KEY,)


def is_immutable_source(value: object) -> bool:
    """True for ``git+https://host/path@<40 lowercase hex>`` (no ``..`` segment); anything else is rejected."""
    return isinstance(value, str) and _SOURCE_RE.match(value) is not None and ".." not in value


def validate(block: Any, *, layer: str) -> None:
    """Validate one layer's raw ``lint_checks.ci`` block; raises :class:`riglib.config.ConfigError`."""
    from .config import ConfigError

    path = "lint_checks.ci"
    if not isinstance(block, Mapping):
        raise ConfigError(
            f"{path} must be a mapping, got {block!r} (layer: {layer})",
            fix=f"write {path} as a YAML mapping, e.g. {KEY}: {EXAMPLE}", schema_path=path,
        )
    unknown = sorted(str(k) for k in block if k not in _CI_KEYS)
    if unknown:
        raise ConfigError(
            f"unknown {path} key: {', '.join(unknown)} (layer: {layer})",
            why=f"{', '.join(unknown)} is not a known {path} key",
            fix=f"remove it, or use one of: {', '.join(_CI_KEYS)}", schema_path=f"{path}.{unknown[0]}",
        )
    if KEY in block and not is_immutable_source(block[KEY]):
        raise ConfigError(
            f"{path}.{KEY} must be an immutable `git+https://<host>/<path>@<40-hex commit sha>`, got {block[KEY]!r} (layer: {layer})",
            why="a branch, tag, PyPI spec or bare URL can change under the CI gate that installs rig from it",
            fix=f"pin a full commit sha, e.g. {EXAMPLE}", schema_path=f"{path}.{KEY}",
        )


def rig_source(data: Mapping[str, Any]) -> str | None:
    """The configured install source from the CASCADED config (repo wins), or None when not configured."""
    block = data.get("lint_checks")
    ci = block.get("ci") if isinstance(block, Mapping) else None
    value = ci.get(KEY) if isinstance(ci, Mapping) else None
    return value if is_immutable_source(value) else None
