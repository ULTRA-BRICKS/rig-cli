"""PreToolUse removal guard — decide allow / deny / pass for ONE bash command.

The agent-tools ``block-unlanded-removal`` hook hands every bash command that smells like a
removal to ``rig worktree guard`` (this module). The guard recognizes exactly these removal shapes —
``git worktree remove [--force] <path>``, ``git branch -d|-D <name>…``, and ``git push <remote>
--delete <name>…`` / ``git push <remote> :<name>`` — and runs the landed-removal verdict
(:mod:`riglib.removal_verdict`) on EVERY target: all SAFE → ``allow`` (logged), any UNSAFE → ``deny``
with the reasons and the sanctioned rescue, anything that is not a removal → ``pass`` (the hook
stays silent). ``--force`` / ``-f`` never changes the answer: it only silences git's own dirty-tree
check, which is exactly what the verdict exists to make unnecessary.

Parsing is deliberately conservative: a command is judged only when it is ONE simple command whose
every relevant word is a plain literal. Anything else that contains a removal — a chain (``;`` ``&&``
``||`` ``|`` ``&``, newline), a subshell or group, ``$(…)``/backticks, ``bash -c``/``eval``/``xargs``/
``sudo``/``env``/an assignment prefix, a variable, glob, brace or tilde in a target, ``--git-dir``/
``-c``/``--work-tree``, a subcommand or flag assembled from an expansion, a symlinked or unknown
worktree path, ``push --mirror/--prune`` — is DENIED
with the reason, never guessed. Words inside quotes and heredoc bodies that merely MENTION a removal
(a commit message, a PR body, an ``echo``) are data, not commands, and pass. ``git update-ref -d
refs/heads/<b>`` (or ``HEAD``, an empty new value, ``--stdin``) deletes a branch with no per-target
verdict, so it is denied with a pointer to ``git branch -D`` rather than judged.

Two more shapes lose work without being a deletion and are judged by the same verdict
(rig-cli#740). **Overwriting a local branch** — ``git branch -f``/``-M``/``-C`` (and ``-m``/``-c`` with
``-f``/``--force``), ``git checkout -B``, ``git switch -C``/``--force-create``: the DESTINATION branch's
current tip must be landed or held elsewhere (:func:`riglib.removal_verdict.overwrite_verdict`; a
fast-forward, a brand-new branch and a landed tip pass). **``rm -r <path>``** of a linked worktree or of a
directory containing one: each literal operand (unquoted globs expanded, ``~``/``$HOME`` resolved, symlinks
followed) is compared with the existing linked worktrees of the cwd's repository, of the repository the
operand sits in and of any repository a bounded scan finds inside it; every worktree that is or sits under
an operand gets the worktree verdict. Unlike a deletion these are judged PER COMMAND inside a chain (they
are everyday commands), but a ``cd``/``pushd`` anywhere in the command makes relative paths and the
cwd-resolved repository unknowable (denied). Operands from an expansion, ``xargs``/``find``/``parallel``-fed
``rm -r`` and ``bash -c``/``eval``/``ssh`` strings are denied. NOT judged: ``rm -r`` of a sub-directory OF a
worktree (ordinary editing) and of a primary checkout or a directory whose worktrees are all SAFE (the
primary's own local-only branches, stashes and untracked files are not examined), and any target too large
for the scan caps.

Known limits (heuristic hook, not a sandbox): a removal hidden behind something this lexer cannot
see — a script file the command runs, a git alias, ``python -c``, an interpreter other than the
listed executors — is not caught; ``git push --force`` can also overwrite a REMOTE branch without
being a deletion and is out of scope here (a separate guard owns it); a ``find -delete``, ``rsync
--delete``, ``mv`` or ``shutil.rmtree`` of a worktree is not seen at all (agent-tools#966);
``git branch -r -d`` deletes only a remote-tracking ref, which loses nothing the remote lacks, and
passes. A ``git push`` whose ``--delete`` or ``:name`` refspec arrives inside a variable
(``git push origin "$D" x`` with ``D=--delete``, or ``x$F`` with ``F=' --delete'``) is not caught: refusing every pushed variable would
block ordinary ``git push origin "$BRANCH"``. (``git branch`` has no such everyday variable use, so a
bare expansion among its arguments IS refused.)
"""

from __future__ import annotations

import glob
import json
import os
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from . import removal_verdict as rv
from . import worktree_gc

_MENTION_RE = re.compile(
    r"worktree\s+(?:remove|rm)\b"
    r"|\bbranch\b.*?(?:\s--d|\s-[A-Za-z]*[dD][A-Za-z]*\b)"
    r"|\bbranch\b.*?(?:\s--(?:forc|mo|cop)|\s-[A-Za-z]*[fMC][A-Za-z]*\b)"  # overwrite: -f/--force, -M, -C
    r"|\bcheckout\b.*?\s-[A-Za-z]*B"  # checkout -B
    r"|\bswitch\b.*?(?:\s-[A-Za-z]*C|\s--force-c)"  # switch -C / --force-create
    r"|(?<![\w.-])rm\b[^\n]*?\s(?:-[A-Za-z]*[rR]|--r)"  # a recursive rm
    r"|\bpush\b.*?(?:--d|--mi|--pr|\s-[A-Za-z]*d[A-Za-z]*\b|\s\+?:\S)"
    r"|\bupdate-ref\b",  # every update-ref is analyzed: a deletion can hide in `-d`, `--stdin` or an empty value
    re.S,
)
_QUOTE_CHARS = re.compile(r"['\"\\]")
_ANSI_C_QUOTE = re.compile(r"\$(?=['\"])")
_EXPANSION_RE = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\$[0-9@*#?!$-]")
_ANSI_C_STRING = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.S)
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_EXECUTORS = frozenset(
    {"bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "eval", "exec", "source", ".", "ssh", "su", "doas"}
)
# Commands that run another command line for you: a git behind one of these is parsed like a direct one.
_WRAPPERS = frozenset(
    {
        "sudo", "doas", "env", "xargs", "nice", "ionice", "time", "command", "builtin", "nohup", "timeout", "find",
        "watch", "stdbuf", "setsid", "parallel", "flock", "caffeinate", "systemd-run", "taskset", "chrt", "chronic",
        "unbuffer", "firejail", "strace", "nsenter", "runuser", "script",
    }
)
# Shell keywords that precede a command on the same segment (`then git …`, `! git …`).
_KEYWORD_HEADS = frozenset({"then", "do", "else", "elif", "while", "until", "if", "!"})
_GIT_RUNNERS = _WRAPPERS | _KEYWORD_HEADS
_SUBCOMMANDS = frozenset({"branch", "push", "worktree", "update-ref", "checkout", "switch"})
_SUBCOMMAND_RE = re.compile(r"\b(?:" + "|".join(sorted(_SUBCOMMANDS)) + r")\b")
_GIT_VALUE_GLOBALS = frozenset({"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix", "--config-env", "--attr-source"})
_GIT_PLAIN_GLOBALS = frozenset({"--no-pager", "-P", "--paginate", "-p", "--no-optional-locks", "--no-replace-objects", "--literal-pathspecs"})
_PUSH_VALUE_FLAGS = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
# Shortest abbreviation of each long option the parsers treat as that option. git parse-options
# accepts any UNAMBIGUOUS prefix, so this errs wide: `--d` is `--delete` for `git branch` (and where
# it is ambiguous git refuses the command anyway). `_MENTION_RE` above must cover the same prefixes
# (tests/test_removal_guard.py pins that), because it decides which commands reach the parsers.
_LONG_OPTIONS = {
    "--delete": 3, "--mirror": 4, "--prune": 4, "--remotes": 4, "--stdin": 4, "--repo": 5,
    "--force": 6, "--move": 4, "--copy": 5, "--recursive": 3, "--force-create": 9,
}
_FEEDERS = frozenset({"xargs", "find", "parallel"})  # run another command on operands they produce themselves
_JOINING_EXECUTORS = frozenset({"eval", "ssh"})  # hand their separate arguments to a shell as ONE command line


def _is_long_option(token: str, full: str) -> bool:
    """True when ``token`` spells long option ``full`` or an accepted abbreviation of it."""
    name = token.split("=", 1)[0]
    return name.startswith("--") and len(name) >= _LONG_OPTIONS.get(full, len(full)) and full.startswith(name)


def _raw_line(words: list[_Word]) -> str:
    return " ".join(w.raw for w in words)


def _text_line(words: list[_Word]) -> str:
    """The words' values as far as they are known ($'…' decoded; an expansion stays as ``$…``)."""
    return " ".join(w.text for w in words)


_BRACE_WORD_RE = re.compile(r"\{[^{}\s]*[,][^{}\s]*\}|\{[^{}\s]*\.\.[^{}\s]*\}")  # {a,b} / {1..3} brace expansion


def _flatten(text: str) -> str:
    """``text`` with quotes/backslashes stripped, ``$'…'``/``$"…"`` unwrapped and every ``$NAME`` /
    ``${…}`` replaced by a space — the form the mention regex reads."""
    decoded = _ANSI_C_STRING.sub(lambda m: _decode_ansi_c(m.group(1)), text)  # `$'\\x2dD'` -> `-D`
    return _EXPANSION_RE.sub(" ", _QUOTE_CHARS.sub("", _ANSI_C_QUOTE.sub("", decoded)))


def mentions_removal(text: str) -> bool:
    """Cheap superset test: could ``text`` contain a worktree/branch removal? Quotes and backslashes
    are stripped first (so ``g''it``-style splitting cannot dodge it), ``$'-D'`` becomes ``-D`` and
    every ``$NAME`` / ``${…}`` becomes a space (``git${IFS}branch${IFS}-D`` → ``git branch -D``)."""
    return _MENTION_RE.search(_flatten(text)) is not None


def _parse_error_is_suspicious(command: str) -> bool:
    """An unparseable command stays DENIED when it mentions a removal, or when an expansion could
    assemble one (an expansion next to a git subcommand word)."""
    if mentions_removal(command):
        return True
    expands = "$" in command or "`" in command or _BRACE_WORD_RE.search(command) is not None
    return expands and _SUBCOMMAND_RE.search(_flatten(command)) is not None


def needs_parse(command: str) -> bool:
    """False only when ``command`` provably cannot contain a removal: no mention after normalization
    AND no expansion that could assemble one (``$…``, backticks)."""
    return mentions_removal(command) or "$" in command or "`" in command or _BRACE_WORD_RE.search(command) is not None


# ── result type ──────────────────────────────────────────────────────────────────
Decision = Literal["pass", "allow", "deny"]


@dataclass(frozen=True)
class GuardDecision:
    decision: Decision
    reason: str = ""
    verdicts: tuple[rv.Verdict, ...] = ()

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "targets": [
                {
                    "kind": v.kind,
                    "target": v.target,
                    "safe": v.safe,
                    "landed_by": v.landed_by,
                    "reasons": [r.code for r in v.reasons],
                }
                for v in self.verdicts
            ],
        }


