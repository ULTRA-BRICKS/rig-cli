"""Apply-time oxlint finding counts.

Accessed via: rig apply provision_linter_config → preview_oxlint_policy → _run.
Assumptions: a local oxlint binary on PATH or at node_modules/.bin/oxlint; nested
carrier configs must not poison JSON output (agent-tools#633 / rig-cli#433).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from riglib.lint_preview import preview_oxlint_policy


_STUB = r"""#!/usr/bin/env python3
import json
import sys

# Real oxlint 1.82 writes this to stdout (not stderr) when a nested oxlint.config.ts
# contains options.typeAware. Without --disable-nested-config, JSON parse fails.
if "--disable-nested-config" not in sys.argv:
    print(
        "Failed to parse oxlint configuration file.\n\n"
        "  x The `options.typeAware` option is only supported in the root config, "
        "but it was found in linters/oxc/oxlint.config.ts."
    )
    raise SystemExit(1)

print(
    json.dumps(
        {
            "diagnostics": [
                {"severity": "error", "code": "anti-slop(no-chained-type-assertions)"},
                {"severity": "error", "code": "anti-slop(require-safety-comment-for-type-assertion)"},
                {"severity": "warning", "code": "anti-slop(no-unknown-parameters)"},
            ]
        }
    )
)
raise SystemExit(1)
"""


def _repo_with_nested_config_and_stub_oxlint(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    nested = repo / "linters" / "oxc"
    nested.mkdir(parents=True)
    (nested / "oxlint.config.ts").write_text(
        'import { defineConfig } from "oxlint";\n'
        "export default defineConfig({ options: { typeAware: true }, rules: {} });\n"
    )
    (repo / "oxlint.config.ts").write_text(
        'import { defineConfig } from "oxlint";\n'
        "export default defineConfig({ options: { typeAware: true }, rules: {} });\n"
    )
    bindir = repo / "node_modules" / ".bin"
    bindir.mkdir(parents=True)
    oxlint = bindir / "oxlint"
    oxlint.write_text(_STUB)
    oxlint.chmod(oxlint.stat().st_mode | stat.S_IXUSR)
    return repo


def test_preview_counts_findings_when_nested_oxc_carrier_exists(tmp_path: Path):
    repo = _repo_with_nested_config_and_stub_oxlint(tmp_path)
    desired = (repo / "oxlint.config.ts").read_text()
    impact = preview_oxlint_policy(repo, desired, "oxlint.config.ts")
    assert impact.available, impact.detail
    assert impact.current is not None and impact.desired is not None
    assert impact.current.errors == 2
    assert impact.current.warnings == 1
    assert "lint findings:" in impact.render()
    assert "errors 2→2" in impact.render()


_STUB_NESTED_OK = r"""#!/usr/bin/env python3
import json
import sys

# Simulates a monorepo whose package-level oxlint.config files load cleanly.
# Nested discovery adds one extra error; disabling nested configs drops it.
if "--disable-nested-config" in sys.argv:
    diagnostics = [
        {"severity": "error", "code": "anti-slop(no-chained-type-assertions)"},
        {"severity": "warning", "code": "anti-slop(no-unknown-parameters)"},
    ]
else:
    diagnostics = [
        {"severity": "error", "code": "anti-slop(no-chained-type-assertions)"},
        {"severity": "error", "code": "anti-slop(no-object-parameters)"},
        {"severity": "warning", "code": "anti-slop(no-unknown-parameters)"},
    ]
print(json.dumps({"diagnostics": diagnostics}))
raise SystemExit(1)
"""


def test_preview_keeps_intentional_nested_oxlint_configs(tmp_path: Path):
    repo = tmp_path / "mono"
    repo.mkdir()
    (repo / "oxlint.config.ts").write_text("export default {}\n")
    bindir = repo / "node_modules" / ".bin"
    bindir.mkdir(parents=True)
    oxlint = bindir / "oxlint"
    oxlint.write_text(_STUB_NESTED_OK)
    oxlint.chmod(oxlint.stat().st_mode | stat.S_IXUSR)
    impact = preview_oxlint_policy(repo, "export default {}\n", "oxlint.config.ts")
    assert impact.available, impact.detail
    assert impact.current is not None
    assert impact.current.errors == 2
    assert impact.current.warnings == 1
