"""`rig lint text` — check Markdown drafts before they are published.

A draft (PR description, issue, reply, doc) goes through the rule-based checks first, then a
copy-edit by model seats through `review just-ask`:

- **Vale** with the pinned styles in agent-tools ``linters/prose/vale.ini`` (English only);
- **slopless** (English only);
- **LanguageTool** (English or Russian) on the Markdown-stripped text;
- **dictionary** rules: ``~/.claude/DICT.json`` (the list ``tg`` enforces) plus the packs in
  ``linters/prose/dict/``;
- **model review**: one ``review just-ask`` call per slot (default Fable and Astra), each slot
  trying its seats in order until one answers;
- **fact guard**: every code span, link and number of the original must survive in the revised
  draft.

Output per draft: ``<name>.report.md`` (each check, what it checks, its findings) and, when a
seat returned one, ``<name>.checked.md`` (the revised draft). A missing tool, a broken settings
file or a failing seat is reported as a skipped or failed check, never a crash. Stdlib-only;
every external command goes through an injectable runner so the tests never spawn a real
linter or model.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Literal

from .catalog import CatalogError, prose_carrier, resolve_source, validate_prose_dir
from .detect import WhichFn as Which
from .detect import git_toplevel
from .errors import ConfigError, MissingTargetError

# Slot name -> seats tried in order. Fable 5.2 is not in every harness catalog yet, so the
# chain ends on the newest Fable that answers; Astra runs through the codex CLI login.
DEFAULT_SLOTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Fable", ("claude:claude-fable-5-2", "omp:anthropic/claude-fable-5-2", "omp:anthropic/claude-fable-5-1")),
    ("Astra", ("codex:gpt-6-astra", "omp:openai-codex/gpt-6-astra")),
)
DEFAULT_TASK = "TEXT-CHECK"
USER_DICTIONARY = "~/.claude/DICT.json"
CACHE_DIR = "~/.cache/rig/prose"
TOOL_TIMEOUT_S = 300
STALE_SYNC_S = 3600  # a staging directory older than this belongs to a killed sync
MODEL_TIMEOUT_S = 1200
# `review just-ask` takes the question as one argv element; Linux caps one at 128 KiB.
MAX_PROMPT_BYTES = 100_000
OUTPUT_SUFFIXES = (".report.md", ".checked.md")
TIMED_OUT = 124  # exit code a runner reports for a command it had to stop
CANNOT_RUN = 126  # exit code a runner reports for a command that could not start

SCOPES = {
    "Vale": "stock vocabulary, filler, hedging and sentence tics (tells); clichés and redundancy "
    "(proselint); passive voice, weasel words, wordy phrases (write-good); Flesch-Kincaid grade "
    "and Gunning Fog index (Readability)",
    "slopless": "padded, vague or formulaic English: hollow framing, fake contrasts, hedging, "
    "em-dash tics, vacuous closers",
    "LanguageTool": "grammar, spelling, punctuation and style",
    "Dictionary": "banned words and stock phrases",
    "Fact guard": "the revised draft keeps every code span, link and number of the original",
}
MODEL_SCOPE = "copy-edit: flags filler and stock phrasing, suggests tighter wording, keeps facts"

# Report aliases for third-party style names; the rule id after the dot is unchanged.
_STYLE_ALIASES = {"ai-tells": "tells"}

Runner = Callable[..., subprocess.CompletedProcess]
Status = Literal["ok", "skipped", "failed"]


def default_runner(cmd: list[str], *, timeout: int = TOOL_TIMEOUT_S, cwd: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, TIMED_OUT, "", f"timed out after {timeout}s")
    except OSError as exc:  # unlaunchable binary, argument list too long
        return subprocess.CompletedProcess(cmd, CANNOT_RUN, "", f"error: could not run {cmd[0]}: {exc}")


@dataclass(frozen=True)
class Finding:
    line: int
    column: int | None  # None: the check ran on the stripped text, so only the line is exact
    rule: str
    message: str


@dataclass
class CheckResult:
    name: str
    scope: str
    status: Status
    findings: list[Finding] = field(default_factory=list)
    note: str = ""


@dataclass
class ModelAnswer:
    slot: str
    seat: str | None
    attempts: list[tuple[str, str]]
    findings: list[str] = field(default_factory=list)
    revised: str | None = None


def _check(name: str, status: Status = "ok", note: str = "", *, scope: str | None = None) -> CheckResult:
    return CheckResult(name, scope or SCOPES[name], status, note=note)


# ── Markdown → plain text (line structure preserved) ───────────────────────────────────
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
# A code span is a backtick run, content, and the same run again (CommonMark), so a span
# delimited by two backticks may contain a single one.
_INLINE_CODE = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_IMAGE_OR_LINK = re.compile(r"!?\[([^\]\n]*)\]\([^)\n]*\)")
_URL = re.compile(r"https?://\S+")
_HTML_COMMENT = re.compile(r"<!--.*?-->")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_QUOTE = re.compile(r"^\s*>\s?")
_STRONG = re.compile(r"(\*\*|__)(.+?)\1")
_EMPHASIS = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_PLACEHOLDERS = {"code", "link"}  # what plain_text() puts in place of code spans and URLs
_ARTICLE_RULES = {"EN_A_VS_AN"}  # a/an agreement, which depends on the word the placeholder hides


def _fence_lines(markdown: str) -> Iterator[tuple[int, str, str]]:
    """(line number, line, kind) with kind "fence" for a delimiter, "code" inside a fence, else "text".

    As in CommonMark, a fence closes only on the same character repeated at least as many times,
    so a ``` line inside a ```` block stays code. plain_text() and fact_tokens() both walk this.
    """
    fence = ""
    for number, line in enumerate(markdown.split("\n"), 1):
        opener = _FENCE.match(line)
        if fence:
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence) and not line[opener.end():].strip():
                fence = ""
                yield number, line, "fence"
            else:
                yield number, line, "code"
        elif opener:
            fence = opener.group(1)
            yield number, line, "fence"
        else:
            yield number, line, "text"


def plain_text(markdown: str) -> str:
    """Strip Markdown syntax for grammar and phrase checks; line N of the result is line N of the input.

    Code (fenced and inline), tables, HTML comments, link targets and bare URLs carry no prose,
    so they are blanked or replaced with a neutral word instead of being fed to a grammar checker.
    """
    out: list[str] = []
    in_comment = False
    for _number, line, kind in _fence_lines(markdown):
        if kind != "text":
            out.append("")
            continue
        if in_comment:
            end = line.find("-->")
            if end < 0:
                out.append("")
                continue
            in_comment = False
            line = line[end + 3 :]
        if line.lstrip().startswith("|"):
            out.append("")
            continue
        line = _INLINE_CODE.sub("code", line)  # first: a `<!--` inside code is not a comment
        line = _HTML_COMMENT.sub("", line)
        if "<!--" in line:
            line, in_comment = line[: line.index("<!--")], True
        line = _IMAGE_OR_LINK.sub(r"\1", line)
        line = _URL.sub("link", line)
        for pattern in (_HEADING, _LIST_MARKER, _QUOTE):
            line = pattern.sub("", line)
        line = _STRONG.sub(r"\2", line)
        line = _EMPHASIS.sub(r"\1", line)
        out.append(line)
    return "\n".join(out)


def detect_language(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    cyrillic = sum(1 for c in letters if "Ѐ" <= c <= "ӿ")
    return "ru" if letters and cyrillic / len(letters) >= 0.3 else "en"


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _code_words(markdown: str) -> set[str]:
    words: set[str] = set()
    for span in _INLINE_CODE.finditer(markdown):
        words.update(w.lower() for w in re.findall(r"[^\W\d_]+", span.group(2)))
    return words


def _tool_error(proc: subprocess.CompletedProcess) -> str:
    return (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:300]


# ── Vale ───────────────────────────────────────────────────────────────────────────────
def _display_rule(check: str) -> str:
    style, _, rule = check.partition(".")
    return f"{_STYLE_ALIASES.get(style, style)}.{rule}" if rule else check


def _clean_message(message: str) -> str:
    message = re.sub(r"^AI\s+", "", message.strip())
    return message[:1].upper() + message[1:]


def _styles_path(ini_text: str) -> str:
    match = re.search(r"^\s*StylesPath\s*=\s*(\S+)", ini_text, re.MULTILINE)
    return match.group(1) if match else "styles"


def _vale_config(carrier: Path, cache_dir: Path, vale: str, runner: Runner) -> tuple[Path | None, str]:
    """Return a synced vale.ini, running `vale sync` only for settings not synced before.

    Each settings revision gets its own directory named after the ini's hash, filled in a
    staging directory and renamed into place only after the sync succeeded (a marker is
    written last). A synced directory never changes afterwards, so a failed or killed sync,
    two runs racing, or runs with different `--settings` never see each other's styles.
    """
    source = (carrier / "vale.ini").read_text(encoding="utf-8")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    final = cache_dir / digest[:16]

    def synced() -> bool:
        marker = final / ".synced"
        return marker.is_file() and marker.read_text(encoding="utf-8") == digest and (final / _styles_path(source)).is_dir()

    if synced():
        return final / "vale.ini", ""
    cache_dir.mkdir(parents=True, exist_ok=True)
    for stale in cache_dir.glob("sync-*"):
        if time.time() - stale.stat().st_mtime > STALE_SYNC_S:
            shutil.rmtree(stale, ignore_errors=True)  # left by a sync that was killed
    staging = Path(tempfile.mkdtemp(prefix="sync-", dir=cache_dir))
    (staging / "vale.ini").write_text(source, encoding="utf-8")
    proc = runner([vale, f"--config={staging / 'vale.ini'}", "sync"], timeout=TOOL_TIMEOUT_S)
    if proc.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        return None, f"vale sync failed: {_tool_error(proc)}"
    (staging / ".synced").write_text(digest, encoding="utf-8")
    if final.exists() and not synced():
        shutil.rmtree(final, ignore_errors=True)  # an incomplete directory from an older layout or crash
    try:
        staging.rename(final)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)  # another run finished the same sync first
        if not synced():
            return None, "vale cache is being written by another run; try again"
    return final / "vale.ini", ""


def run_vale(path: Path, language: str, carrier: Path, cache_dir: Path, runner: Runner, which: Which) -> CheckResult:
    if language != "en":
        return _check("Vale", "skipped", "the styles are English-only")
    vale = which("vale")
    if not vale:
        return _check("Vale", "skipped", "vale is not installed (brew install vale)")
    ini, error = _vale_config(carrier, cache_dir, vale, runner)
    if ini is None:
        return _check("Vale", "failed", error)
    proc = runner([vale, f"--config={ini}", "--output=JSON", str(path)], timeout=TOOL_TIMEOUT_S)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        data = None
    if proc.returncode not in (0, 1) or not isinstance(data, dict):
        return _check("Vale", "failed", _tool_error(proc))
    result = _check("Vale")
    for alerts in data.values():
        for alert in alerts if isinstance(alerts, list) else []:
            if not isinstance(alert, dict):
                continue
            span = alert.get("Span") or [0]
            result.findings.append(
                Finding(int(alert.get("Line", 0)), int(span[0]), _display_rule(alert.get("Check", "")), _clean_message(alert.get("Message", "")))
            )
    return result


# ── slopless ───────────────────────────────────────────────────────────────────────────
def run_slopless(path: Path, language: str, runner: Runner, which: Which) -> CheckResult:
    if language != "en":
        return _check("slopless", "skipped", "English only")
    exe = which("slopless")
    if not exe:
        return _check("slopless", "skipped", "slopless is not installed (npm install -g slopless)")
    proc = runner([exe, str(path)], timeout=TOOL_TIMEOUT_S)
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        data = None
    if proc.returncode not in (0, 1) or not isinstance(data, list):
        return _check("slopless", "failed", _tool_error(proc))
    result = _check("slopless")
    for entry in data:
        messages = entry.get("messages", []) if isinstance(entry, dict) else []
        for msg in messages if isinstance(messages, list) else []:
            if not isinstance(msg, dict):
                continue
            result.findings.append(
                Finding(int(msg.get("line", 0)), int(msg.get("column", 0)), str(msg.get("ruleId", "")), str(msg.get("message", "")).strip())
            )
    return result


# ── LanguageTool ───────────────────────────────────────────────────────────────────────
def _sentence_start(text: str, offset: int) -> bool:
    before = text[:offset].rstrip(" \t\"'(«")
    return not before or before[-1] in ".!?:\n"


def _skip_spelling(word: str, code_words: set[str], sentence_start: bool = False) -> bool:
    """Identifiers and proper nouns are not typos: CamelCase, digits, paths, words used in code,
    and a capitalised word inside a sentence. A capitalised first word of a sentence is checked."""
    return (
        any(c.isupper() for c in word[1:])
        or (word[:1].isupper() and not sentence_start)
        or any(c.isdigit() for c in word)
        or any(c in "_./:-" for c in word)
        or word.lower() in code_words
    )


def _utf16_to_index(text: str, units: int) -> int:
    """LanguageTool (Java) counts UTF-16 code units; a character outside the BMP counts as two."""
    if units <= 0 or all(ord(c) <= 0xFFFF for c in text[:units]):
        return units
    index = count = 0
    while index < len(text) and count < units:
        count += 2 if ord(text[index]) > 0xFFFF else 1
        index += 1
    return index


def _mapping(value: object, what: str) -> dict:
    if not isinstance(value, dict):
        raise TypeError(f"{what} must be a JSON object, got {type(value).__name__}")
    return value


def run_languagetool(markdown: str, text: str, language: str, carrier: Path, runner: Runner, which: Which) -> CheckResult:
    settings = _mapping(json.loads((carrier / "languagetool.json").read_text(encoding="utf-8")), "languagetool.json")
    code = _mapping(settings.get("languages", {}), "languagetool.json languages").get(language)
    if not isinstance(code, str) or not code:
        return _check("LanguageTool", "skipped", f"languagetool.json has no language code for {language!r}")
    scope = f"{SCOPES['LanguageTool']} ({code})"
    exe = which("languagetool")
    if not exe:
        return _check("LanguageTool", "skipped", "languagetool is not installed (brew install languagetool)", scope=scope)
    disabled = [str(rule) for rule in _mapping(settings.get("disable", {}), "languagetool.json disable").get(code, [])]
    code_words = _code_words(markdown)
    with tempfile.TemporaryDirectory() as tmp:
        plain = Path(tmp) / "draft.txt"
        plain.write_text(text, encoding="utf-8")
        cmd = [exe, "-l", code, "--json"] + (["-d", ",".join(disabled)] if disabled else []) + [str(plain)]
        proc = runner(cmd, timeout=TOOL_TIMEOUT_S)
    raw = proc.stdout or ""
    start = raw.find("{")
    try:
        data = json.loads(raw[start:]) if start >= 0 else None
    except json.JSONDecodeError:
        data = None
    if proc.returncode != 0 or not isinstance(data, dict):
        return _check("LanguageTool", "failed", _tool_error(proc), scope=scope)
    result = _check("LanguageTool", scope=scope)
    matches = data.get("matches", [])
    for match in matches if isinstance(matches, list) else []:
        if not isinstance(match, dict):
            continue
        offset = _utf16_to_index(text, int(match.get("offset", 0)))
        length = _utf16_to_index(text[offset:], int(match.get("length", 0)))
        word = text[offset : offset + length]
        rule = match.get("rule") if isinstance(match.get("rule"), dict) else {}
        if rule.get("issueType") == "misspelling" and _skip_spelling(word, code_words, _sentence_start(text, offset)):
            continue
        words = set(re.findall(r"\w+", word))
        if words and words <= _PLACEHOLDERS:
            continue  # a match on the words plain_text() put in place of code and links
        following = re.match(r"\s*(\w+)", text[offset + length :])
        if rule.get("id") in _ARTICLE_RULES and following and following.group(1) in _PLACEHOLDERS:
            continue  # "an `IDLE` status" became "an code status": the article fits the real code
        replacements = [r.get("value", "") for r in match.get("replacements", [])[:3]]
        message = f'{match.get("message", "").strip()} "{" ".join(word.split())}"'
        if replacements:
            message += " -> " + " / ".join(replacements)
        result.findings.append(Finding(_line_of(text, offset), None, str(rule.get("id", "")), message))
    return result


# ── dictionary rules (DICT.json format) ────────────────────────────────────────────────
def load_dictionary_rules(paths: list[Path]) -> list[dict]:
    rules: list[dict] = []
    for path in paths:
        if path.is_file():
            data = _mapping(json.loads(path.read_text(encoding="utf-8")), path.name)
            entries = data.get("rules", [])
            if not isinstance(entries, list) or not all(isinstance(rule, dict) for rule in entries):
                raise TypeError(f"{path.name}: rules must be a list of objects")
            rules.extend(rule for rule in entries if rule.get("pattern"))
    return rules


# Same flag contract as the chat-dictionary-gate reading ~/.claude/DICT.json: never ignore a flag.
_REGEX_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "u": 0, "g": 0}


def regex_flags(flags: str) -> int:
    value = 0
    for char in flags:
        if char not in _REGEX_FLAGS:
            raise ValueError(f"unsupported regex flag {char!r}")
        value |= _REGEX_FLAGS[char]
    return value


def run_dictionary(text: str, rule_files: list[Path]) -> CheckResult:
    names = ", ".join(p.name for p in rule_files if p.is_file()) or "none found"
    result = _check("Dictionary", scope=f"{SCOPES['Dictionary']} ({names})")
    has_cyrillic = bool(re.search(r"[Ѐ-ӿ]", text))
    lines = text.split("\n")
    for rule in load_dictionary_rules(rule_files):
        if rule.get("only_if_cyrillic") and not has_cyrillic:
            continue
        pattern = re.compile(str(rule["pattern"]), regex_flags(str(rule.get("flags", ""))))
        for number, line in enumerate(lines, 1):
            for m in pattern.finditer(line):
                why = f" ({rule['why']})" if rule.get("why") else ""
                result.findings.append(
                    Finding(number, None, str(rule.get("id", "")), f'"{m.group(0)}": use {rule.get("replacement", "?")}{why}')
                )
    return result


def _guarded(name: str, fn: Callable[[], CheckResult]) -> CheckResult:
    """A broken settings file (bad JSON, bad regex, wrong types) fails one check, not the run."""
    try:
        return fn()
    except (OSError, ValueError, TypeError, re.error) as exc:
        return _check(name, "failed", f"{type(exc).__name__}: {exc}")


# ── model review via `review just-ask` ─────────────────────────────────────────────────
_SECTION = re.compile(r"^## (?P<seat>\S+)(?: \[(?P<role>[^\]\n]+)\])? \[(?P<status>[^\]\n]+)\][ \t]*$", re.MULTILINE)
DRAFT_LABEL = "DRAFT"  # prompt.md frames the draft as <<<DRAFT ... DRAFT>>>
# Most specific first: a stack trace also contains "error", but the useful line names the cause.
_FAILURE_KEYS = ("not logged in", "no api key", "not found", "unrecognized", "unauthorized", "revoked", "timed out", "error:", "error")


def parse_just_ask(output: str) -> dict[str, tuple[str, str]]:
    """Map seat -> (status, answer) from a `review just-ask` result file."""
    sections: dict[str, tuple[str, str]] = {}
    heads = list(_SECTION.finditer(output))
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(output)
        body = output[head.end() : end]
        cut = body.rfind("\nstderr:")
        answer = body[:cut] if cut >= 0 else body
        answer = re.sub(r"\A\s*`[^`\n]*`[ \t]*\n", "", answer)  # the echoed command line
        answer = re.sub(r"\n-{3,}\s*\Z", "", answer.rstrip())
        sections.setdefault(head.group("seat"), (head.group("status"), answer.strip()))
    return sections


def draft_label(draft: str) -> str:
    """A fresh marker word framing the draft, so text inside the draft cannot close the frame."""
    while True:
        label = f"{DRAFT_LABEL}-{secrets.token_hex(4)}"
        if label not in draft:
            return label


def parse_answer(answer: str, label: str = DRAFT_LABEL) -> tuple[list[str], str | None]:
    answer = answer.replace("\r\n", "\n")  # a seat may answer with CRLF line ends
    marker = re.escape(label)
    drafts = re.findall(rf"<<<{marker}[ \t]*\n(.*?)\n?{marker}>>>", answer, re.DOTALL)
    revised = drafts[0].strip("\n") + "\n" if drafts else None
    head = answer.split("REVISED DRAFT", 1)[0]
    findings = [
        line.strip()[2:].strip()
        for line in head.split("\n")
        if line.strip().startswith("- ") and line.strip()[2:].strip().lower() not in ("none", "none.")
    ]
    return findings, revised


def _failure_reason(output: str, stderr: str) -> str:
    lines = [line.strip() for line in (output + "\n" + stderr).split("\n") if line.strip()]
    for key in _FAILURE_KEYS:
        for line in lines:
            if key in line.lower():
                return line[:200]
    return "no usable answer"


def ask_slot(
    slot: str, seats: tuple[str, ...], prompt: str, task: str, runner: Runner, which: Which, label: str = DRAFT_LABEL
) -> ModelAnswer:
    answer = ModelAnswer(slot, None, [])
    review = which("review")
    if not review:
        answer.attempts.append(("review", "review CLI is not installed"))
        return answer
    if not seats:
        answer.attempts.append(("-", "no seats given for this slot"))
        return answer
    for seat in seats:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "answer.md"
            cmd = [review, "just-ask", prompt, "--task", task, "-m", seat, "--pool", "1", "-C", tmp, "-o", str(out)]
            proc = runner(cmd, timeout=MODEL_TIMEOUT_S)
            try:
                output = out.read_text(encoding="utf-8") if out.is_file() else (proc.stdout or "")
            except (OSError, UnicodeDecodeError) as exc:
                output = f"error: unreadable answer file: {exc}"
        status, answer_text = parse_just_ask(output).get(seat, ("missing", ""))
        if status == "ok" and answer_text:
            answer.seat = seat
            answer.findings, answer.revised = parse_answer(answer_text, label)
            return answer
        answer.attempts.append((seat, _failure_reason(output, proc.stderr or "")))
    return answer


def parse_slot(value: str, index: int) -> tuple[str, tuple[str, ...]]:
    name, sep, seats = value.partition("=")
    if not sep:
        name, seats = f"Model {index}", value
    return name.strip(), tuple(s.strip() for s in seats.split("|") if s.strip())


def build_prompt(template: str, draft: str, checks: list[CheckResult], label: str = DRAFT_LABEL) -> str:
    lines = [f"- {check.name} L{f.line}: {f.rule}: {f.message}" for check in checks for f in check.findings]
    framed = template.replace(f"<<<{DRAFT_LABEL}", f"<<<{label}").replace(f"{DRAFT_LABEL}>>>", f"{label}>>>")
    values = {"findings": "\n".join(lines) or "- none", "draft": draft.rstrip("\n")}
    # One pass, so a `{draft}` quoted inside a finding is never expanded again.
    return re.sub(r"\{(findings|draft)\}", lambda m: values[m.group(1)], framed)


def review_slots(
    prompt: str, slots: list[tuple[str, tuple[str, ...]]], task: str, runner: Runner, which: Which, label: str = DRAFT_LABEL
) -> list[ModelAnswer]:
    if not slots:
        return []
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        reason = f"draft and findings exceed {MAX_PROMPT_BYTES} bytes, too large for one question"
        return [ModelAnswer(name, None, [("all seats", reason)]) for name, _seats in slots]
    with ThreadPoolExecutor(max_workers=len(slots)) as pool:
        return list(pool.map(lambda s: ask_slot(s[0], s[1], prompt, task, runner, which, label), slots))


# ── fact guard ─────────────────────────────────────────────────────────────────────────
# An inline link destination with up to two levels of balanced parentheses (`(a_(v1))` stays
# whole), optionally in <...> and followed by a "title"; and a reference definition `[id]: url`.
_LINK_TARGET = re.compile(
    r"\]\(\s*<?((?:[^()\s<>]|\((?:[^()\s]|\([^()\s]*\))*\))+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)"
)
_LINK_DEFINITION = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?([^\s>]+)>?")
_NUMBER = re.compile(r"(?<![\w.])[-+−]?\d+(?:[.,:]\d+)*")  # the sign is part of the fact


def _bare_url(url: str) -> str:
    url = url.rstrip(".,;:>")  # sentence punctuation, the end of an <autolink>
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1]
    return url