class _Deny(Exception):
    """The command contains a removal (or might) that cannot be judged with certainty."""


class _ParseError(Exception):
    pass


# ── lexer ────────────────────────────────────────────────────────────────────────
@dataclass
class _Word:
    text: str = ""
    raw: str = ""
    dynamic: bool = False  # $var, $(…), `…`, brace expansion, tilde — value unknowable pre-expansion
    glob: bool = False  # unquoted * ? [
    subs: list[str] = field(default_factory=list)  # source of every $(…) / `…` / <(…) inside the word


@dataclass
class _Segment:
    words: list[_Word] = field(default_factory=list)
    heredocs: list[str] = field(default_factory=list)
    heredoc_subs: list[str] = field(default_factory=list)  # $(…)/`…` bodies of UNQUOTED heredocs
    redirect_words: list[_Word] = field(default_factory=list)  # `> file`, `<<< text`, `< <(cmd)` targets


@dataclass
class _Parsed:
    segments: list[_Segment]
    grouped: bool  # a pipe/background/subshell/group/&&/|| construct was seen


_ANSI_C_SIMPLE = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "e": "\x1b", "f": "\f", "v": "\v", "\\": "\\", "'": "'", '"': '"'}


def _decode_ansi_c(body: str) -> str:
    """Value of a ``$'…'`` string (escapes ``\\n \\t \\xHH \\NNN \\\\ \\'`` …): the shell expands it to
    plain literal text BEFORE running the command, so ``$'-D'`` is just ``-D``."""
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in _ANSI_C_SIMPLE:
            out.append(_ANSI_C_SIMPLE[nxt])
            i += 2
        elif nxt in "xuU":
            width = {"x": 2, "u": 4, "U": 8}[nxt]
            j = 0
            while j < width and i + 2 + j < n and body[i + 2 + j] in "0123456789abcdefABCDEF":
                j += 1
            if j == 0:
                out.append("\\" + nxt)  # bash keeps a malformed escape literally
                i += 2
            else:
                try:
                    out.append(chr(int(body[i + 2 : i + 2 + j], 16)))
                except (ValueError, OverflowError):
                    out.append("\\" + nxt + body[i + 2 : i + 2 + j])
                i += 2 + j
        elif nxt == "c" and i + 2 < n:
            out.append(chr(ord(body[i + 2]) & 0x1F))
            i += 3
        elif nxt in "01234567":
            digits = body[i + 1 : i + 4]
            j = 0
            while j < len(digits) and digits[j] in "01234567":
                j += 1
            out.append(chr(int(digits[:j], 8) & 0xFF))
            i += 1 + j
        else:
            out.append("\\" + nxt)
            i += 2
    return "".join(out)


def _read_heredoc_delim(s: str, i: int) -> tuple[str, bool, bool, int]:
    """Parse the ``<<[-]DELIM`` operator starting at ``i`` (``s[i:i+2] == "<<"``): returns
    ``(delimiter, strip_leading_tabs, delimiter_was_quoted, index_after_the_delimiter_word)``. A
    QUOTED delimiter makes the body literal data; an unquoted one lets the shell expand it."""
    n = len(s)
    j = i + 2
    strip_tabs = j < n and s[j] == "-"
    if strip_tabs:
        j += 1
    while j < n and s[j] in " \t":
        j += 1
    delim = ""
    quoted = False
    while j < n and s[j] not in " \t\n;&|()<>":
        if s[j] in "'\"\\":
            quoted = True
            j += 1
            continue
        delim += s[j]
        j += 1
    if not delim:
        raise _ParseError("heredoc without a delimiter")
    return delim, strip_tabs, quoted, j


def _consume_heredoc_body(s: str, i: int, delim: str, strip_tabs: bool) -> tuple[str, int]:
    """Body text (without the terminator line) of a heredoc whose first body line starts at ``i``,
    and the index just past the terminator line."""
    n = len(s)
    body: list[str] = []
    while True:
        if i >= n:
            raise _ParseError("unterminated heredoc")
        end = s.find("\n", i)
        line = s[i:] if end < 0 else s[i:end]
        i = n if end < 0 else end + 1
        if (line.lstrip("\t") if strip_tabs else line) == delim:
            return "\n".join(body), i
        body.append(line)


def _body_substitutions(text: str) -> list[str]:
    """Source of every command substitution (``$(…)`` / backticks, also inside ``${…}``) that the
    shell would run when it expands ``text`` — an unquoted heredoc body or a ``${…}`` expansion."""
    found: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
        elif c == "$" and text.startswith("$(", i):
            end = _scan_balanced(text, i + 2, "(", ")")
            found.append(text[i + 2 : end - 1])
            i = end
        elif c == "$" and text.startswith("${", i):
            end = _scan_balanced(text, i + 2, "{", "}")
            found.extend(_body_substitutions(text[i + 2 : end - 1]))
            i = end
        elif c == "`":
            end = text.find("`", i + 1)
            if end < 0:
                raise _ParseError("unterminated backtick")
            found.append(text[i + 1 : end])
            i = end + 1
        else:
            i += 1
    return found


