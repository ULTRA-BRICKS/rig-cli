"""`rig lint text` / `rig lint code`: parsing, the rule-based checks, the model fallback and the report.

Every external tool (vale, slopless, languagetool, review, oxlint) is faked through the
injectable runner/which seams; nothing here spawns a real linter or model.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from riglib import code_check
from riglib import text_check as tc
from riglib.cli import build_parser, main
from riglib.code_check import build_command
from riglib.errors import ConfigError, MissingDepError, MissingTargetError

PROMPT = "Edit this.\nAutomated findings:\n{findings}\n\nDraft:\n<<<DRAFT\n{draft}\nDRAFT>>>\n"

DRAFT = """# Fix the crash

Branch: `alex:fix/crash` -> `main`

It is worth noting that this change seamlessly fixes the crash in 750 ms.

```
Error: Script error. at :0:0
```

| Step | Result |
| --- | --- |
| press OK | no crash |

See [the log](https://example.com/log.txt) for details.
"""


@pytest.fixture
def carrier(tmp_path: Path) -> Path:
    root = tmp_path / "prose"
    (root / "dict").mkdir(parents=True)
    (root / "vale.ini").write_text("StylesPath = styles\n[*.md]\nBasedOnStyles = ai-tells\n", encoding="utf-8")
    (root / "languagetool.json").write_text(
        json.dumps({"languages": {"en": "en-US", "ru": "ru-RU"}, "disable": {"en-US": ["EN_QUOTES"]}}), encoding="utf-8"
    )
    (root / "prompt.md").write_text(PROMPT, encoding="utf-8")
    (root / "dict" / "ru.json").write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [
                    {"id": "ru-yavl", "pattern": "\\bявляется\\b", "flags": "iu", "replacement": "тире", "why": "copula"},
                    {"id": "en-only-with-cyr", "pattern": "\\bfork\\b", "flags": "iu", "only_if_cyrillic": True, "replacement": "x"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return root


def _done(cmd, rc=0, out="", err=""):
    return subprocess.CompletedProcess(cmd, rc, out, err)


def _answer(seat: str, body: str, role: str | None = None) -> str:
    head = f"## {seat} [{role}] [ok]" if role else f"## {seat} [ok]"
    return f"{head}\n\n`omp -p --model x @<payloadfile>`\n\n{body}\n\nstderr:\nWorking...\n\n---\n"


REVISED = (
    "# Fix the crash\n\nBranch: `alex:fix/crash` -> `main`\n\nThis change fixes the crash in 750 ms.\n\n"
    "```\nError: Script error. at :0:0\n```\n\n---\n\n## Testing\n\nSee [the log](https://example.com/log.txt).\n"
)
ANSWER = (
    'FINDINGS\n- "It is worth noting that" -> "" (filler)\n- "seamlessly" -> "" (stock word)\n\n'
    f"REVISED DRAFT\n<<<DRAFT\n{REVISED}DRAFT>>>"
)


def _relabel(answer: str, prompt: str) -> str:
    """Answer with the draft marker the prompt used, the way a seat copies it."""
    found = re.search(r"<<<(\S+)\n", prompt)
    label = found.group(1) if found else "DRAFT"
    return answer.replace("<<<DRAFT", f"<<<{label}").replace("DRAFT>>>", f"{label}>>>")


class FakeTools:
    """Records commands and answers like the real tools; `review` fails for seats in `broken`."""

    def __init__(self, broken=()):
        self.calls: list[list[str]] = []
        self.broken = set(broken)

    def which(self, name):
        return f"/bin/{name}"

    def __call__(self, cmd, *, timeout=0, cwd=None):
        self.calls.append(cmd)
        tool = Path(cmd[0]).name
        if tool == "vale" and cmd[-1] == "sync":
            ini = Path(cmd[1].split("=", 1)[1])
            (ini.parent / "styles").mkdir(exist_ok=True)
            return _done(cmd)
        if tool == "vale":
            alerts = [{"Line": 5, "Span": [4, 20], "Check": "ai-tells.OverusedVocabulary", "Message": "AI vocabulary: 'seamlessly'."}]
            return _done(cmd, 1, json.dumps({cmd[-1]: alerts}))
        if tool == "slopless":
            return _done(cmd, 1, json.dumps([{"messages": [{"line": 5, "column": 1, "ruleId": "hollow-framing", "message": "Hollow framing."}]}]))
        if tool == "languagetool":
            text = Path(cmd[-1]).read_text(encoding="utf-8")
            off = text.index("seamlessly")
            matches = [
                {"offset": off, "length": 10, "message": "Style.", "replacements": [{"value": "smoothly"}], "rule": {"id": "STYLE_X", "issueType": "style"}},
                {"offset": text.index("Fix"), "length": 3, "message": "Typo?", "replacements": [], "rule": {"id": "MORFOLOGIK_RULE_EN_US", "issueType": "misspelling"}},
                {"offset": text.index("code -> code"), "length": 12, "message": "Repeat.", "replacements": [], "rule": {"id": "PLACEHOLDER_REPEAT", "issueType": "duplication"}},
            ]
            return _done(cmd, 0, "Expected text language: English\n" + json.dumps({"matches": matches}))
        if tool == "review":
            seat = cmd[cmd.index("-m") + 1]
            if seat in self.broken:
                return _done(cmd, 1, "", f'Model "{seat}" not found')
            Path(cmd[cmd.index("-o") + 1]).write_text(_answer(seat, _relabel(ANSWER, cmd[2])), encoding="utf-8")
            return _done(cmd)
        raise AssertionError(f"unexpected command {cmd}")


def test_parser_accepts_text_and_code_actions():
    args = build_parser().parse_args(["lint", "text", "a.md", "b.md", "-m", "Fable=x|y", "--out-dir", "o"])
    assert args.lint_command == "text" and args.files == ["a.md", "b.md"] and args.models == ["Fable=x|y"]
    assert not args.no_models and args.out_dir == "o" and args.task == "TEXT-CHECK"
    assert build_parser().parse_args(["lint", "text", "a.md", "--no-models"]).no_models
    with pytest.raises(SystemExit):
        build_parser().parse_args(["lint", "text", "a.md", "-m", "x", "--no-models"])
    args = build_parser().parse_args(["lint", "code", "src", "-C", "repo"])
    assert args.lint_command == "code" and args.paths == ["src"] and args.cwd == "repo"


def test_plain_text_drops_code_tables_and_link_targets_but_keeps_lines():
    text = tc.plain_text(DRAFT)
    lines = text.split("\n")
    assert len(lines) == len(DRAFT.split("\n"))
    assert lines[0] == "Fix the crash"
    assert lines[2] == "Branch: code -> code"
    assert "Script error" not in text and "press OK" not in text
    assert "See the log for details." in text and "example.com" not in text


def test_detect_language():
    assert tc.detect_language("This change fixes the crash.") == "en"
    assert tc.detect_language("Этот патч чинит падение на `Enter`.") == "ru"


def test_dictionary_applies_rules_and_cyrillic_scope(carrier):
    rules = sorted((carrier / "dict").glob("*.json"))
    ru = tc.run_dictionary("Модуль является частью ядра, fork.", rules)
    assert [f.rule for f in ru.findings] == ["ru-yavl", "en-only-with-cyr"]
    assert ru.findings[0].message.startswith('"является": use тире')
    en = tc.run_dictionary("Please fork the repo.", rules)
    assert en.findings == []


def test_parse_just_ask_handles_roles_and_headings_inside_the_answer():
    output = "Roles: 1/1 complete\n" + _answer("omp:a/b", ANSWER, role="quality") + _answer("oc:c/d", "PONG")
    sections = tc.parse_just_ask(output)
    assert sections["oc:c/d"] == ("ok", "PONG")
    status, answer = sections["omp:a/b"]
    findings, revised = tc.parse_answer(answer)
    assert status == "ok" and revised == REVISED
    assert findings == ['"It is worth noting that" -> "" (filler)', '"seamlessly" -> "" (stock word)']


def test_parse_answer_without_findings():
    findings, revised = tc.parse_answer("FINDINGS\n- none\n\nREVISED DRAFT\n<<<DRAFT\nsame\nDRAFT>>>")
    assert findings == [] and revised == "same\n"


def test_ask_slot_falls_back_to_the_next_seat_and_records_why():
    tools = FakeTools(broken={"claude:x"})
    answer = tc.ask_slot("Fable", ("claude:x", "omp:y"), "prompt", "T-1", tools, tools.which)
    assert answer.seat == "omp:y" and answer.revised == REVISED
    assert answer.attempts == [("claude:x", 'Model "claude:x" not found')]
    review_calls = [c for c in tools.calls if Path(c[0]).name == "review"]
    assert all(c[1] == "just-ask" and c[c.index("--pool") + 1] == "1" and c[c.index("--task") + 1] == "T-1" for c in review_calls)


def test_ask_slot_reports_all_failures():
    tools = FakeTools(broken={"a", "b"})
    answer = tc.ask_slot("Astra", ("a", "b"), "p", "T", tools, tools.which)
    assert answer.seat is None and [s for s, _ in answer.attempts] == ["a", "b"]


def test_failure_reason_prefers_the_cause_over_a_stack_trace_line():
    out = "6900 | throw new Error(\nerror: No API key found for openai-codex.\n"
    assert tc._failure_reason(out, "") == "error: No API key found for openai-codex."
    assert tc._failure_reason("", "") == "no usable answer"


def test_revised_draft_prefers_a_slot_that_changed_the_text(tmp_path, carrier):
    draft = tmp_path / "d.md"
    draft.write_text(DRAFT, encoding="utf-8")

    class Unchanged(FakeTools):
        def __call__(self, cmd, *, timeout=0, cwd=None):
            if Path(cmd[0]).name == "review" and cmd[cmd.index("-m") + 1] == "same":
                body = f"FINDINGS\n- none\n\nREVISED DRAFT\n<<<DRAFT\n{DRAFT}DRAFT>>>"
                Path(cmd[cmd.index("-o") + 1]).write_text(_answer("same", _relabel(body, cmd[2])), encoding="utf-8")
                return _done(cmd)
            return super().__call__(cmd, timeout=timeout, cwd=cwd)

    tools = Unchanged()
    _report, checked, report = tc.check_file(
        draft, carrier, [("A", ("same",)), ("B", ("edits",))], "T", tmp_path, runner=tools, which=tools.which, cache_dir=tmp_path / "c"
    )
    assert checked.read_text(encoding="utf-8") == REVISED and "(from B, seat `edits`)" in report


def test_parse_slot():
    assert tc.parse_slot("Astra=codex:x | omp:y", 1) == ("Astra", ("codex:x", "omp:y"))
    assert tc.parse_slot("oc:z", 2) == ("Model 2", ("oc:z",))


def test_fact_guard_flags_dropped_code_numbers_and_links():
    ok = tc.run_fact_guard(DRAFT, DRAFT)
    assert ok.status == "ok" and ok.findings == []
    dropped = tc.run_fact_guard(DRAFT, "# Fix\n\nBranch: `main`.\n")
    missing = {f.message.split(": ", 1)[1] for f in dropped.findings}
    assert {"`alex:fix/crash`", "750", "https://example.com/log.txt", "Error: Script error. at :0:0"} <= missing
    assert tc.run_fact_guard(DRAFT, None).status == "skipped"


def test_missing_tools_are_skipped_not_fatal(tmp_path, carrier):
    draft = tmp_path / "d.md"
    draft.write_text(DRAFT, encoding="utf-8")
    none = lambda _name: None  # noqa: E731
    assert tc.run_vale(draft, "en", carrier, tmp_path / "cache", FakeTools(), none).status == "skipped"
    assert tc.run_slopless(draft, "en", FakeTools(), none).status == "skipped"
    assert tc.run_slopless(draft, "ru", FakeTools(), FakeTools().which).note == "English only"
    assert tc.run_languagetool(DRAFT, tc.plain_text(DRAFT), "en", carrier, FakeTools(), none).status == "skipped"


def test_check_file_writes_report_and_revised_draft(tmp_path, carrier):
    draft = tmp_path / "1-fix.md"
    draft.write_text(DRAFT, encoding="utf-8")
    tools = FakeTools(broken={"claude:fable"})
    slots = [("Fable", ("claude:fable", "omp:fable")), ("Astra", ("codex:astra",))]
    report_path, checked_path, report = tc.check_file(
        draft, carrier, slots, "TEXT-CHECK", tmp_path, runner=tools, which=tools.which, cache_dir=tmp_path / "cache"
    )
    assert report_path == tmp_path / "1-fix.report.md" and checked_path == tmp_path / "1-fix.checked.md"
    assert checked_path.read_text(encoding="utf-8") == REVISED
    assert "| Vale |" in report and "| slopless |" in report and "| LanguageTool |" in report
    assert "| Dictionary |" in report and "| Fact guard |" in report and "| Model review: Astra |" in report
    # style alias + message cleanup; spelling of a capitalised word is filtered, style kept
    assert "`tells.OverusedVocabulary` Vocabulary: 'seamlessly'." in report
    assert "STYLE_X" in report and "MORFOLOGIK" not in report and "PLACEHOLDER_REPEAT" not in report
    assert "Seats that did not answer: `claude:fable`" in report
    # the revision kept every code span, link and number, so it was written
    assert "| Fact guard | the revised draft keeps every code span, link and number of the original | clean |" in report
    # linter findings reach the model prompt
    prompt = next(c for c in tools.calls if Path(c[0]).name == "review")[2]
    assert "tells.OverusedVocabulary" in prompt and "It is worth noting" in prompt
    # vale was synced once into the cache, then reused
    tc.check_file(draft, carrier, [], "T", tmp_path, runner=tools, which=tools.which, cache_dir=tmp_path / "cache")
    assert sum(1 for c in tools.calls if c[-1] == "sync") == 1


def test_run_rejects_a_settings_dir_without_the_prose_files(tmp_path, carrier):
    draft = tmp_path / "d.md"
    draft.write_text(DRAFT, encoding="utf-8")
    args = build_parser().parse_args(["lint", "text", str(draft), "--settings", str(tmp_path / "nope"), "--no-models"])
    with pytest.raises(MissingTargetError):
        tc.run(args)
    assert tc._explicit_settings(carrier) == carrier


def test_run_rejects_a_missing_file():
    args = build_parser().parse_args(["lint", "text", "/no/such/draft.md"])
    with pytest.raises(MissingTargetError):
        tc.run(args)


def test_code_check_needs_a_config_and_a_runnable_oxlint(tmp_path):
    with pytest.raises(MissingTargetError):
        build_command(tmp_path, [], lambda _n: None)
    (tmp_path / "oxlint.config.ts").write_text('jsPlugins: [{ specifier: "./tools/oxlint/anti-slop/index.ts" }]', encoding="utf-8")
    with pytest.raises(MissingDepError):
        build_command(tmp_path, [], lambda _n: None)
    cmd, config, anti_slop = build_command(tmp_path, ["src"], lambda n: f"/bin/{n}" if n == "npx" else None)
    assert cmd == ["npx", "--no-install", "oxlint", "--type-aware", "src"] and config.name == "oxlint.config.ts" and anti_slop
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    cmd, _config, _anti = build_command(tmp_path, [], lambda n: f"/bin/{n}")
    assert cmd == ["pnpm", "exec", "oxlint", "--type-aware", "."]


def test_plain_text_drops_multiline_html_comments():
    text = tc.plain_text("keep\n<!-- TODO:\nreword this\n--> tail\nend")
    assert text.split("\n") == ["keep", "", "", " tail", "end"]


def test_a_broken_settings_file_fails_one_check_not_the_run(tmp_path, carrier):
    user_dict = Path.home() / ".claude" / "DICT.json"
    user_dict.parent.mkdir(parents=True, exist_ok=True)
    user_dict.write_text('{"rules": [', encoding="utf-8")
    (carrier / "languagetool.json").write_text("{}", encoding="utf-8")
    draft = tmp_path / "d.md"
    draft.write_text(DRAFT, encoding="utf-8")
    tools = FakeTools()
    _report_path, _checked, report = tc.check_file(draft, carrier, [], "T", tmp_path, runner=tools, which=tools.which, cache_dir=tmp_path / "c")
    assert "| Dictionary |" in report and "failed: JSONDecodeError" in report
    assert "skipped: languagetool.json has no language code for 'en'" in report
    assert "| Vale |" in report and "1 finding(s)" in report


def test_a_failed_vale_sync_is_a_failed_check_and_is_retried_next_run(tmp_path, carrier):
    class SyncFailsOnce(FakeTools):
        failed = False

        def __call__(self, cmd, *, timeout=0, cwd=None):
            if Path(cmd[0]).name == "vale" and cmd[-1] == "sync" and not self.failed:
                self.failed = True
                return _done(cmd, 2, "", "download failed")
            return super().__call__(cmd, timeout=timeout, cwd=cwd)

    draft = tmp_path / "d.md"
    draft.write_text(DRAFT, encoding="utf-8")
    tools = SyncFailsOnce()
    first = tc.run_vale(draft, "en", carrier, tmp_path / "cache", tools, tools.which)
    assert first.status == "failed" and "vale sync failed: download failed" in first.note
    assert not list((tmp_path / "cache").glob("*/.synced"))
    second = tc.run_vale(draft, "en", carrier, tmp_path / "cache", tools, tools.which)
    assert second.status == "ok" and len(list((tmp_path / "cache").glob("*/.synced"))) == 1


def test_review_slots_refuses_an_oversized_prompt():
    answers = tc.review_slots("x" * (tc.MAX_PROMPT_BYTES + 1), [("A", ("s",))], "T", FakeTools(), FakeTools().which)
    assert answers[0].seat is None and "too large" in answers[0].attempts[0][1]


def test_run_writes_to_out_dir_skips_outputs_and_refuses_collisions(tmp_path, carrier, monkeypatch, capsys):
    monkeypatch.setattr(tc.shutil, "which", lambda _name: None)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    for path in (tmp_path / "a" / "d.md", tmp_path / "b" / "d.md", tmp_path / "a" / "d.report.md"):
        path.write_text(DRAFT, encoding="utf-8")
    out = tmp_path / "out"
    common = ["--settings", str(carrier), "--no-models", "--out-dir", str(out)]
    args = build_parser().parse_args(["lint", "text", str(tmp_path / "a" / "d.md"), str(tmp_path / "a" / "d.report.md"), *common])
    assert tc.run(args) == 0
    assert (out / "d.report.md").is_file() and not (out / "d.checked.md").exists()
    assert "skip d.report.md" in capsys.readouterr().out
    clash = build_parser().parse_args(["lint", "text", str(tmp_path / "a" / "d.md"), str(tmp_path / "b" / "d.md"), *common])
    with pytest.raises(ConfigError):
        tc.run(clash)


def test_cmd_lint_dispatches_text_code_and_rules(monkeypatch):
    import riglib.lint_rules_cli as rules_cli

    monkeypatch.setattr(tc, "run", lambda _args: 11)
    monkeypatch.setattr(code_check, "run", lambda _args: 12)
    monkeypatch.setattr(rules_cli, "run", lambda _args: 13)
    assert main(["lint", "text", "x.md"]) == 11
    assert main(["lint", "code"]) == 12
    assert main(["lint", "rules"]) == 13
    assert main(["lint"]) == 13


def test_resolve_oxlint_prefers_the_repo_local_binary(tmp_path):
    local = tmp_path / "node_modules" / ".bin" / "oxlint"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n", encoding="utf-8")
    local.chmod(0o755)
    assert code_check.resolve_oxlint(tmp_path, lambda n: f"/bin/{n}") == [str(local)]


def test_code_lint_retries_without_nested_configs(tmp_path):
    calls = []

    def runner(cmd, _root):
        calls.append(cmd)
        if "--disable-nested-config" in cmd:
            return _done(cmd, 1, "x anti-slop(no-unknown-returns)\nFound 0 warnings and 1 error.\nFinished in 5ms\n")
        return _done(cmd, 1, "x The `options.typeAware` option is only supported in the root config\n")

    proc, retried = code_check.lint(tmp_path, ["oxlint", "--type-aware", "."], runner)
    assert retried and proc.returncode == 1 and "anti-slop" in proc.stdout
    assert calls[1] == ["oxlint", "--type-aware", "--disable-nested-config", "."]
    clean = code_check.lint(tmp_path, ["oxlint", "--type-aware", "."], lambda cmd, _r: _done(cmd, 0, "Finished in 1ms\n"))
    assert clean[0].returncode == 0 and not clean[1]


def test_code_check_run_prints_the_command_and_returns_oxlint_code(tmp_path, monkeypatch, capsys):
    (tmp_path / "oxlint.config.ts").write_text("rules: {}", encoding="utf-8")
    monkeypatch.setattr(code_check, "_repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr(code_check, "resolve_oxlint", lambda _root, _which=None: ["oxlint"])
    monkeypatch.setattr(code_check, "lint", lambda _root, cmd: (_done(cmd, 1, "Found 1 error.\nFinished in 1ms\n"), False))
    args = build_parser().parse_args(["lint", "code", "src", "-C", str(tmp_path)])
    assert code_check.run(args) == 1
    out = capsys.readouterr().out
    assert "rig lint code: oxlint --type-aware src" in out and "anti-slop rules: off" in out and "Found 1 error." in out


def test_out_dir_collision_uses_the_derived_report_name(tmp_path, carrier):
    (tmp_path / "notes.md").write_text(DRAFT, encoding="utf-8")
    (tmp_path / "notes").write_text(DRAFT, encoding="utf-8")
    args = build_parser().parse_args(
        ["lint", "text", str(tmp_path / "notes.md"), str(tmp_path / "notes"), "--settings", str(carrier), "--no-models"]
    )
    with pytest.raises(ConfigError):
        tc.run(args)


def test_wrong_type_settings_fail_one_check(tmp_path, carrier):
    (carrier / "languagetool.json").write_text('{"languages": []}', encoding="utf-8")
    (carrier / "dict" / "ru.json").write_text('{"rules": {"x": {"pattern": "a"}}}', encoding="utf-8")
    lt = tc._guarded("LanguageTool", lambda: tc.run_languagetool("a", "a", "en", carrier, FakeTools(), FakeTools().which))
    dictionary = tc._guarded("Dictionary", lambda: tc.run_dictionary("a", sorted((carrier / "dict").glob("*.json"))))
    assert lt.status == "failed" and "TypeError" in lt.note
    assert dictionary.status == "failed" and "rules must be a list" in dictionary.note


def test_regex_flags_follow_the_dictionary_contract():
    assert tc.regex_flags("ium") == tc.re.IGNORECASE | tc.re.MULTILINE
    with pytest.raises(ValueError):
        tc.regex_flags("x")


def test_fact_guard_compares_exact_tokens():
    original = "Release 12: see [runbook](../runbook.md) and https://example.com/a.\n"
    changed = tc.run_fact_guard(original, "Release 123: see [runbook](../other.md) and https://example.com/a-b.\n")
    missing = {f.message.split(": ", 1)[1] for f in changed.findings}
    assert missing == {"12", "../runbook.md", "https://example.com/a"}
    assert tc.run_fact_guard(original, original).findings == []


def test_empty_slot_is_reported():
    answer = tc.ask_slot("Fable", (), "p", "T", FakeTools(), FakeTools().which)
    assert answer.seat is None and answer.attempts == [("-", "no seats given for this slot")]


def test_draft_containing_the_marker_gets_a_unique_label(carrier):
    draft = "Example:\n\n```\nDRAFT>>>\n```\n"
    label = tc.draft_label(draft)
    assert label.startswith("DRAFT-") and label not in draft and tc.draft_label("plain") != tc.draft_label("plain")
    prompt = tc.build_prompt(PROMPT, draft, [], label)
    assert f"<<<{label}\n" in prompt and f"\n{label}>>>" in prompt
    answer = f"FINDINGS\n- none\n\nREVISED DRAFT\n<<<{label}\n{draft}{label}>>>"
    assert tc.parse_answer(answer, label)[1] == draft


def test_code_lint_does_not_retry_a_timeout(tmp_path):
    calls = []

    def runner(cmd, _root):
        calls.append(cmd)
        return _done(cmd, code_check.TIMED_OUT, "", "oxlint timed out")

    proc, retried = code_check.lint(tmp_path, ["oxlint", "--type-aware", "."], runner)
    assert proc.returncode == code_check.TIMED_OUT and not retried and len(calls) == 1


def test_catalog_settings_come_from_the_agent_tools_checkout(fake_agent_tools, monkeypatch, tmp_path):
    prose = fake_agent_tools / "linters" / "prose"
    prose.mkdir(parents=True)
    for name in ("vale.ini", "languagetool.json", "prompt.md"):
        (prose / name).write_text("x", encoding="utf-8")
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    assert tc._catalog_settings(outside) == prose.resolve()


def test_plain_text_keeps_a_comment_opener_inside_inline_code():
    text = tc.plain_text("see `<x><!--` for details\nnext line")
    assert text.split("\n") == ["see code for details", "next line"]


def test_build_prompt_does_not_expand_placeholders_inside_findings():
    finding = tc.CheckResult("Dictionary", "s", "ok", [tc.Finding(1, None, "r", 'quotes "{draft}"')])
    prompt = tc.build_prompt("F:\n{findings}\nD:\n<<<DRAFT\n{draft}\nDRAFT>>>", "BODY", [finding], "DRAFT-1")
    assert prompt.count("BODY") == 1 and 'quotes "{draft}"' in prompt and "<<<DRAFT-1\nBODY\nDRAFT-1>>>" in prompt


def test_review_slots_with_no_slots_returns_nothing():
    assert tc.review_slots("p", [], "T", FakeTools(), FakeTools().which) == []


def test_choose_revision_prefers_a_clean_edit():
    original = "Wait 750 ms before `retry()`.\n"
    dropped = tc.ModelAnswer("A", "a", [], revised="Wait before retrying.\n")
    clean = tc.ModelAnswer("B", "b", [], revised="Wait 750 ms, then call `retry()`.\n")
    chosen, guard = tc.choose_revision(original, [dropped, clean])
    assert chosen is clean and guard.findings == []
    chosen, guard = tc.choose_revision(original, [dropped])
    assert chosen is dropped and {f.message.split(": ", 1)[1] for f in guard.findings} == {"750", "`retry()`"}
    assert tc.choose_revision(original, []) == (None, tc.run_fact_guard(original, None))


def test_run_rejects_a_draft_that_is_not_utf8(tmp_path, carrier):
    draft = tmp_path / "latin1.md"
    draft.write_bytes("caf\xe9".encode("latin-1"))
    args = build_parser().parse_args(["lint", "text", str(draft), "--settings", str(carrier), "--no-models"])
    with pytest.raises(MissingTargetError):
        tc.run(args)


def test_oxlint_that_cannot_start_is_reported_not_retried(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("Exec format error")

    monkeypatch.setattr(code_check.subprocess, "run", boom)
    proc, retried = code_check.lint(tmp_path, ["oxlint", "--type-aware", "."])
    assert proc.returncode == code_check.CANNOT_RUN and "Exec format error" in proc.stderr and not retried


def test_fact_guard_keeps_parenthesised_links_whole_and_counts_repeats():
    original = "See [spec](https://example.test/a_(v1)) and `x` or `x`.\n"
    changed = tc.run_fact_guard(original, "See [spec](https://example.test/a_(v1)/other) and `x`.\n")
    missing = {f.message for f in changed.findings}
    assert missing == {"not in the revised draft: https://example.test/a_(v1)", "not in the revised draft (1 of 2): `x`"}
    assert tc.run_fact_guard(original, original).findings == []


def test_unreadable_prompt_fails_the_model_review_only(tmp_path, carrier):
    (carrier / "prompt.md").write_bytes(b"\xff\xfe broken")
    draft = tmp_path / "d.md"
    draft.write_text(DRAFT, encoding="utf-8")
    tools = FakeTools()
    _report_path, checked, report = tc.check_file(draft, carrier, [("Fable", ("omp:fable",))], "T", tmp_path, runner=tools, which=tools.which, cache_dir=tmp_path / "c")
    assert checked is None and "`prompt.md` (unreadable" in report and "| Vale |" in report


def test_an_unreadable_answer_file_falls_back_to_the_next_seat():
    class BadBytesFirst(FakeTools):
        def __call__(self, cmd, *, timeout=0, cwd=None):
            if Path(cmd[0]).name == "review" and cmd[cmd.index("-m") + 1] == "bad":
                Path(cmd[cmd.index("-o") + 1]).write_bytes(b"\xff\xfe")
                return _done(cmd)
            return super().__call__(cmd, timeout=timeout, cwd=cwd)

    tools = BadBytesFirst()
    answer = tc.ask_slot("Fable", ("bad", "good"), "prompt", "T", tools, tools.which)
    assert answer.seat == "good" and answer.attempts[0][0] == "bad" and "unreadable answer file" in answer.attempts[0][1]


def test_an_older_checked_draft_is_kept_and_flagged(tmp_path, carrier):
    draft = tmp_path / "d.md"
    draft.write_text(DRAFT, encoding="utf-8")
    older = tmp_path / "d.checked.md"
    older.write_text("old revision", encoding="utf-8")
    tools = FakeTools()
    _report_path, checked, report = tc.check_file(draft, carrier, [], "T", tmp_path, runner=tools, which=tools.which, cache_dir=tmp_path / "c")
    assert checked is None and older.read_text(encoding="utf-8") == "old revision"
    assert "`d.checked.md` is from an earlier run or another tool" in report


def test_outputs_are_never_written_through_a_symlink(tmp_path, carrier):
    draft = tmp_path / "d.md"
    draft.write_text(DRAFT, encoding="utf-8")
    target = tmp_path / "precious.txt"
    target.write_text("keep", encoding="utf-8")
    (tmp_path / "d.report.md").symlink_to(target)
    tools = FakeTools()
    with pytest.raises(ConfigError):
        tc.check_file(draft, carrier, [], "T", tmp_path, runner=tools, which=tools.which, cache_dir=tmp_path / "c")
    assert target.read_text(encoding="utf-8") == "keep"


def test_fact_guard_keeps_signs_nested_links_and_autolinks():
    original = "Retry after -1 attempts; see [doc](../a_(b_(c)).md) and <https://example.test/x>.\n"
    assert tc.run_fact_guard(original, original).findings == []
    changed = tc.run_fact_guard(original, "Retry after 1 attempts; see [doc](../a.md) and [x](https://example.test/x).\n")
    assert {f.message.split(": ", 1)[1] for f in changed.findings} == {"-1", "../a_(b_(c)).md"}


def test_tilde_fences_are_code_too():
    kinds = [kind for _n, _line, kind in tc._fence_lines("~~~\nx = 1\n```\n~~~\ntext")]
    assert kinds == ["fence", "code", "code", "fence", "text"]


def test_spelling_filter_checks_the_first_word_of_a_sentence():
    assert tc._skip_spelling("Sendspin", set(), sentence_start=False)
    assert not tc._skip_spelling("Teh", set(), sentence_start=True)
    assert tc._skip_spelling("PlayerManager", set(), sentence_start=True)
    assert tc._sentence_start("Done. Teh", 6) and not tc._sentence_start("uses Sendspin", 5)


def test_code_lint_retry_without_the_type_aware_flag(tmp_path):
    calls = []

    def runner(cmd, _root):
        calls.append(cmd)
        return _done(cmd, 1, "Finished in 1ms\n" if "--disable-nested-config" in cmd else "config error\n")

    proc, retried = code_check.lint(tmp_path, ["oxlint", "."], runner)
    assert retried and calls[1] == ["oxlint", ".", "--disable-nested-config"]


def test_vale_cache_keeps_one_directory_per_settings_revision(tmp_path, carrier):
    draft = tmp_path / "d.md"
    draft.write_text(DRAFT, encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    (other / "vale.ini").write_text("StylesPath = styles\n[*.md]\nBasedOnStyles = proselint\n", encoding="utf-8")
    tools = FakeTools()
    cache = tmp_path / "cache"
    for settings in (carrier, other, carrier):
        assert tc.run_vale(draft, "en", settings, cache, tools, tools.which).status == "ok"
    assert len(list(cache.glob("*/.synced"))) == 2
    assert sum(1 for c in tools.calls if c[-1] == "sync") == 2


def test_fences_close_only_on_a_long_enough_marker():
    markdown = "````\nfirst()\n```\nsecond()\n````\nprose 7\n"
    kinds = [kind for _n, _line, kind in tc._fence_lines(markdown)]
    assert kinds == ["fence", "code", "code", "code", "fence", "text", "text"]
    tokens = {t for t, _l, _k in tc.fact_tokens(markdown)}
    assert {"first()", "```", "second()", "7"} <= tokens
    assert tc.plain_text(markdown).split("\n") == ["", "", "", "", "", "prose 7", ""]


def test_fact_guard_sees_titled_and_reference_links():
    original = 'Read [runbook](../docs/runbook.md "Operations runbook").\n\n[spec]: https://example.test/spec\n'
    dropped = tc.run_fact_guard(original, "Read the runbook.\n")
    missing = {f.message.split(": ", 1)[1] for f in dropped.findings}
    assert missing == {"../docs/runbook.md", "https://example.test/spec"}


def test_code_paths_are_relative_to_where_rig_runs(tmp_path):
    sub = tmp_path / "pkg" / "src"
    sub.mkdir(parents=True)
    assert code_check.repo_paths(sub, tmp_path, ["a.ts", "."]) == [str(Path("pkg/src/a.ts")), str(Path("pkg/src"))]
    assert code_check.repo_paths(sub, tmp_path, []) == [str(Path("pkg/src"))]
    assert code_check.repo_paths(tmp_path, tmp_path, []) == ["."]
    outside = tmp_path.parent / "elsewhere.ts"
    assert code_check.repo_paths(tmp_path, tmp_path, [str(outside)]) == [str(outside.resolve())]


def test_resolve_oxlint_uses_yarn_in_a_yarn_repo(tmp_path):
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    assert code_check.resolve_oxlint(tmp_path, lambda n: f"/bin/{n}") == ["yarn", "oxlint"]


def test_article_matches_before_a_code_placeholder_are_dropped(carrier):
    markdown = "Send an `IDLE` status and an update.\n"
    text = tc.plain_text(markdown)

    def runner(cmd, *, timeout=0, cwd=None):
        plain = Path(cmd[-1]).read_text(encoding="utf-8")
        matches = [
            {"offset": plain.index("an code"), "length": 2, "message": "Use a.", "replacements": [{"value": "a"}], "rule": {"id": "EN_A_VS_AN", "issueType": "misspelling"}},
            {"offset": plain.index("an update"), "length": 2, "message": "Use a.", "replacements": [{"value": "a"}], "rule": {"id": "EN_A_VS_AN", "issueType": "misspelling"}},
        ]
        return _done(cmd, 0, json.dumps({"matches": matches}))

    result = tc.run_languagetool(markdown, text, "en", carrier, runner, lambda n: f"/bin/{n}")
    assert [f.message for f in result.findings] == ['Use a. "an" -> a']


def test_a_revision_that_drops_a_fact_is_withheld(tmp_path, carrier):
    draft = tmp_path / "d.md"
    draft.write_text(DRAFT, encoding="utf-8")

    class DropsTheNumber(FakeTools):
        def __call__(self, cmd, *, timeout=0, cwd=None):
            if Path(cmd[0]).name == "review":
                body = "FINDINGS\n- none\n\nREVISED DRAFT\n<<<DRAFT\n" + REVISED.replace(" in 750 ms", "") + "DRAFT>>>"
                Path(cmd[cmd.index("-o") + 1]).write_text(_answer(cmd[cmd.index("-m") + 1], _relabel(body, cmd[2])), encoding="utf-8")
                return _done(cmd)
            return super().__call__(cmd, timeout=timeout, cwd=cwd)

    tools = DropsTheNumber()
    _report_path, checked, report = tc.check_file(draft, carrier, [("Fable", ("omp:fable",))], "T", tmp_path, runner=tools, which=tools.which, cache_dir=tmp_path / "c")
    assert checked is None and not (tmp_path / "d.checked.md").exists()
    assert "Revised draft: withheld. The best revision (Fable) dropped 1 fact token(s)" in report
    assert "not in the revised draft: 750" in report


def test_multi_backtick_code_spans_are_one_token():
    tokens = [t for t, _l, kind in tc.fact_tokens("Run ``git commit -m `x` `` now.\n") if kind == "code"]
    assert tokens == ["``git commit -m `x` ``"]
    assert tc.plain_text("Run ``a `b` c`` now") == "Run code now"


def test_malformed_tool_json_fails_nothing_and_crashes_nothing(tmp_path, carrier):
    draft = tmp_path / "d.md"
    draft.write_text(DRAFT, encoding="utf-8")

    class OddJson(FakeTools):
        def __call__(self, cmd, *, timeout=0, cwd=None):
            tool = Path(cmd[0]).name
            if tool == "vale" and cmd[-1] != "sync":
                return _done(cmd, 1, json.dumps({str(draft): {"error": "x"}}))
            if tool == "slopless":
                return _done(cmd, 1, json.dumps(["bad"]))
            if tool == "languagetool":
                return _done(cmd, 0, json.dumps({"matches": [None, {"offset": 0, "length": 1, "message": ",", "rule": {"id": "COMMA"}}]}))
            return super().__call__(cmd, timeout=timeout, cwd=cwd)

    tools = OddJson()
    _report_path, _checked, report = tc.check_file(draft, carrier, [], "T", tmp_path, runner=tools, which=tools.which, cache_dir=tmp_path / "c")
    assert "| Vale |" in report and "| slopless |" in report and "`COMMA`" in report


def test_languagetool_offsets_count_utf16_units():
    text = "ok \U0001F600 teh end"
    assert tc._utf16_to_index(text, 6) == text.index("teh")
    assert tc._utf16_to_index("plain", 3) == 3


def test_crlf_answers_still_yield_the_revised_draft():
    answer = "FINDINGS\r\n- none\r\n\r\nREVISED DRAFT\r\n<<<DRAFT-1\r\nline one\r\nline two\r\nDRAFT-1>>>"
    assert tc.parse_answer(answer, "DRAFT-1")[1] == "line one\nline two\n"


def test_option_shaped_paths_reach_oxlint_as_paths(tmp_path):
    assert code_check.repo_paths(tmp_path, tmp_path, ["--fix"]) == [f".{code_check.os.sep}--fix"]