def fact_tokens(markdown: str) -> list[tuple[str, int, str]]:
    """Every code span, fenced code line, link and number as (token, line, kind), repeats included."""
    tokens: list[tuple[str, int, str]] = []
    for number, line, kind in _fence_lines(markdown):
        if kind == "code" and line.strip():
            tokens.append((line.strip(), number, "code"))
        if kind != "text":
            continue
        tokens += [(m.group(0), number, "code") for m in _INLINE_CODE.finditer(line)]
        prose = _INLINE_CODE.sub(" ", line)
        tokens += [(t, number, "link") for t in _LINK_TARGET.findall(prose) + _LINK_DEFINITION.findall(prose)]
        prose = _LINK_DEFINITION.sub("", _LINK_TARGET.sub("]()", prose))
        tokens += [(_bare_url(u), number, "link") for u in _URL.findall(prose)]
        tokens += [(t, number, "number") for t in _NUMBER.findall(_URL.sub(" ", prose))]
    return tokens


def run_fact_guard(original: str, revised: str | None) -> CheckResult:
    """Exact tokens, counted: a number or link that changed (12 -> 123, /a -> /a-2) or one of two
    identical spans that disappeared counts as dropped."""
    if revised is None:
        return _check("Fact guard", "skipped", "no revised draft")
    result = _check("Fact guard")
    kept = Counter((token, kind) for token, _line, kind in fact_tokens(revised))
    wanted = Counter((token, kind) for token, _line, kind in fact_tokens(original))
    first_line = {}
    for token, line, kind in fact_tokens(original):
        first_line.setdefault((token, kind), line)
    for key, count in wanted.items():
        lost = count - kept.get(key, 0)
        if lost > 0:
            extra = f" ({lost} of {count})" if count > 1 else ""
            result.findings.append(Finding(first_line[key], None, "missing", f"not in the revised draft{extra}: {key[0]}"))
    return result


