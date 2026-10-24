"""Opt-in real-agent E2E: a guard block must lead a real omp agent to self-serve (rig-cli#756).

Needs docker AND an agent credential (ANTHROPIC_OAUTH_TOKEN / ANTHROPIC_API_KEY, or a logged-in
host omp), costs a few cents of a cheap model per trial, and takes minutes — so it is OFF in the
default `pytest -q`. Enable with RIG_OMP_TICKET_E2E=1. The same matrix with N trials per cell and
the baseline-vs-fixed table is `python3 tests/omp_ticket_e2e/run_matrix.py` (see its README).

What it asserts (the FIXED variant only — the baseline is evidence, not a gate): in every scenario
the real agents produce the guard-intended behaviour in at least RIG_OMP_TICKET_E2E_MIN of
RIG_OMP_TICKET_E2E_N trials (default 3 of 3 for A/B/C/D; LLM variance is the reason this is not a
required PR check).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

E2E_DIR = Path(__file__).resolve().parent / "omp_ticket_e2e"
RUN_MATRIX = E2E_DIR / "run_matrix.py"

_OPTED_IN = os.environ.get("RIG_OMP_TICKET_E2E", "").strip().lower() in ("1", "true", "yes")
pytestmark = pytest.mark.skipif(
    not _OPTED_IN or shutil.which("docker") is None,
    reason="opt-in real-agent E2E: set RIG_OMP_TICKET_E2E=1 (needs docker and an agent credential)",
)


@pytest.mark.parametrize("scenario", ["A", "B", "C", "D"])
def test_fixed_guard_leads_real_agents_to_a_ticketed_commit(scenario: str, tmp_path: Path) -> None:
    trials = int(os.environ.get("RIG_OMP_TICKET_E2E_N", "3"))
    need = int(os.environ.get("RIG_OMP_TICKET_E2E_MIN", str(trials)))
    out = tmp_path / "run"
    proc = subprocess.run(
        [sys.executable, str(RUN_MATRIX), "-n", str(trials), "--scenarios", scenario,
         "--variants", "fixed", "--out", str(out)],
        capture_output=True, text=True, timeout=trials * 900 + 1800,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    results = json.loads((out / "results.json").read_text())["results"]
    wins = sum(1 for r in results if r["success"])
    assert wins >= need, f"scenario {scenario}: {wins}/{len(results)} trials behaved as intended\n" + (
        out / "results.md").read_text()
