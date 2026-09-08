"""Hatch-usage analysis for `rig stats show`.

Hermetic fixtures under ``tmp_path/home/.config/agent-tools/{overrides.log,ship-audit.jsonl}``.
Drive ``collect_hatches(home=...)`` and ``run(_ns(home=..., format="json", since="2026-09-08"))``.
Assert observable counts (by hook / by kind), date filtering, missing-file emptiness, and
dedup — not source text, field copies, or mock echoes.
"""

from __future__ import annotations

import json
from pathlib import Path

from riglib.stats.command import parse_date, run
from riglib.stats.hatches import collect_hatches
from riglib.stats.render import tui
from tests.test_stats import _cc_event, write_claude_session


def _ns(**kw):
    import argparse

    base = dict(
        format="tui",
        harness=None,
        repo=None,
        since=None,
        until=None,
        web_port=0,
        baseline=False,
        home=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _agent_tools(home: Path) -> Path:
    d = home / ".config" / "agent-tools"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _overrides(home: Path, rows: list[dict]) -> None:
    _write_jsonl(_agent_tools(home) / "overrides.log", rows)


def _ship_audit(home: Path, rows: list[dict]) -> None:
    _write_jsonl(_agent_tools(home) / "ship-audit.jsonl", rows)


def _override(
    *,
    ts: str,
    hatch: str,
    reason: str = "",
    command: str = "",
    decision: str = "approved",
    detail: str = "",
) -> dict:
    return {
        "ts": ts,
        "session": "s",
        "hatch": hatch,
        "env_var": "RIG_HATCH_REQUEST_X",
        "command": command,
        "reason": reason,
        "decision": decision,
        "detail": detail,
        "cwd": "/tmp",
    }


def test_ship_review_quorum_distinct_models_is_broken_gate(tmp_path):
    home = tmp_path / "home"
    _overrides(
        home,
        [
            _override(
                ts="2026-09-08T08:27:12Z",
                hatch="ship-review-quorum",
                reason="board cannot record 3 PASSED role iterations: distinct_models is 2 of 3",
            )
        ],
    )
    agg = collect_hatches(home=home)
    assert agg.total == 1
    assert agg.by_kind["broken-gate"] == 1
    assert agg.by_hook["ship-review-quorum"]["broken_gate"] == 1
    assert agg.by_hook["ship-review-quorum"]["lazy"] == 0


def test_ship_audit_models_zero_is_broken_gate(tmp_path):
    """Alex's example: review-quorum bypass with models:0 is a broken gate, not laziness."""
    home = tmp_path / "home"
    _ship_audit(
        home,
        [
            {
                "ts": "2026-09-08T08:40:50Z",
                "pr": "637",
                "task_code": "#634",
                "iterations": 0,
                "models": 0,
                "decision": "bypass:approved",
                "override_reason": "approved by tg-ctl ask",
            }
        ],
    )
    agg = collect_hatches(home=home)
    assert agg.total == 1
    assert agg.by_kind["broken-gate"] == 1
    assert agg.by_hook["ship-review-quorum"]["broken_gate"] == 1
    assert agg.by_hook["ship-review-quorum"]["approved"] == 1


def test_visual_proof_gate_is_lazy(tmp_path):
    home = tmp_path / "home"
    _overrides(
        home,
        [
            _override(
                ts="2026-09-08T10:00:00Z",
                hatch="visual-proof-gate",
                reason="skipping the screenshot, shipping anyway",
            )
        ],
    )
    agg = collect_hatches(home=home)
    assert agg.total == 1
    assert agg.by_kind["lazy"] == 1
    assert agg.by_kind.get("broken-gate", 0) == 0
    assert agg.by_hook["visual-proof-gate"]["lazy"] == 1
    assert agg.by_hook["visual-proof-gate"]["broken_gate"] == 0


def test_orchestrator_stays_thin_leaf_worker_is_broken_gate(tmp_path):
    home = tmp_path / "home"
    _overrides(
        home,
        [
            _override(
                ts="2026-09-08T05:30:48Z",
                hatch="orchestrator-stays-thin",
                reason="I am a dispatched subagent / leaf worker",
                command="gh pr comment 321",
            )
        ],
    )
    agg = collect_hatches(home=home)
    assert agg.by_kind["broken-gate"] == 1
    assert agg.by_hook["orchestrator-stays-thin"]["broken_gate"] == 1
    assert agg.by_hook["orchestrator-stays-thin"]["lazy"] == 0


def test_grouping_two_hooks_by_hook_and_kind(tmp_path):
    home = tmp_path / "home"
    _overrides(
        home,
        [
            _override(
                ts="2026-09-08T08:27:12Z",
                hatch="ship-review-quorum",
                reason="weekly quota exhausted, 2 of 3 seats empty",
            ),
            _override(
                ts="2026-09-08T10:00:00Z",
                hatch="visual-proof-gate",
                reason="no screenshot this time",
            ),
            _override(
                ts="2026-09-08T10:05:00Z",
                hatch="visual-proof-gate",
                reason="still no screenshot",
            ),
        ],
    )
    agg = collect_hatches(home=home)
    assert agg.total == 3
    assert agg.by_kind["broken-gate"] == 1
    assert agg.by_kind["lazy"] == 2
    assert agg.by_hook["ship-review-quorum"]["total"] == 1
    assert agg.by_hook["visual-proof-gate"]["total"] == 2
    # count-desc: the hook with 2 events comes first
    assert list(agg.by_hook)[0] == "visual-proof-gate"


def test_since_until_drops_out_of_window(tmp_path):
    home = tmp_path / "home"
    _overrides(
        home,
        [
            _override(ts="2026-09-07T23:00:00Z", hatch="visual-proof-gate", reason="before"),
            _override(ts="2026-09-08T12:00:00Z", hatch="visual-proof-gate", reason="inside"),
            _override(ts="2026-09-09T00:00:01Z", hatch="visual-proof-gate", reason="after"),
        ],
    )
    agg = collect_hatches(
        home=home,
        since=parse_date("2026-09-08"),
        until=parse_date("2026-09-08", end=True),
    )
    assert agg.total == 1
    assert agg.by_hook["visual-proof-gate"]["total"] == 1


def test_missing_both_files_total_zero(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    agg = collect_hatches(home=home)
    assert agg.total == 0
    assert agg.by_kind.get("broken-gate", 0) == 0
    assert agg.by_kind.get("lazy", 0) == 0


def test_dedup_same_ts_hook_counts_once(tmp_path):
    """Same ts+hook in both files counts once; overrides.log wins (richer reason)."""
    home = tmp_path / "home"
    ts = "2026-09-08T08:40:50Z"
    _overrides(
        home,
        [
            _override(
                ts=ts,
                hatch="ship-review-quorum",
                reason="distinct_models is 2 of 3 so the board cannot pass",
            )
        ],
    )
    _ship_audit(
        home,
        [
            {
                "ts": ts,
                "pr": "637",
                "task_code": "#634",
                "iterations": 3,
                "models": 3,
                "decision": "bypass:approved",
                "override_reason": "approved by tg-ctl ask",
            }
        ],
    )
    agg = collect_hatches(home=home)
    assert agg.total == 1
    # ship-audit alone (models=3, no broken-gate phrase) would be lazy; overrides wins.
    assert agg.by_kind["broken-gate"] == 1
    assert agg.by_hook["ship-review-quorum"]["broken_gate"] == 1


def test_run_json_includes_hatches_grouped(tmp_path, capsys):
    home = tmp_path / "home"
    _overrides(
        home,
        [
            _override(
                ts="2026-09-08T08:27:12Z",
                hatch="ship-review-quorum",
                reason="quota exhausted, 0/3 roles",
            ),
            _override(
                ts="2026-09-08T10:00:00Z",
                hatch="visual-proof-gate",
                reason="skipping screenshots",
            ),
        ],
    )
    rc = run(_ns(home=str(home), format="json", since="2026-09-08"))
    assert rc == 0
    d = json.loads(capsys.readouterr().out)
    hatches = d["hatches"]
    assert hatches["total"] == 2
    assert hatches["by_kind"]["broken-gate"] == 1
    assert hatches["by_kind"]["lazy"] == 1
    assert hatches["by_hook"]["ship-review-quorum"]["broken_gate"] == 1
    assert hatches["by_hook"]["visual-proof-gate"]["lazy"] == 1


def test_plain_tui_contains_escape_hatches_and_hook_id(tmp_path, capsys):
    home = tmp_path / "home"
    _overrides(
        home,
        [
            _override(
                ts="2026-09-08T08:27:12Z",
                hatch="ship-review-quorum",
                reason="weekly limit hit, 2 of 3 seats",
            )
        ],
    )
    write_claude_session(
        home,
        "-tmp",
        "s",
        "/tmp/x",
        [_cc_event("2026-09-08T12:00:00Z", "/tmp/x", [{"name": "Read", "input": {"file_path": "/tmp/x"}}])],
    )
    rc = run(_ns(home=str(home), format="tui", since="2026-09-08"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Escape hatches" in out
    assert "ship-review-quorum" in out

    # plain fallback (no rich) still names the section and the hook
    from riglib.stats.aggregate import aggregate

    plain = tui.render_plain(aggregate([]), hatches=collect_hatches(home=home))
    assert "Escape hatches" in plain
    assert "ship-review-quorum" in plain


def test_empty_hatches_prints_honest_zero(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    from riglib.stats.aggregate import aggregate

    plain = tui.render_plain(aggregate([]), hatches=collect_hatches(home=home))
    assert "Escape hatches: 0" in plain


def test_tui_renders_hatches_when_harness_logs_absent(tmp_path, capsys):
    """TUI still prints Escape hatches when only overrides.log exists (no session logs)."""
    home = tmp_path / "home"
    _overrides(
        home,
        [
            _override(
                ts="2026-09-08T08:27:12Z",
                hatch="visual-proof-gate",
                reason="skipping screenshots",
            )
        ],
    )
    rc = run(_ns(format="tui", home=str(home)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Escape hatches" in out
    assert "visual-proof-gate" in out


def test_overrides_same_ts_hook_are_not_deduped(tmp_path):
    """Dedup is only across sources. Two overrides.log rows with the same ts+hook both count;
    a matching ship-audit row is skipped, not added as a third."""
    home = tmp_path / "home"
    ts = "2026-09-08T08:40:50Z"
    _overrides(
        home,
        [
            _override(ts=ts, hatch="ship-review-quorum", reason="first"),
            _override(ts=ts, hatch="ship-review-quorum", reason="second"),
        ],
    )
    agg = collect_hatches(home=home)
    assert agg.total == 2
    _ship_audit(
        home,
        [
            {
                "ts": ts,
                "pr": "637",
                "task_code": "#634",
                "iterations": 3,
                "models": 0,
                "decision": "bypass:approved",
                "override_reason": "same ts+hook from ship-audit",
            }
        ],
    )
    agg = collect_hatches(home=home)
    assert agg.total == 2