def choose_revision(original: str, answers: list[ModelAnswer]) -> tuple[ModelAnswer | None, CheckResult]:
    """Pick the revised draft to write, and its fact-guard result.

    Order: an edit that keeps every guarded token, then a clean "nothing to fix" answer, then
    an edit that dropped a token (the caller withholds that one and reports why).
    """
    guarded = [(a, run_fact_guard(original, a.revised)) for a in answers if a.revised]
    if not guarded:
        return None, run_fact_guard(original, None)
    clean = [(a, g) for a, g in guarded if not g.findings]
    clean_edits = [(a, g) for a, g in clean if a.revised.strip() != original.strip()]
    return (clean_edits or clean or guarded)[0]


# ── report ─────────────────────────────────────────────────────────────────────────────
def _cell(text: str) -> str:
    return text.replace("\n", " ").replace("|", "\\|")


def _result_cell(check: CheckResult) -> str:
    if check.status != "ok":
        return _cell(f"{check.status}: {check.note}")
    return f"{len(check.findings)} finding(s)" if check.findings else "clean"


def _where(f: Finding) -> str:
    return f"L{f.line}:{f.column}" if f.column is not None else f"L{f.line}"


@dataclass(frozen=True)
class Outcome:
    """What happened to the revised draft, for the report's opening lines."""

    checked_name: str  # the <stem>.checked.md name
    written: ModelAnswer | None = None  # the revision written to checked_name
    withheld: ModelAnswer | None = None  # the best revision, not written: it dropped guarded facts
    older_file: bool = False  # checked_name already exists and this run did not write it