def _scan_balanced(s: str, i: int, open_ch: str, close_ch: str) -> int:
    """Index just past the ``close_ch`` matching the ``open_ch`` that precedes ``i``. Quoted
    strings and heredoc bodies inside are skipped (a commit message's apostrophe must not end the
    scan). Unterminated → :class:`_ParseError`."""
    depth, n = 1, len(s)
    pending: list[tuple[str, bool]] = []
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "\n" and pending:
            i += 1
            for delim, strip_tabs in pending:
                _, i = _consume_heredoc_body(s, i, delim, strip_tabs)
            pending = []
            continue
        if c == "<" and s.startswith("<<", i) and not s.startswith("<<<", i):
            delim, strip_tabs, _quoted, i = _read_heredoc_delim(s, i)
            pending.append((delim, strip_tabs))
            continue
        if c in "'\"":
            end = s.find(c, i + 1) if c == "'" else _find_dquote_end(s, i + 1)
            if end < 0:
                raise _ParseError("unterminated quote")
            i = end + 1
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise _ParseError(f"unbalanced {open_ch}")


def _find_dquote_end(s: str, i: int) -> int:
    n = len(s)
    while i < n:
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == '"':
            return i
        i += 1
    return -1


def _tokenize(s: str) -> _Parsed:  # noqa: C901 — one flat state machine reads clearer than helpers
    segments: list[_Segment] = [_Segment()]
    grouped = False
    cur: _Word | None = None
    skip_next_word = False  # the word after a redirection operator is a file, not an argument
    pending_heredocs: list[tuple[str, bool, bool, _Segment]] = []
    i, n = 0, len(s)

    def start() -> _Word:
        nonlocal cur
        if cur is None:
            cur = _Word()
        return cur

    def flush() -> None:
        nonlocal cur, skip_next_word
        if cur is not None:
            if skip_next_word:
                skip_next_word = False
                segments[-1].redirect_words.append(cur)
            else:
                segments[-1].words.append(cur)
            cur = None

    def new_segment() -> None:
        flush()
        segments.append(_Segment())

    while i < n:
        c = s[i]
        if c in " \t":
            flush()
            i += 1
        elif c == "\n":
            new_segment()
            i += 1
            if pending_heredocs:
                for delim, strip_tabs, quoted, owner in pending_heredocs:
                    body_text, i = _consume_heredoc_body(s, i, delim, strip_tabs)
                    owner.heredocs.append(body_text)
                    if not quoted:
                        owner.heredoc_subs.extend(_body_substitutions(body_text))
                pending_heredocs = []
        elif c == "#" and cur is None:
            end = s.find("\n", i)
            i = n if end < 0 else end
        elif c == "'":
            end = s.find("'", i + 1)
            if end < 0:
                raise _ParseError("unterminated single quote")
            w = start()
            w.text += s[i + 1 : end]
            w.raw += s[i : end + 1]
            i = end + 1
        elif c == '"':
            w = start()
            j = i + 1
            w.raw += '"'
            while True:
                if j >= n:
                    raise _ParseError("unterminated double quote")
                d = s[j]
                if d == '"':
                    w.raw += '"'
                    j += 1
                    break
                if d == "\\" and j + 1 < n:
                    nxt = s[j + 1]
                    w.raw += d + nxt
                    w.text += nxt if nxt in '$`"\\' else d + nxt
                    j += 2
                elif d == "$" and j + 1 < n and s[j + 1] == "(":
                    end = _scan_balanced(s, j + 2, "(", ")")
                    w.dynamic = True
                    w.subs.append(s[j + 2 : end - 1])
                    w.raw += s[j:end]
                    w.text += s[j:end]
                    j = end
                elif d == "$" and j + 1 < n and s[j + 1] == "{":
                    end = _scan_balanced(s, j + 2, "{", "}")
                    w.dynamic = True
                    w.subs.extend(_body_substitutions(s[j:end]))
                    w.raw += s[j:end]
                    w.text += s[j:end]
                    j = end
                elif d == "`":
                    end = s.find("`", j + 1)
                    if end < 0:
                        raise _ParseError("unterminated backtick")
                    w.dynamic = True
                    w.subs.append(s[j + 1 : end])
                    w.raw += s[j : end + 1]
                    w.text += s[j : end + 1]
                    j = end + 1
                elif d == "$" and j + 1 < n and (s[j + 1].isalnum() or s[j + 1] in "_@*#?!$-"):
                    w.dynamic = True
                    w.raw += d
                    w.text += d
                    j += 1
                else:
                    w.raw += d
                    w.text += d
                    j += 1
            i = j
        elif c == "\\":
            if i + 1 < n and s[i + 1] == "\n":
                i += 2
            elif i + 1 < n:
                w = start()
                w.text += s[i + 1]
                w.raw += s[i : i + 2]
                i += 2
            else:
                i += 1
        elif c == "$" and i + 1 < n and s[i + 1] == '"':
            i += 1  # locale-quoted `$"…"` is plain double-quoted text: drop the `$` (so `raw` omits it) and let the quote branch read it
        elif c == "$" and i + 1 < n and s[i + 1] == "'":
            end = i + 2
            while end < n and s[end] != "'":
                end += 2 if s[end] == "\\" else 1
            if end >= n:
                raise _ParseError("unterminated $'...' quote")
            w = start()
            w.text += _decode_ansi_c(s[i + 2 : end])
            w.raw += s[i : end + 1]
            i = end + 1
        elif c == "$":
            w = start()
            if i + 1 < n and s[i + 1] == "(":
                end = _scan_balanced(s, i + 2, "(", ")")
                w.subs.append(s[i + 2 : end - 1])
            elif i + 1 < n and s[i + 1] == "{":
                end = _scan_balanced(s, i + 2, "{", "}")
                w.subs.extend(_body_substitutions(s[i:end]))
            else:
                end = i + 1
                while end < n and (s[end].isalnum() or s[end] == "_"):
                    end += 1
                if end == i + 1 and i + 1 < n and s[i + 1] in "@*#?!$-0123456789":
                    end = i + 2
            w.dynamic = True
            w.text += s[i:end]
            w.raw += s[i:end]
            i = end
        elif c == "`":
            end = s.find("`", i + 1)
            if end < 0:
                raise _ParseError("unterminated backtick")
            w = start()
            w.dynamic = True
            w.subs.append(s[i + 1 : end])
            w.text += s[i : end + 1]
            w.raw += s[i : end + 1]
            i = end + 1
        elif c in "<>" and i + 1 < n and s[i + 1] == "(":
            end = _scan_balanced(s, i + 2, "(", ")")
            w = start()
            w.subs.append(s[i + 2 : end - 1])
            w.dynamic = True
            w.text += s[i:end]
            w.raw += s[i:end]
            i = end
        elif c == "<" and s.startswith("<<", i) and not s.startswith("<<<", i):
            flush()
            delim, strip_tabs, quoted, i = _read_heredoc_delim(s, i)
            pending_heredocs.append((delim, strip_tabs, quoted, segments[-1]))
        elif c in "<>":
            if cur is not None and cur.text.isdigit() and not cur.dynamic:
                cur = None  # `2>` — the digit is a file descriptor, not an argument
            else:
                flush()
            j = i + 1
            while j < n and s[j] in "<>&|":
                j += 1
            skip_next_word = True
            i = j
        elif c == "&":
            if i + 1 < n and s[i + 1] == ">":
                flush()
                skip_next_word = True
                i += 2 + (1 if i + 2 < n and s[i + 2] == ">" else 0)
            else:
                new_segment()
                grouped = True
                i += 2 if i + 1 < n and s[i + 1] == "&" else 1
        elif c == "|":
            new_segment()
            grouped = True
            i += 2 if i + 1 < n and s[i + 1] in "|&" else 1
        elif c == ";":
            new_segment()
            i += 2 if i + 1 < n and s[i + 1] == ";" else 1
        elif c in "()":
            new_segment()
            grouped = True
            i += 1
        elif c in "{}":
            if cur is None and (i + 1 >= n or s[i + 1] in " \t\n"):
                new_segment()
                grouped = True
            else:
                w = start()
                w.dynamic = True
                w.text += c
                w.raw += c
            i += 1
        else:
            w = start()
            if c in "*?[":
                w.glob = True
            if c == "~" and not w.raw:
                w.dynamic = True
            w.text += c
            w.raw += c
            i += 1
    flush()
    return _Parsed([seg for seg in segments if seg.words or seg.heredocs], grouped)


