"""Tests for TypeScript type-escape-hatch scanning (`riglib.stats.type_hatches`).

Distinct from `test_stats_hatches.py`, which covers agent-hook GATE BYPASSES. This
module's "hatch" is unsafe TypeScript typing per the `no-type-escape-hatches` convention
(`any`, `as any`, `as unknown as`, `@ts-ignore`/`@ts-expect-error`, loose
`Record<string, unknown>`).
"""

from __future__ import annotations

from pathlib import Path

from riglib.stats import build_report
from riglib.stats.render import json_out, tui
from riglib.stats.type_hatches import scan_type_hatches

from tests.test_stats import _cc_event, write_claude_session


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fixture_repo(tmp_path: Path) -> Path:
    """A tiny TS repo with a handful of real escape hatches plus files that must NOT count."""
    repo = tmp_path / "repo"
    _write(
        repo / "src" / "api.ts",
        "\n".join(
            [
                "export function parse(raw: string) {",
                "  const config = JSON.parse(raw) as unknown as Config;",
                "  const x = (config as any).maybeMissing;",
                "  // @ts-ignore next line is fine, trust me",
                "  return legacyCall(x);",
                "}",
                "",
                "export function handler(event: any): void {",
                "  const cache: Record<string, unknown> = {};",
                "}",
            ]
        ),
    )
    _write(
        repo / "src" / "clean.ts",
        "\n".join(
            [
                "export interface User { id: string; name: string; }",
                "export function greet(u: User): string { return `hi ${u.name}`; }",
            ]
        ),
    )
    # excluded: type declarations and vendored/generated trees
    _write(repo / "src" / "vendor.d.ts", "declare const globalThing: any;\n")
    _write(repo / "node_modules" / "pkg" / "index.ts", "export const x: any = 1;\n")
    _write(repo / "dist" / "bundle.ts", "export const y: any = 1;\n")
    return repo


def test_scan_type_hatches_counts_each_kind(tmp_path):
    repo = _fixture_repo(tmp_path)
    agg = scan_type_hatches([str(repo)])
    assert agg.total == 5
    assert agg.by_kind == {
        "as-any": 1,
        "as-unknown-as": 1,
        "bare-any": 1,
        "ts-ignore": 1,
        "loose-record": 1,
    }
    assert agg.by_repo == {str(repo): 5}
    # api.ts + clean.ts only — vendor.d.ts / node_modules / dist excluded
    assert agg.files_scanned == 2


def test_scan_type_hatches_does_not_double_count_overlapping_patterns(tmp_path):
    """`as any[]` matches BOTH `as-any` (`as any`) and `bare-any` (`any[]`) as
    independently-defined regexes, and `as unknown as any` matches both `as-unknown-as`
    and `as-any` on the trailing `as any`. Each is one real hatch and must count once."""
    repo = tmp_path / "overlap-repo"
    _write(
        repo / "src" / "overlap.ts",
        "\n".join(
            [
                "export const arr = data as any[];",
                "export const y = foo as unknown as any;",
            ]
        ),
    )
    agg = scan_type_hatches([str(repo)])
    assert agg.total == 2
    assert agg.by_kind == {"as-any": 1, "as-unknown-as": 1}


def test_scan_type_hatches_skips_missing_repo(tmp_path):
    agg = scan_type_hatches([str(tmp_path / "does-not-exist")])
    assert agg.total == 0
    assert agg.by_repo == {}


def test_scan_type_hatches_clean_repo_is_honest_zero(tmp_path):
    repo = tmp_path / "clean-repo"
    _write(repo / "src" / "ok.ts", "export const x: number = 1;\n")
    agg = scan_type_hatches([str(repo)])
    assert agg.total == 0
    assert agg.by_repo == {}
    assert agg.files_scanned == 1


def test_build_report_scans_explicit_repo_filter_even_with_no_session_logs(tmp_path):
    """A fresh HOME (no harness logs at all) but an explicit --repo still gets scanned —
    `by_repo` in the tool-adoption aggregate is empty, so build_report must fall back to
    the explicitly-requested repos, not silently report zero."""
    home = tmp_path / "home"
    home.mkdir()
    repo = _fixture_repo(tmp_path)
    report = build_report(home=home, repos=[str(repo)])
    assert report.type_hatches.total == 5
    assert report.type_hatches.by_repo == {str(repo): 5}


def test_build_report_falls_back_to_observed_repos_when_no_repo_filter(tmp_path):
    home = tmp_path / "home"
    repo = _fixture_repo(tmp_path)
    write_claude_session(
        home,
        "-tmp-repo",
        "s1",
        str(repo),
        [_cc_event("2026-06-10T00:00:00Z", str(repo), [{"name": "Bash", "input": {"command": "ls"}}])],
    )
    report = build_report(home=home)
    assert report.type_hatches.total == 5
    assert report.type_hatches.by_repo.get(str(repo)) == 5


def test_json_output_includes_type_hatches(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = _fixture_repo(tmp_path)
    report = build_report(home=home, repos=[str(repo)])
    d = json_out.to_dict(report.aggregate, meta=report.meta, type_hatches=report.type_hatches)
    assert d["type_hatches"]["total"] == 5
    assert d["type_hatches"]["by_repo"][str(repo)] == 5
    assert d["type_hatches"]["by_kind"]["as-any"] == 1


def test_plain_tui_reports_type_hatches(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = _fixture_repo(tmp_path)
    report = build_report(home=home, repos=[str(repo)])
    out = tui.render_plain(report.aggregate, meta=report.meta, type_hatches=report.type_hatches)
    assert "Type escape hatches" in out
    assert "total=5" in out


def test_empty_type_hatches_prints_honest_zero(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "clean-repo"
    _write(repo / "src" / "ok.ts", "export const x: number = 1;\n")
    report = build_report(home=home, repos=[str(repo)])
    out = tui.render_plain(report.aggregate, meta=report.meta, type_hatches=report.type_hatches)
    assert "Type escape hatches (TypeScript): 0" in out