def render_report(name: str, language: str, checks: list[CheckResult], answers: list[ModelAnswer], outcome: Outcome) -> str:
    lang = {"en": "English", "ru": "Russian"}[language]
    out = [f"# Text check: {name}", ""]
    rule_based = sum(len(c.findings) for c in checks if c.name != "Fact guard")
    summary = f"Language: {lang}. Rule-based findings: {rule_based}."
    for a in answers:
        summary += f" {a.slot}: " + (f"{len(a.findings)} suggestion(s)." if a.seat else "no answer.")
    out += [summary, ""]
    if outcome.written:
        out += [f"Revised draft: `{outcome.checked_name}` (from {outcome.written.slot}, seat `{outcome.written.seat}`).", ""]
    elif outcome.withheld:
        guard = next((c for c in checks if c.name == "Fact guard"), None)
        dropped = len(guard.findings) if guard else 0
        out += [
            f"Revised draft: withheld. The best revision ({outcome.withheld.slot}) dropped {dropped} fact token(s), "
            "listed under Fact guard; the suggestions below are still usable.",
            "",
        ]
    else:
        out += ["Revised draft: none (no seat returned one).", ""]
    if outcome.older_file:
        out += [f"`{outcome.checked_name}` is from an earlier run or another tool; it does not reflect this draft.", ""]
    out += ["| Check | What it checks | Result |", "| --- | --- | --- |"]
    for check in checks:
        out.append(f"| {check.name} | {_cell(check.scope)} | {_result_cell(check)} |")
    for a in answers:
        cell = f"{len(a.findings)} suggestion(s), seat `{a.seat}`" if a.seat else "no answer (see below)"
        out.append(f"| Model review: {a.slot} | {MODEL_SCOPE} | {cell} |")
    for check in checks:
        if not check.findings:
            continue
        out += ["", f"## {check.name}", ""]
        out += [f"- {_where(f)} `{f.rule}` {f.message}" for f in sorted(check.findings, key=lambda f: (f.line, f.column or 0))]
    for a in answers:
        out += ["", f"## Model review: {a.slot}", ""]
        if a.attempts:
            out += ["Seats that did not answer: " + "; ".join(f"`{s}` ({r})" for s, r in a.attempts) + ".", ""]
        if a.seat:
            out += [f"Seat: `{a.seat}`.", ""]
            out += [f"- {line}" for line in a.findings] or ["- none"]
    return "\n".join(out).rstrip() + "\n"