# ── segment analysis ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Item:
    kind: str  # an rv.TargetKind, or "rm-path" (a path a recursive `rm` deletes)
    name: str
    remote: str = ""
    start: str = ""  # overwrite only: the revision the branch is re-pointed at ("" = unknown)
    overwrite: bool = False  # a local branch that is RE-POINTED (branch -f/-M/-C, checkout -B) rather than deleted
    glob: bool = False  # rm-path only: the operand carries an unquoted glob


@dataclass(frozen=True)
class _Removal:
    items: tuple[_Item, ...]
    c_dirs: tuple[str, ...]

    @property
    def in_place(self) -> bool:
        """Overwrites and `rm -r` are judged per command, even inside a chain (see ``_evaluate_in_place``);
        a deletion is judged only as ONE simple command."""
        return all(i.overwrite or i.kind == "rm-path" for i in self.items)


def _plain(word: _Word, what: str) -> str:
    if word.dynamic:
        raise _Deny(f"{what} {word.raw!r} contains unexpanded shell syntax ($var, $(…), `…`, brace or ~ expansion) — cannot know its value")
    if word.glob:
        raise _Deny(f"{what} {word.raw!r} contains a glob — name each target explicitly")
    return word.text


def _branch_name(raw: str) -> str | None:
    """Branch name out of a push refspec / delete argument, or None for a non-branch ref (tag…)."""
    name = raw.lstrip("+")
    if name.startswith(":"):
        name = name[1:]
    if name.startswith("refs/heads/"):
        return name[len("refs/heads/") :]
    if name.startswith("refs/"):
        return None
    return name


def _split_args(
    args: list[_Word], value_flags: frozenset[str] = frozenset(), short_value_letters: str = ""
) -> tuple[list[_Word], list[_Word]]:
    """``(positionals, flags)``: ``--`` ends flag parsing once; a flag in ``value_flags`` swallows
    the next word (its value); a bare ``-`` is positional. In a short cluster, a letter in
    ``short_value_letters`` takes the REST of the cluster as its value (``-od`` is ``-o d``), or the
    next word when it is last — the letters after it are not flags."""
    positionals: list[_Word] = []
    flags: list[_Word] = []
    only_positionals = skip = False
    for w in args:
        if skip:
            skip = False
        elif only_positionals or not w.text.startswith("-") or w.text == "-":
            positionals.append(w)
        elif w.text == "--":
            only_positionals = True
        elif not w.text.startswith("--") and any(c in short_value_letters for c in w.text[1:]):
            cut = min(w.text.index(c, 1) for c in short_value_letters if c in w.text[1:])
            rest = w.text[cut + 1 :]
            flags.append(_Word(w.text[: cut + 1], w.raw, w.dynamic, w.glob, w.subs))
            skip = rest == ""
        else:
            flags.append(w)
            skip = w.text in value_flags
    return positionals, flags


# `git branch` flags that state a mode other than "create"/"delete" — a bare expansion next to one of
# these is a name/value, not a flag. EVERYTHING else (-q, -f, -v, --sort=…, --ignore-case, …) states no
# mode and leaves the expansion refused: only flags known to conflict with -d/-D count.
_BRANCH_MODE_LETTERS = frozenset("mMcCulr")
_BRANCH_MODE_LONG = frozenset(
    {
        "--list", "--move", "--copy", "--set-upstream-to", "--unset-upstream", "--edit-description",
        "--show-current", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--remotes",
    }
)
# `git branch` options that take a separate VALUE word (`--sort refname`): that word is not a flag.
_BRANCH_VALUE_FLAGS = frozenset(
    {"--format", "--sort", "--set-upstream-to", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at"}
)


_QUOTED_SPAN = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'")


def _unquoted_expansion(word: _Word) -> bool:
    """True when an expansion sits OUTSIDE quotes in the word — only those can word-split."""
    outside = _QUOTED_SPAN.sub("", word.raw)
    return word.dynamic and ("$" in outside or "`" in outside)


def _hides_flag(word: _Word) -> bool:
    """A dynamic word whose expansion can word-split into a LATER flag with a LITERAL dash
    (``feature${IFS}-D``) — judged on the text after expansions become spaces. A dash that arrives
    inside the variable's value (``x$F`` with ``F=' --delete'``) is the documented known gap."""
    return word.dynamic and re.search(r"\s-", _flatten(word.raw)) is not None


def _deny_hidden_flags(words: list[_Word], what: str) -> None:
    for w in words:
        if _hides_flag(w):
            raise _Deny(f"`{what}` argument {w.raw!r} can word-split into a flag after expansion")


def _is_branch_mode_long(token: str) -> bool:
    """A `git branch` mode flag by exact name or any abbreviation of 3+ characters (`--l` is `--list`)."""
    name = token.split("=", 1)[0]
    return name.startswith("--") and len(name) >= 3 and any(full.startswith(name) for full in _BRANCH_MODE_LONG)


def _brace_word(word: _Word) -> bool:
    return word.dynamic and word.raw.lstrip("'\"").startswith("{")


def _bare_expansion(word: _Word) -> bool:
    """True when the word's value begins with an expansion (``$X``, ``"$X"``, ``$(…)``, a backtick
    substitution) — nothing literal in front of it, so it could itself be a flag."""
    return word.dynamic and word.raw.lstrip("'\"").startswith(("$", "`", "{"))


def _deny_dynamic_flags(flags: list[_Word], what: str) -> None:
    """A flag whose letters come from an expansion (`-$L`, `--$L`) cannot be classified."""
    for f in flags:
        name = f.raw.split("=", 1)[0]  # an expansion only in the VALUE (`--opt=$X`) cannot change the option
        if f.dynamic and ("$" in name or "`" in name):
            raise _Deny(f"`{what}` flag {f.raw!r} contains an expansion — its letters are unknowable before the shell runs")


def _wants_delete(flags: list[_Word], uppercase: bool = False) -> bool:
    """``-d`` (and ``-D`` when ``uppercase``) in any short cluster, or ``--delete`` / an abbreviation."""
    letters = _short_letters(flags)
    return "d" in letters or (uppercase and "D" in letters) or any(_is_long_option(f.text, "--delete") for f in flags)


def _short_letters(flags: list[_Word]) -> str:
    """Every letter of every short-flag cluster (``-df`` → ``df``)."""
    return "".join(f.text[1:] for f in flags if not f.text.startswith("--"))


def _parse_worktree_remove(args: list[_Word], c_dirs: tuple[str, ...]) -> _Removal | None:
    if not args:
        return None
    if args[0].dynamic:
        raise _Deny(f"the `git worktree` subcommand {args[0].raw!r} comes from an expansion — cannot know it is not `remove`")
    if args[0].text != "remove":
        return None
    positionals, flags = _split_args(args[1:])
    _deny_dynamic_flags(flags, "git worktree remove")
    for f in flags:
        if f.text not in ("-f", "--force"):
            raise _Deny(f"unsupported `git worktree remove` option {f.raw!r}")
    if len(positionals) != 1:
        raise _Deny(f"`git worktree remove` must name exactly one worktree path (got {len(positionals)}) — remove them one command at a time")
    return _Removal((_Item("worktree", _plain(positionals[0], "worktree path")),), c_dirs)


def _parse_branch_delete(args: list[_Word], c_dirs: tuple[str, ...]) -> _Removal | None:
    names, flags = _split_args(args, _BRANCH_VALUE_FLAGS, short_value_letters="u")
    _deny_dynamic_flags(flags, "git branch")
    letters = _short_letters(flags)
    states_a_mode = bool(set(letters) & _BRANCH_MODE_LETTERS) or any(_is_branch_mode_long(f.text) for f in flags)
    _deny_hidden_flags(names, "git branch")
    for w in names:  # a bare or UNQUOTED expansion can be (or split into) `-D`; a quoted prefix cannot
        if not states_a_mode and (_bare_expansion(w) or _unquoted_expansion(w)):
            raise _Deny(f"`git branch` argument {w.raw!r} contains an expansion — it could be or hide `-D`")
    delete = _wants_delete(flags, uppercase=True)
    # git refuses -d/-D combined with another mode (--list, -m, -c, -u, …), and `-r` (a mode here)
    # deletes only remote-tracking refs: neither removes a local branch
    if not delete or states_a_mode or not names:
        return None
    return _Removal(tuple(_Item("local-branch", _plain(w, "branch name")) for w in names), c_dirs)


def _parse_push_delete(args: list[_Word], c_dirs: tuple[str, ...]) -> _Removal | None:
    positionals, flags = _split_args(args, _PUSH_VALUE_FLAGS, short_value_letters="o")
    _deny_dynamic_flags(flags, "git push")
    for f in flags:
        if _is_long_option(f.text, "--mirror") or _is_long_option(f.text, "--prune"):
            raise _Deny(f"`git push {f.text}` can delete remote branches in bulk and cannot be evaluated target by target")
    delete = _wants_delete(flags)
    _deny_hidden_flags(positionals, "git push")
    if any(_brace_word(w) for w in positionals):
        raise _Deny("a `git push` argument is a brace expansion — it could assemble `--delete`")
    if any(_is_long_option(f.text, "--repo") for f in flags) and (
        delete or any(w.text.lstrip("+").startswith(":") for w in positionals)
    ):
        raise _Deny("`git push --repo=<remote>` names the remote by flag; a deletion through it is not evaluated")
    refspecs = positionals[1:]
    if delete:
        if len(positionals) < 2:
            raise _Deny("`git push --delete` needs a remote and at least one branch")
        deletions = [_plain(w, "branch name") for w in refspecs]
        if any(":" in d for d in deletions):
            raise _Deny("`git push --delete` with a `src:dst` refspec cannot be evaluated")
    else:
        deletions = [_plain(w, "refspec") for w in refspecs if w.text.lstrip("+").startswith(":")]
    if not deletions:
        return None
    remote = _plain(positionals[0], "remote name")
    items = [_Item("remote-branch", name, remote) for name in map(_branch_name, deletions) if name is not None]
    if items and len(deletions) != len(refspecs):
        # The branch that vouches for a deleted one could be the very ref this same push rewrites.
        raise _Deny(
            "a `git push` that deletes a branch AND updates another ref in the same command cannot be evaluated "
            "— run the deletion as its own command"
        )
    return _Removal(tuple(items), c_dirs) if items else None


def _parse_update_ref(args: list[_Word], c_dirs: tuple[str, ...]) -> _Removal | None:
    """``git update-ref`` deletes any ref with no landed check — ``-d``, an empty new value, or
    ``--stdin`` commands — so a branch (``refs/heads/…``), ``HEAD`` (which follows its symref) or an
    unknowable target is refused (use ``git branch -D <name>`` so the verdict judges it). Setting a
    branch to a new value overwrites it like ``git branch -f`` and is judged as an overwrite. Tags,
    remote-tracking refs and other namespaces pass (``update-ref HEAD <x>`` moves the CURRENT branch and
    is not judged: a known gap)."""
    positionals, flags = _split_args(args, frozenset({"-m"}), short_value_letters="m")
    _deny_dynamic_flags(flags, "git update-ref")
    if any(_is_long_option(f.text, "--stdin") for f in flags):
        raise _Deny("`git update-ref --stdin` reads ref deletions from a stream the guard cannot evaluate")
    new_value = positionals[1] if len(positionals) > 1 else None
    null_new_value = new_value is not None and (new_value.text == "" or set(new_value.text) == {"0"})
    deletes = _wants_delete(flags) or null_new_value
    if not positionals:
        return None
    ref = positionals[0]
    if ref.dynamic:
        raise _Deny("`git update-ref` with an expanded ref name cannot be judged")
    if new_value is not None and new_value.dynamic and (
        ref.text == "HEAD" or ref.text.startswith("refs/heads/") or not ref.text.startswith("refs/")
    ):
        raise _Deny("`git update-ref` on a branch with an expanded new value could be a deletion (empty / null oid)")
    if not deletes:
        if ref.text.startswith("refs/heads/"):
            # overwriting a branch re-points it exactly like `git branch -f`: judge its current tip
            name = ref.text.removeprefix("refs/heads/")
            return _Removal((_Item("local-branch", name, start=_start_text(new_value), overwrite=True),), c_dirs)
        return None
    if ref.text == "HEAD" or ref.text.startswith("refs/heads/") or not ref.text.startswith("refs/"):
        raise _Deny(
            f"`git update-ref` deleting {ref.text!r} removes a branch with no landed-work check — "
            "use `git branch -D <name>` so the removal is verified"
        )
    return None


def _parse_branch(args: list[_Word], c_dirs: tuple[str, ...]) -> _Removal | None:
    return _parse_branch_delete(args, c_dirs) or _parse_branch_overwrite(args, c_dirs)


def _start_text(word: _Word | None) -> str:
    """The start-point revision a branch is re-pointed at, as written (``""`` when absent). A word from an
    expansion carries its unexpanded text, which is no revision: the overwrite verdict then simply gets
    no fast-forward exemption."""
    return "" if word is None else word.text


def _parse_branch_overwrite(args: list[_Word], c_dirs: tuple[str, ...]) -> _Removal | None:
    """``git branch -f <name> [<start>]``, ``-M``/``-C`` and ``-m``/``-c`` combined with ``-f``/
    ``--force`` (and the long spellings): the DESTINATION branch is overwritten when it already exists.
    A plain ``-m``/``-c`` refuses to overwrite, a delete or another mode (list, -u, -r, …) is not an
    overwrite: those return ``None``."""
    names, flags = _split_args(args, _BRANCH_VALUE_FLAGS, short_value_letters="u")
    _deny_dynamic_flags(flags, "git branch")
    letters = _short_letters(flags)
    is_move = "m" in letters or "M" in letters or any(_is_long_option(f.text, "--move") for f in flags)
    is_copy = "c" in letters or "C" in letters or any(_is_long_option(f.text, "--copy") for f in flags)
    forced = "f" in letters or "M" in letters or "C" in letters or any(_is_long_option(f.text, "--force") for f in flags)
    other_mode = bool(set(letters) & set("ulr")) or any(
        _is_branch_mode_long(f.text) and not _is_long_option(f.text, "--move") and not _is_long_option(f.text, "--copy")
        for f in flags
    )
    if not forced or other_mode or (is_move and is_copy) or _wants_delete(flags, uppercase=True):
        return None
    if is_move or is_copy:
        if len(names) not in (1, 2):
            return None
        dest = _plain(names[-1], "branch name")
        start = f"refs/heads/{_plain(names[0], 'branch name')}" if len(names) == 2 else "HEAD"
    else:
        if not names:
            return None
        dest = _plain(names[0], "branch name")
        start = _start_text(names[1]) if len(names) > 1 else "HEAD"
    return _Removal((_Item("local-branch", dest, start=start, overwrite=True),), c_dirs)


def _parse_checkout_overwrite(args: list[_Word], c_dirs: tuple[str, ...], switch: bool) -> _Removal | None:
    """``git checkout -B <name> [<start>]`` / ``git switch -C|--force-create <name> [<start>]`` re-point
    ``<name>`` (creating it when absent). The value of ``-B``/``-C`` is the rest of its cluster
    (``-Bname``) or the next word; everything after ``--`` is a path, not a start-point."""
    letter = "C" if switch else "B"
    dest: _Word | None = None
    positionals: list[_Word] = []
    options_done = False
    i = 0
    while i < len(args):
        word = args[i]
        text = word.text
        i += 1
        if options_done:
            continue  # a path after `--`, never a start-point
        if text == "-" or not text.startswith("-"):
            positionals.append(word)
        elif text == "--":
            options_done = True
        elif text.startswith("--"):
            if switch and _is_long_option(text, "--force-create"):
                if "=" in text:
                    dest = _Word(text.split("=", 1)[1], word.raw, word.dynamic, word.glob, word.subs)
                elif i < len(args):
                    dest, i = args[i], i + 1
        else:
            _deny_dynamic_flags([word], f"git {'switch' if switch else 'checkout'}")
            if letter in text[1:]:
                rest = text[text.index(letter, 1) + 1 :]
                if rest:
                    dest = _Word(rest, word.raw, word.dynamic, word.glob, word.subs)
                elif i < len(args):
                    dest, i = args[i], i + 1
    if dest is None:
        return None
    name = _plain(dest, "branch name")
    start = _start_text(positionals[0]) if positionals else "HEAD"
    return _Removal((_Item("local-branch", name, start=start, overwrite=True),), c_dirs)


def _parse_checkout(args: list[_Word], c_dirs: tuple[str, ...]) -> _Removal | None:
    return _parse_checkout_overwrite(args, c_dirs, switch=False)


def _parse_switch(args: list[_Word], c_dirs: tuple[str, ...]) -> _Removal | None:
    return _parse_checkout_overwrite(args, c_dirs, switch=True)


def _home_resolved(word: _Word) -> str | None:
    """The text of ``word`` with a leading ``~`` / ``$HOME`` / ``${HOME}`` resolved, ``word.text``
    itself when it has no expansion, ``None`` when anything else in it is unknowable."""
    if not word.dynamic:
        return word.text
    text = word.text
    for prefix in ("~", "$HOME", "${HOME}"):
        if text == prefix or text.startswith(prefix + "/"):
            rest = text[len(prefix) :]
            return None if any(ch in rest for ch in "$`{}~") else os.path.expanduser("~") + rest
    return None


def _parse_rm(args: list[_Word], *, fed: bool) -> _Removal | None:
    """A RECURSIVE ``rm`` (``-r``/``-R``/``--recursive`` in any cluster, anywhere before ``--``): each
    operand becomes an ``rm-path`` item judged against the linked worktrees it is or contains. A
    non-recursive ``rm`` cannot delete a directory and is not one; ``fed`` means the operands arrive
    from ``xargs``/``find``/``parallel``, which the guard cannot see."""
    positionals, flags = _split_args(args)
    _deny_dynamic_flags(flags, "rm")
    recursive = bool(set(_short_letters(flags)) & set("rR")) or any(_is_long_option(f.text, "--recursive") for f in flags)
    if not recursive:
        return None
    if fed:
        raise _Deny(
            "a recursive `rm` fed by xargs/find/parallel deletes paths the guard cannot see — name each path in a "
            "plain `rm -r <path>` command"
        )
    items: list[_Item] = []
    for word in positionals:
        text = _home_resolved(word)
        if text is None:
            raise _Deny(
                f"`rm -r` target {word.raw!r} contains unexpanded shell syntax ($var, $(…), `…`, brace expansion) — "
                "cannot know whether it is a worktree; name the path literally"
            )
        items.append(_Item("rm-path", text, glob=word.glob))
    return _Removal(tuple(items), ()) if items else None


def _parse_git(words: list[_Word]) -> _Removal | None:
    """Parse a direct ``git …`` segment. ``None`` = not a removal; raises :class:`_Deny` for a
    removal that cannot be judged."""
    c_dir_words: list[_Word] = []
    unsupported: str | None = None
    i = 1
    while i < len(words):
        t = words[i].text
        if t == "-C":
            if i + 1 >= len(words):
                raise _Deny("`git -C` without a directory")
            c_dir_words.append(words[i + 1])  # judged below, only when the parse yields a removal
            i += 2
        elif t in _GIT_PLAIN_GLOBALS:
            i += 1
        elif t in _GIT_VALUE_GLOBALS:
            unsupported = unsupported or t
            i += 2
        elif t.startswith("-"):
            unsupported = unsupported or t
            i += 1
        else:
            break
    if i >= len(words):
        return None
    if words[i].dynamic:
        raise _Deny(f"the git subcommand {words[i].raw!r} comes from an expansion — cannot know it is not a removal")
    sub, args = words[i].text, words[i + 1 :]
    parsers = {
        "worktree": _parse_worktree_remove,
        "branch": _parse_branch,
        "push": _parse_push_delete,
        "update-ref": _parse_update_ref,
        "checkout": _parse_checkout,
        "switch": _parse_switch,
    }
    parser = parsers.get(sub)
    if parser is None:
        return None
    removal = parser(args, ())
    if removal is None:
        return None
    if unsupported is not None:
        raise _Deny(f"git global option {unsupported!r} changes which repository/config the removal acts on — not supported by the guard")
    return replace(removal, c_dirs=tuple(_plain(w, "`-C` directory") for w in c_dir_words))


def _check_substitution(source: str) -> None:
    """A ``$(…)`` / backtick / ``<(…)`` body runs as a command: analyze it like any other, so a
    removal hidden in it is denied while an innocent one (``$(cat <<'EOF' … EOF)`` carrying a
    commit message that merely MENTIONS a removal) is not."""
    try:
        inner = _tokenize(source)
    except _ParseError as exc:
        if _parse_error_is_suspicious(source):
            raise _Deny(f"a command substitution could not be parsed ({exc}) and could hide a removal") from exc
        return
    for seg in inner.segments:
        if _analyze_segment(seg) is not None:
            raise _Deny("a command substitution runs a removal command")


def _runs_a_string(head: str, words: list[_Word]) -> bool:
    """Does this executor run a command STRING (``bash -c …``, ``eval …``, ``ssh host …``) rather than a
    script path (``bash "$HOME/run.sh"``, ``source "$VENV/activate"``)?"""
    if head in {"eval", "ssh"}:  # `ssh host "$CMD"` hands its arguments to a remote shell
        return True
    return any(w.text == "-c" or (w.text.startswith("-") and not w.text.startswith("--") and "c" in w.text[1:]) for w in words[1:])


def _analyze_segment(seg: _Segment) -> _Removal | None:
    words = seg.words
    if not words:
        return None
    if words[0].dynamic and (
        mentions_removal(_text_line(words)) or any(w.text in _SUBCOMMANDS for w in words[1:])
    ):
        raise _Deny("the command word itself comes from an expansion and the line looks like a git removal")
    for w in (*words, *seg.redirect_words):
        for sub in w.subs:
            _check_substitution(sub)
    for sub in seg.heredoc_subs:
        _check_substitution(sub)
    if _ASSIGNMENT_RE.match(words[0].text):
        first_command = next((k for k, w in enumerate(words) if not _ASSIGNMENT_RE.match(w.text)), len(words))
        rest = _Segment(words[first_command:], seg.heredocs, seg.heredoc_subs, seg.redirect_words)
        if rest.words and _analyze_segment(rest) is not None:
            raise _Deny("an environment-assignment prefix in front of a removal command is not supported")
        return None
    head = os.path.basename(words[0].text)
    if head == "git":
        return _parse_git(words)
    if (head == "exec" or head in _GIT_RUNNERS) and len(words) > 1 and _bare_expansion(words[1]):
        raise _Deny(f"`{head}` runs a command that comes from an expansion — the guard cannot see it")
    if head in _EXECUTORS and _runs_a_string(head, words) and any(w.dynamic for w in words[1:]):
        raise _Deny(f"`{head}` runs a command string that comes from an expansion — the guard cannot see it")
    if head in _EXECUTORS and (
        any(mentions_removal(w.raw) or mentions_removal(w.text) for w in (*words, *seg.redirect_words))
        or any(mentions_removal(b) for b in seg.heredocs)
        # `eval` and `ssh` join their ARGUMENTS into one command line: `eval git branch -D x`
        or (head in _JOINING_EXECUTORS and mentions_removal(_raw_line(words[1:])))
    ):
        raise _Deny(f"`{head}` would run a removal command from a string/script the guard cannot evaluate")
    for k, w in enumerate(words[1:], start=1):
        if head in _GIT_RUNNERS and os.path.basename(w.text) in _EXECUTORS:
            # `sudo bash -c "$CMD"`: the wrapper hands the rest of the line to an executor — judge that
            _analyze_segment(_Segment(words[k:], seg.heredocs, seg.heredoc_subs, seg.redirect_words))
            continue
        if os.path.basename(w.text) != "git":
            continue
        # a wrapper runs the git; for any other command `git` is just an argument (`grep git "$f"`)
        if mentions_removal(_raw_line(words[k:])) or (head in _GIT_RUNNERS and _parse_git(words[k:]) is not None):
            raise _Deny(f"`{head}` wraps a git removal command (sudo/env/xargs/time/find -exec …) — run it directly")
    if head == "rm":
        return _parse_rm(words[1:], fed=False)
    if head == "exec" or head in _GIT_RUNNERS:
        k = next((k for k in range(1, len(words)) if os.path.basename(words[k].text) == "rm"), None)
        if k is not None:
            wrapped = _parse_rm(words[k + 1 :], fed=head in _FEEDERS)
            if wrapped is not None:
                return wrapped
    return None


# ── evaluation ───────────────────────────────────────────────────────────────────
def _effective_cwd(cwd: Path, c_dirs: tuple[str, ...]) -> Path:
    eff = cwd
    for d in c_dirs:
        eff = Path(d) if os.path.isabs(d) else eff / d
    return Path(os.path.normpath(eff))


def _worktree_target(eff: Path, raw: str) -> Path:
    """The absolute path to judge for ``raw``. A target reached THROUGH a symlink (anywhere in its
    path) is refused unless git itself recorded exactly that spelling."""
    written = Path(os.path.normpath(raw if os.path.isabs(raw) else eff / raw))
    if written.resolve() != written:
        recorded = {Path(rec.get("worktree", "")) for rec in worktree_gc.worktree_records(eff)}
        if written not in recorded:
            raise _Deny(f"{raw!r} reaches its worktree through a symlink — name the real path (`git worktree list`)")
    return written


def _rescue_text(verdicts: list[rv.Verdict]) -> str:
    lines = [
        "Sanctioned ways forward:",
        "  - Land it first (merge the PR / push + merge the branch), then retry. Inspect any target with `rig worktree verdict <path|branch>`.",
    ]
    unsafe = next((v for v in verdicts if not v.safe), None)
    if unsafe is not None:
        repo = _repo_name(Path(unsafe.target)) if unsafe.kind == "worktree" else "<repo>"
        hint = rv.rescue_hint(unsafe.kind, unsafe.target, repo, unsafe.tip)
        lines.append(f"  - To discard {unsafe.kind} {unsafe.target} on purpose, {hint}.")
    return "\n".join(lines)


def _repo_name(path: Path) -> str:
    """The repo's directory name (`git worktree list` always reports the PRIMARY checkout first)."""
    try:
        records = worktree_gc.worktree_records(path if path.exists() else path.parent)
        return Path(records[0]["worktree"]).name
    except (worktree_gc.WorktreeGcError, KeyError, IndexError, OSError):
        return "repo"


def _verdict_for(
    item: _Item, eff: Path, forge: rv.ForgeLookup, process_cwds: rv.ProcessCwds, removing: frozenset[str], fetch: bool
) -> rv.Verdict:
    if item.kind == "worktree":
        return rv.worktree_verdict(eff, _worktree_target(eff, item.name), forge=forge, process_cwds=process_cwds)
    if item.kind == "local-branch" and item.overwrite:
        return rv.overwrite_verdict(eff, item.name, start=item.start, forge=forge, fetch=fetch)
    if item.kind == "local-branch":
        return rv.branch_verdict(eff, item.name, forge=forge)
    return rv.remote_branch_verdict(eff, item.remote, item.name, forge=forge, removing=removing)


def _cached_forge(forge: rv.ForgeLookup) -> rv.ForgeLookup:
    """One forge lookup per command, however many targets it judges."""
    cached: list[rv.PrRecord] | None = None

    def lookup(repo: Path) -> list[rv.PrRecord]:
        nonlocal cached
        if cached is None:
            cached = forge(repo)
        return cached

    return lookup


def _decide(verdicts: list[rv.Verdict], refresh_notes: list[str]) -> GuardDecision:
    """``allow`` when every verdict is SAFE, else ``deny`` with every verdict rendered and the rescue."""
    notes = "".join(f"\nnote: {n}" for n in (*refresh_notes, *(n for v in verdicts for n in v.notes)))
    if all(v.safe for v in verdicts):
        proofs = ", ".join(f"{v.kind} {v.target} ({v.landed_by})" for v in verdicts)
        return GuardDecision(
            "allow",
            "every target is clean and proven landed on the default branch or held by another remote branch that "
            f"stays: {proofs}{notes}",
            tuple(verdicts),
        )
    body = "\n".join(v.render() for v in verdicts)
    reason = (
        "removal guard: DENY — this removal/overwrite could lose work that is not on the default branch.\n"
        f"{body}\n"
        "Nothing was run. A removal or overwrite is allowed only when the target is clean AND every commit on it is "
        "landed on origin/<default>, proven (ancestor, merged PR with the same head, content no-op, or held by "
        "another remote branch that stays) — `--force` never changes that.\n"
        f"{_rescue_text(verdicts)}{notes}"
    )
    return GuardDecision("deny", reason, tuple(verdicts))


# ── rm -r of a worktree ──────────────────────────────────────────────────────────
_CWD_CHANGERS = frozenset({"cd", "pushd", "popd", "chdir"})


def _changes_cwd(parsed: _Parsed) -> bool:
    """True when some word of the command is a directory change: what a relative path or a repository
    resolved from the cwd means after it is not knowable here (a loose superset — a false hit only
    asks for an absolute path)."""
    return any(w.text in _CWD_CHANGERS for seg in parsed.segments for w in seg.words)


def _rm_targets(item: _Item, cwd: Path, cwd_moves: bool) -> list[Path]:
    """Every path ``rm -r`` would delete for one operand (read the way the kernel resolves it: symlinks
    first, then ``..``), plus — for an unquoted glob — what the shell would expand it to (relative to the
    REAL cwd)."""
    text = item.name
    if not os.path.isabs(text):
        if cwd_moves:
            raise _Deny(
                f"`rm -r {text}` follows a `cd`/`pushd` in the same command, so the directory it is relative to is "
                "unknowable — use an absolute path"
            )
        text = os.path.join(cwd, text)
    found = [Path(os.path.realpath(text))]  # physical: `link/..` is the parent of the link's TARGET
    if item.glob:
        found.extend(Path(os.path.normpath(m)) for m in sorted(glob.glob(text)))
    return found


def _nearest_directory(path: Path) -> Path:
    current = path
    while not current.is_dir() and current != current.parent:
        current = current.parent
    return current


def _contains(outer: Path, inner: Path) -> bool:
    return inner == outer or outer in inner.parents


_SCAN_ENTRY_LIMIT = 5000  # directory entries looked at per `rm -r` target when hunting for nested repositories
_SCAN_DEPTH = 4
_SCAN_SKIP = frozenset({".git", "node_modules"})


def _repositories_inside(path: Path) -> list[Path]:
    """Directories under ``path`` (itself included) that hold a ``.git`` entry — a linked worktree's
    pointer file or a repository — found by a BOUNDED walk (depth and entry caps, no symlinks, no
    descent into a repository root or ``node_modules``). A worktree of a repository that is neither the
    cwd's nor the one ``path`` sits in (``rm -rf ~/old-worktrees``) is found this way. A target so large
    that the cap is hit is only partially scanned: a documented limit, not a guess."""
    found: list[Path] = []
    seen = 0
    if not path.is_dir():
        return found
    for root, dirs, files in os.walk(path, followlinks=False):
        here = Path(root)
        seen += len(dirs) + len(files)
        if ".git" in dirs or ".git" in files:
            found.append(here)
            dirs[:] = []  # the repository's own records list every worktree it has
        else:
            depth = len(here.relative_to(path).parts)
            dirs[:] = [] if depth >= _SCAN_DEPTH else [d for d in dirs if d not in _SCAN_SKIP]
        if seen > _SCAN_ENTRY_LIMIT:
            break
    return found


def _worktrees_under(paths: list[Path], cwd: Path) -> list[tuple[Path, Path]]:
    """``(primary checkout, linked worktree)`` for every EXISTING linked worktree that one of ``paths``
    is or contains. The repositories consulted are the cwd's, the one each path sits in and any found
    inside a path (:func:`_repositories_inside`), so a worktree of another repository is found from
    anywhere; each target is compared in its written and its symlink-resolved form, so a link to a
    worktree (or to the directory holding it) cannot hide it. A sub-directory OF a worktree is
    ordinary editing and is not matched."""
    seeds = list(dict.fromkeys([cwd, *(_nearest_directory(p) for p in paths), *(r for p in paths for r in _repositories_inside(p))]))
    repos: dict[Path, list[dict[str, str]]] = {}
    for seed in seeds:
        if not rv.is_git_repo(seed):
            continue
        try:
            records = worktree_gc.worktree_records(seed)
        except worktree_gc.WorktreeGcError as exc:
            raise _Deny(f"could not list the worktrees of the repository at {seed} ({exc})") from exc
        if records:
            repos.setdefault(Path(os.path.realpath(records[0]["worktree"])), records)
    targets = {form for p in paths for form in (p, Path(os.path.realpath(p)))}
    found: list[tuple[Path, Path]] = []
    for records in repos.values():
        for rec in records[1:]:
            wt = Path(rec.get("worktree", ""))
            if "bare" in rec or not wt.exists():
                continue
            forms = {Path(os.path.normpath(wt)), Path(os.path.realpath(wt))}
            if any(_contains(t, w) for t in targets for w in forms) and (Path(records[0]["worktree"]), wt) not in found:
                found.append((Path(records[0]["worktree"]), wt))
    return found


def _evaluate_in_place(
    removals: list[_Removal],
    parsed: _Parsed,
    cwd: Path,
    forge: rv.ForgeLookup,
    process_cwds: rv.ProcessCwds,
    fetch: bool,
) -> GuardDecision:
    """Branch OVERWRITES (``branch -f/-M/-C``, ``checkout -B``, ``switch -C``) and recursive ``rm`` are
    judged per command even inside a chain — they are everyday commands (`git fetch && git checkout -B
    x origin/x`, `cd d && rm -rf build`) — but only while the directory they act from is knowable: a
    `cd`/`pushd` anywhere in the command makes every relative target and every cwd-resolved repository
    unknowable. Creating a branch that does not exist yet is not an overwrite and stays silent."""
    cwd_moves = _changes_cwd(parsed)
    lookup = _cached_forge(forge)
    verdicts: list[rv.Verdict] = []
    notes: list[str] = []
    rm_paths: list[Path] = []
    for removal in removals:
        git_items = [i for i in removal.items if i.kind != "rm-path"]
        if git_items:
            if cwd_moves and not (removal.c_dirs and os.path.isabs(removal.c_dirs[0])):
                raise _Deny(
                    "a `cd`/`pushd` earlier in the same command changes the repository this branch overwrite acts on — "
                    "run it as its own command or name the repository with `git -C <absolute dir>`"
                )
            eff = _effective_cwd(cwd, removal.c_dirs)
            if not rv.is_git_repo(eff):
                raise _Deny(f"{eff} is not inside a git repository")
            verdicts.extend(_verdict_for(item, eff, lookup, process_cwds, frozenset(), fetch) for item in git_items)
        for item in removal.items:
            if item.kind == "rm-path":
                rm_paths.extend(_rm_targets(item, cwd, cwd_moves))
    verdicts = [v for v in verdicts if v.landed_by != rv.NEW_BRANCH]
    refreshed: set[Path] = set()
    for primary, wt in _worktrees_under(rm_paths, cwd):
        if fetch and primary not in refreshed:
            refreshed.add(primary)
            notes.extend(rv.refresh_default_ref(primary))
        verdicts.append(rv.worktree_verdict(primary, wt, forge=lookup, process_cwds=process_cwds))
    if not verdicts:
        return GuardDecision("pass")
    return _decide(verdicts, notes)


def _evaluate_segments(
    parsed: _Parsed,
    cwd: Path,
    forge: rv.ForgeLookup,
    process_cwds: rv.ProcessCwds,
    fetch: bool,
) -> GuardDecision:
    removals = [r for r in (_analyze_segment(seg) for seg in parsed.segments) if r is not None]
    if not removals:
        return GuardDecision("pass")
    if all(r.in_place for r in removals):
        return _evaluate_in_place(removals, parsed, cwd, forge, process_cwds, fetch)
    if len(parsed.segments) > 1 or parsed.grouped:
        raise _Deny(
            "a removal inside a chained / piped / grouped / backgrounded command is not evaluated — run each "
            "removal as its own simple command (use `git -C <dir> …` instead of `cd`)"
        )
    removal = removals[0]
    eff = _effective_cwd(cwd, removal.c_dirs)
    if not rv.is_git_repo(eff):
        raise _Deny(f"{eff} is not inside a git repository")
    refresh_notes = rv.refresh_default_ref(eff) if fetch else []
    lookup = _cached_forge(forge)
    # The branches this ONE push deletes on the remote: none of them may vouch for another (deleting
    # `a` and `b` together leaves nothing holding a commit that only they hold). A local branch or
    # worktree removal touches no remote ref, so the remote holders all stay.
    removing = frozenset(item.name for item in removal.items if item.kind == "remote-branch")
    verdicts = [_verdict_for(item, eff, lookup, process_cwds, removing, fetch) for item in removal.items]
    return _decide(verdicts, refresh_notes)


def _log(decision: GuardDecision, command: str, cwd: Path) -> None:
    """Append one JSON line per allow/deny. Logging is best-effort and never changes the decision."""
    try:
        path = Path.home() / ".cache" / "rig" / "removal-guard.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "decision": decision.decision,
            "cwd": str(cwd),
            "command": command[:500],
            "reason": decision.reason[:500],
            "targets": decision.to_dict()["targets"],
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def evaluate_command(
    command: str,
    cwd: Path,
    *,
    forge: rv.ForgeLookup | None = None,
    process_cwds: rv.ProcessCwds = rv.all_process_cwds,
    fetch: bool = True,
) -> GuardDecision:
    """``pass`` / ``allow`` / ``deny`` for one bash ``command`` run from ``cwd``. Never raises:
    any failure is a ``deny`` (fail closed)."""
    forge = forge or rv.default_forge_lookup
    # The REAL cwd: git records real worktree paths, and a symlink in the cwd prefix must not make
    # every relative target look symlinked (only symlinks in what the COMMAND names are refused).
    try:
        if not needs_parse(command):
            return GuardDecision("pass")
        base_cwd = Path(os.path.realpath(cwd))
        try:
            parsed = _tokenize(command)
        except _ParseError as exc:
            if not _parse_error_is_suspicious(command):
                return GuardDecision("pass")  # unparseable, but nothing in it can be a removal
            raise _Deny(f"the command could not be parsed ({exc}) and mentions a removal") from exc
        decision = _evaluate_segments(parsed, base_cwd, forge, process_cwds, fetch)
    except _Deny as exc:
        decision = GuardDecision(
            "deny",
            f"removal guard: DENY — {exc}\nNothing was run. Rewrite it as ONE plain command with literal targets "
            "(`git worktree remove <path>`, `git branch -D <name>`, `git push <remote> --delete <name>`); each "
            "target is then checked: clean and fully landed on the default branch, or refused.",
        )
    except Exception as exc:  # noqa: BLE001 — fail CLOSED on any internal error
        decision = GuardDecision("deny", f"removal guard: DENY — internal error while checking ({exc!r}); failing closed")
    if decision.decision != "pass":
        _log(decision, command, Path(cwd))
    return decision