# ── entry point ────────────────────────────────────────────────────────────────────────
def _catalog_settings(cwd: Path) -> Path:
    from .config import load

    top = git_toplevel(cwd)
    loaded = load(top or cwd, include_repo=top is not None)
    try:
        return prose_carrier(resolve_source(loaded.agent_tools_source))
    except CatalogError as exc:
        raise MissingTargetError(
            what="prose lint settings not found",
            why=str(exc),
            fix="update the agent-tools checkout (git -C <agent-tools> pull) or set agent_tools_source",
        ) from exc


def _explicit_settings(path: Path) -> Path:
    try:
        return validate_prose_dir(path)
    except CatalogError as exc:
        raise MissingTargetError(what=f"not a prose settings directory: {path}", why=str(exc), fix="pass agent-tools linters/prose or a copy of it") from exc


def check_file(
    path: Path,
    carrier: Path,
    slots: list[tuple[str, tuple[str, ...]]],
    task: str,
    out_dir: Path,
    *,
    runner: Runner = default_runner,
    which: Which | None = None,
    cache_dir: Path | None = None,
) -> tuple[Path, Path | None, str]:
    which = which or shutil.which
    markdown = path.read_text(encoding="utf-8")
    text = plain_text(markdown)
    language = detect_language(text)
    cache = cache_dir or Path(os.path.expanduser(CACHE_DIR))
    dictionaries = [Path(os.path.expanduser(USER_DICTIONARY))] + sorted((carrier / "dict").glob("*.json"))
    checks = [
        _guarded("Vale", lambda: run_vale(path, language, carrier, cache, runner, which)),
        _guarded("slopless", lambda: run_slopless(path, language, runner, which)),
        _guarded("LanguageTool", lambda: run_languagetool(markdown, text, language, carrier, runner, which)),
        _guarded("Dictionary", lambda: run_dictionary(text, dictionaries)),
    ]
    answers: list[ModelAnswer] = []
    if slots:
        label = draft_label(markdown)
        try:
            template = (carrier / "prompt.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            answers = [ModelAnswer(name, None, [("prompt.md", f"unreadable: {exc}")]) for name, _seats in slots]
        else:
            answers = review_slots(build_prompt(template, markdown, checks, label), slots, task, runner, which, label)
    best, guard = choose_revision(markdown, answers)
    checks.append(guard)
    report_path, checked = output_paths(path, out_dir)
    # A revision that lost a code span, link or number is not a "checked" draft: withhold it.
    written = best if best and not guard.findings else None
    outcome = Outcome(
        checked.name,
        written=written,
        withheld=best if best and not written else None,
        older_file=not written and checked.exists(),  # never delete it: the file may be the user's
    )
    report = render_report(path.name, language, checks, answers, outcome)
    _write_output(report_path, report)
    if written and written.revised:
        _write_output(checked, written.revised)
    return report_path, checked if written else None, report


def _write_output(path: Path, text: str) -> None:
    """Write a report or revised draft; never through a symlink, which could point anywhere."""
    if path.is_symlink():
        raise ConfigError(what=f"{path} is a symlink; refusing to write through it", fix="remove the link or pass --out-dir")
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise MissingTargetError(what=f"cannot write {path}", why=str(exc), fix="pass a writable --out-dir") from exc


def output_paths(path: Path, out_dir: Path) -> tuple[Path, Path]:
    """(<stem>.report.md, <stem>.checked.md) for a draft; `notes.md` and `notes` share a stem."""
    stem = path.name.removesuffix(".md")
    return out_dir / f"{stem}.report.md", out_dir / f"{stem}.checked.md"


def _plan_outputs(files: list[Path], out_dir: str | None) -> list[tuple[Path, Path]]:
    """(draft, output dir) pairs; refuses two drafts that would write the same report."""
    plan: list[tuple[Path, Path]] = []
    seen: dict[Path, Path] = {}
    for path in files:
        if path.name.endswith(OUTPUT_SUFFIXES):
            print(f"skip {path.name}: a text-check output, not a draft")
            continue
        target = Path(out_dir).expanduser().resolve() if out_dir else path.parent
        report, _checked = output_paths(path, target)
        if report in seen:
            raise ConfigError(
                what=f"{seen[report]} and {path} would write the same report in {target}",
                fix="check them in separate runs or drop --out-dir",
            )
        seen[report] = path
        plan.append((path, target))
    return plan


def run(args) -> int:
    files = [Path(f).expanduser().resolve() for f in args.files]
    for f in files:
        if not f.is_file():
            raise MissingTargetError(what=f"no such file: {f}", fix="pass the path of a Markdown draft")
        try:
            f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise MissingTargetError(what=f"cannot read {f} as UTF-8 text", why=str(exc), fix="save the draft as UTF-8 Markdown") from exc
    plan = _plan_outputs(files, args.out_dir)
    if not plan:
        return 0
    cwd = Path(args.cwd).expanduser().resolve()
    carrier = _explicit_settings(Path(args.settings).expanduser().resolve()) if args.settings else _catalog_settings(cwd)
    for _path, target in plan:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MissingTargetError(what=f"cannot create output directory {target}", why=str(exc), fix="pass a writable --out-dir") from exc
    if args.no_models:
        slots: list[tuple[str, tuple[str, ...]]] = []
    elif args.models:
        slots = [parse_slot(value, i) for i, value in enumerate(args.models, 1)]
    else:
        slots = list(DEFAULT_SLOTS)
    for path, target in plan:
        report_path, checked_path, report = check_file(path, carrier, slots, args.task, target)
        table = [line for line in report.split("\n") if line.startswith("|")]
        print(path.name)
        print("\n".join(table))
        print(f"report:  {report_path}")
        print(f"revised: {checked_path if checked_path else 'none'}\n")
    return 0
