"""omp guard extension codegen — renders the permissions rule registry into a TS hook.

omp auto-discovers TS files under ``<agent root>/extensions/``; a ``tool_call`` handler
returning ``{block: true, reason}`` blocks a bash call BEFORE execution (verified live,
rig-cli#202). This module turns :data:`riglib.permissions.OMP_GUARD_DENY_RULES` /
:data:`OMP_GUARD_ASK_RULES` into the wholly rig-owned ``rig-permissions-guard.ts`` — ONE
registry, ONE renderer, never a hand-copied second list.

The generated file carries a machine-parseable provenance header (template version, rule
ids, sha256 of the rule payload) so drift/deprovision can tell a rig-generated file from a
foreign one, and a runtime event-shape self-check that fails CLOSED with a loud
``rig-permissions-guard.incompatible`` marker if omp ever renames the ``toolName`` /
``input.command`` contract (a silent no-op after an upgrade is the worst outcome).

RESIDUAL fail-open mode (documented, Phase-2): if omp stops LOADING the extension at all
(discovery removed, load-time failure), no handler ever runs — the self-check and heartbeat
cannot fire, drift sees byte-identical files, and the approval posture stays relaxed with
zero enforcement. The opt-in activation probe (:mod:`riglib.omp_probe`) is the Phase-1
detection for this; a passive session-heartbeat check is the planned Phase-2 complement.

Stdlib-only at import time (the repo import rule).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json

from .permissions import (
    OMP_GUARD_ASK_RULES,
    OMP_GUARD_DENY_RULES,
    SIGNAL_MATCHERS,
    GuardRule,
)

#: bump when the generated TS SHAPE changes (not the rules — those flip the rules hash).
#: v2 (rig-cli#187): subcommand_flags now accepts a 1-token (flat command) shape and matches a
#: joined ``flag=value`` single token, not just an exact-token match.
#: v3 (rig-cli#701): stage operators are kept (``Stage.next``) for the signal-safety matchers,
#: literal PIDs resolve against /proc, ``sh -c``/``eval``/``env -S`` strings are re-parsed as
#: commands, and the module exports ``evaluateBash`` plus a check CLI.
GUARD_TEMPLATE_VERSION = 3

#: written next to the extension by the GUARD ITSELF when the omp event contract breaks;
#: drift surfaces its existence (the guard blocks all bash in that state — fail closed).
INCOMPATIBLE_MARKER_NAME = "rig-permissions-guard.incompatible"

_PROVENANCE_PREFIX = "// rig-guard-provenance: "


def _rules_payload(deny: tuple[GuardRule, ...], ask: tuple[GuardRule, ...]) -> str:
    """The canonical JSON the provenance hash is computed over (stable key order)."""
    return json.dumps(
        {
            "deny": [dataclasses.asdict(r) for r in deny],
            "ask": [dataclasses.asdict(r) for r in ask],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _rules_ts(rules: tuple[GuardRule, ...]) -> str:
    """One generated ``{ id, hint, matcher, tokens, flags, value_prefixes, flags_with_value,
    scope }`` entry per line."""
    lines = []
    for r in rules:
        tokens = json.dumps(list(r.tokens))
        flags = json.dumps(list(r.flags))
        prefixes = json.dumps(list(r.value_prefixes))
        flags_with_value = json.dumps(list(r.flags_with_value))
        scope = json.dumps(list(r.scope))
        lines.append(
            f'  {{ id: {json.dumps(r.id)}, hint: {json.dumps(r.hint, ensure_ascii=False)}, '
            f'matcher: {json.dumps(r.matcher)}, tokens: {tokens}, flags: {flags}, '
            f'value_prefixes: {prefixes}, flags_with_value: {flags_with_value}, scope: {scope} }},'
        )
    return "\n".join(lines)


def render_guard_ts(
    deny: tuple[GuardRule, ...] = OMP_GUARD_DENY_RULES,
    ask: tuple[GuardRule, ...] = OMP_GUARD_ASK_RULES,
    *,
    marker_dir: str | None = None,
) -> str:
    """The full generated ``rig-permissions-guard.ts`` content (deterministic per inputs).

    ``marker_dir`` bakes the install dir into the file for the incompatible-marker path
    (rig knows where it installed the guard — including PI_* override roots); when absent
    the generated file falls back to ``import.meta.dir`` at runtime."""
    sha = hashlib.sha256(_rules_payload(deny, ask).encode("utf-8")).hexdigest()[:16]
    provenance = json.dumps({
        "template": GUARD_TEMPLATE_VERSION,
        "sha256": sha,
        "deny": [r.id for r in deny],
        "ask": [r.id for r in ask],
    })
    return (
        _TS_TEMPLATE
        .replace("%%PROVENANCE%%", provenance)
        .replace("%%DENY_RULES%%", _rules_ts(deny))
        .replace("%%ASK_RULES%%", _rules_ts(ask))
        .replace("%%MARKER%%", INCOMPATIBLE_MARKER_NAME)
        .replace("%%MARKER_DIR%%", json.dumps(marker_dir or ""))
        .replace("%%SIGNAL_MATCHERS%%", json.dumps(list(SIGNAL_MATCHERS)))
    )


# The generated extension, as a PLAIN string (no f-string — the TS is brace-dense).
# Placeholders: %%PROVENANCE%% (header JSON), %%DENY_RULES%% / %%ASK_RULES%% (rule table
# lines), %%MARKER%% (the incompatible-marker filename), %%MARKER_DIR%% (the baked install dir),
# %%SIGNAL_MATCHERS%% (the signal matcher names from the registry).
_TS_TEMPLATE = r"""// rig-managed permissions guard — GENERATED by rig (https://git.hyperide.ai/ultrabricks/rig-cli); do not edit.
// rig-guard-provenance: %%PROVENANCE%%
// Wholly rig-owned: hand edits are backed up and replaced on the next `rig apply`.
// deny rules block outright; ask rules confirm-gate via the harness UI and BLOCK headless
// (a prompt nobody can answer must never auto-approve). Reasons carry rule ids + static
// hints only — never command contents (commands can hold secrets).

import { readFileSync } from "node:fs";

export interface Rule { id: string; hint: string; matcher: string; tokens: string[]; flags: string[]; value_prefixes?: string[]; flags_with_value?: string[]; scope: string[]; }

const DENY: Rule[] = [
%%DENY_RULES%%
];
const ASK: Rule[] = [
%%ASK_RULES%%
];

// Quote-aware tokenizer: single/double quotes group, backslash escapes outside single
// quotes. Shell stage operators (&&, ||, |, ;, &, |&) split EVEN UNSPACED — a real shell
// parses `git push --force&&true` as two stages, so the guard must too. An unquoted `#`
// at a word start opens a comment through end-of-line — the shell never sees
// `echo ok # note; git reset --hard`'s "second stage", so the guard must not either.
const OPS = new Set(["&&", "||", "|", ";", "&", "|&"]);
function tokenize(command: string): string[] {
  const out: string[] = [];
  let cur = "";
  let quote = "";
  let has = false;
  const flush = () => { if (has) { out.push(cur); cur = ""; has = false; } };
  for (let i = 0; i < command.length; i++) {
    const c = command[i];
    if (quote === "") {
      if (c === "'" || c === '"') { quote = c; has = true; continue; }
      if (c === "\\" && i + 1 < command.length && command[i + 1] === "\n") { i++; continue; } // line continuation: bash removes it pre-tokenization
      if (c === "\\" && i + 1 < command.length) { cur += command[++i]; has = true; continue; }
      if (c === "#" && !has) { // comment: skip through (not including) the newline
        while (i + 1 < command.length && command[i + 1] !== "\n") i++;
        continue;
      }
      if (c === "\n") { flush(); out.push(";"); continue; } // a newline is a command separator too
      if (c === ">" || c === "<") { // redirections end a token: git push --force>/dev/null still matches
        flush();
        if (c === ">" && command[i + 1] === ">") { out.push(">>"); i++; }
        else out.push(c);
        continue;
      }
      if (c === "&" || c === "|" || c === ";") {
        flush();
        let op = c;
        if (c === "&" && command[i + 1] === "&") { op = "&&"; i++; }
        else if (c === "|" && command[i + 1] === "|") { op = "||"; i++; }
        else if (c === "|" && command[i + 1] === "&") { op = "|&"; i++; }
        out.push(op);
        continue;
      }
      if (/\s/.test(c)) { flush(); continue; }
      cur += c; has = true;
    } else if (quote === "'") {
      if (c === "'") quote = ""; else cur += c;
    } else {
      if (c === '"') quote = "";
      else if (c === "\\" && i + 1 < command.length && '"\\$`'.includes(command[i + 1])) cur += command[++i];
      else cur += c;
    }
  }
  flush();
  return out;
}

// A standalone shell operator begins a fresh pipeline stage; each stage is matched
// independently (a && b checks both). `next` keeps the operator AFTER the stage ("" at the
// end): the signal matchers need it to see a `| xargs kill` feeder, the `|`-split tail of a
// `$(… | …)` substitution, and whether a `[ -n "$P" ] &&` guard really chains into the kill.
interface Stage { argv: string[]; next: string; }
function stages(argv: string[]): Stage[] {
  const out: Stage[] = [];
  let cur: string[] = [];
  for (const tok of argv) {
    if (OPS.has(tok)) { if (cur.length) out.push({ argv: cur, next: tok }); cur = []; }
    else cur.push(tok);
  }
  if (cur.length) out.push({ argv: cur, next: "" });
  return out;
}

// Leading VAR=val env assignments are transparent to matching (FOO=1 gh pr merge still
// matches the gh-pr-merge prefix).
function stripEnv(stage: string[]): string[] {
  let i = 0;
  while (i < stage.length && /^[^\s=/][^\s=]*=/.test(stage[i])) i++;
  return stage.slice(i);
}

// Global options of git/sudo are transparent too: `git -C /repo push --force`,
// `git --no-pager push -f`, `sudo -E rm`, `sudo -u root rm` must still match. Value-taking
// globals skip TWO tokens, boolean globals one. Only applied to the programs listed.
const GLOBAL_OPTS_WITH_VALUE: Record<string, Set<string>> = {
  git: new Set(["-C", "-c", "--git-dir", "--work-tree", "--namespace"]),
  gh: new Set(["-R", "--repo", "--hostname"]),
  sudo: new Set(["-u", "-g", "-h", "-p", "-C", "-T", "-r", "-t",
                 "--user", "--group", "--host", "--prompt", "--role", "--type", "--preserve-env"]),
  env: new Set(["-u", "-C", "-S", "--unset", "--chdir", "--split-string"]),
  timeout: new Set(["-k", "--kill-after", "-s", "--signal"]),
  nice: new Set(["-n", "--adjustment"]),
};
// wrappers whose FIRST operand is a value, not the command (timeout 5 git push --force)
const LEADING_VALUE_WRAPPER = new Set(["timeout"]);
const GLOBAL_OPTS_BOOL: Record<string, Set<string>> = {
  git: new Set(["-P", "--no-pager", "--literal-pathspecs", "--no-optional-locks"]),
  sudo: new Set(["-E", "-n", "-s", "-H", "-k", "-K", "-v", "-l", "-b", "-i", "--preserve-env", "--"]),
  env: new Set(["-i", "-0", "--ignore-environment", "--null", "-"]),
  command: new Set(["-p"]), // `command -p x` still runs x; -v/-V only probe (never matched)
};
// `env`/`command` execute their argument — they are transparent prefixes, not wrappers
// (unlike `sh -c`, whose whole command STRING evaluateBash re-parses via nestedCommand).
const TRANSPARENT_PREFIX = new Set(["env", "command", "timeout", "nice", "nohup"]);
// skip a program's global options starting at index i; returns the first operand index.
function skipOpts(stage: string[], i: number, withValue?: Set<string>, bools?: Set<string>): number {
  while (i < stage.length) {
    const tok = stage[i];
    if (withValue && withValue.has(tok)) { i += 2; continue; }
    // joined --flag=value form skips ONE token
    const eq = tok.indexOf("=");
    if (eq > 0 && withValue && withValue.has(tok.slice(0, eq))) { i += 1; continue; }
    // attached short value (`nice -n10`, `sudo -uroot`, `timeout -sTERM`) skips ONE token
    if (withValue && tok.length > 2 && tok[0] === "-" && tok[1] !== "-" && withValue.has(tok.slice(0, 2))) { i += 1; continue; }
    if (bools && bools.has(tok)) { i += 1; continue; }
    break;
  }
  return i;
}

function stripGlobalOpts(stage: string[]): string[] {
  const program = stage[0];
  if (TRANSPARENT_PREFIX.has(program)) {
    // drop the prefix AND its own options (and a leading value operand for timeout), then
    // re-strip env assignments (env -i VAR=x git push --force / timeout 5 git push --force)
    let i = skipOpts(stage, 1, GLOBAL_OPTS_WITH_VALUE[program], GLOBAL_OPTS_BOOL[program]);
    if (stage[i] === "--") i++; // `env -- kill -- -1`: the option terminator, not the command
    if (LEADING_VALUE_WRAPPER.has(program)) i++;
    const rest = stripEnv(stage.slice(i));
    if (!rest.length) return stage;
    return stripGlobalOpts(rest);
  }
  const withValue = GLOBAL_OPTS_WITH_VALUE[program];
  const bools = GLOBAL_OPTS_BOOL[program];
  if (!withValue && !bools) return stage;
  const i = skipOpts(stage, 1, withValue, bools);
  return [program, ...stage.slice(i)];
}

// `/usr/bin/pkill` is `pkill`: rules name programs, not install paths.
function normalizeProg(stage: string[]): string[] {
  return stage.length ? [progName(stage[0]), ...stage.slice(1)] : stage;
}

function matches(rule: Rule, rawStage: string[]): boolean {
  const envStripped = normalizeProg(stripEnv(rawStage));
  if (rule.matcher === "flag_value_prefix") {
    // PRE-strip: global-option stripping would delete the very -c payload this rule catches
    const stage = envStripped;
    // any of the flag tokens whose FOLLOWING token starts with a value prefix
    if (stage[0] !== rule.tokens[0]) return false;
    for (let i = 1; i < stage.length - 1; i++) {
      if (rule.flags.includes(stage[i]) && (rule.value_prefixes ?? []).some((p) => stage[i + 1].startsWith(p))) {
        return true;
      }
    }
    return false;
  }
  // Two views of the stage: as written (`sudo rm` keeps its sudo for the sudo-rm rule) and with
  // every prefix chain stripped (`sudo /usr/bin/pkill x`, `flock f git push --force`).
  const views = [stripGlobalOpts(envStripped), stripGlobalOpts(normalizeProg(commandWords(rawStage)))];
  return views.some((stage) => matchesStage(rule, stage));
}

function matchesStage(rule: Rule, stage: string[]): boolean {
  if (rule.matcher === "argv_prefix") {
    if (stage.length < rule.tokens.length) return false;
    return rule.tokens.every((t, i) => stage[i] === t);
  }
  // subcommand_flags: the stage is led by `tokens` — program alone (1 token, e.g. `rg`) or
  // program+subcommand (2 tokens, e.g. `git push`) — carrying at least one flag as an EXACT
  // token anywhere (so --force-with-lease never satisfies --force, and a quoted commit
  // MESSAGE mentioning --no-verify never satisfies it either). `flags_with_value` (a subset of
  // `flags`) is checked PER-FLAG, not family-wide: those specific flags ALSO match a joined
  // `flag=value` single token (rg --pre=cmd) via an exact `f + "="` prefix check. Deliberately
  // opt-in — a family-wide `=` check would newly deny e.g. `git commit -m "--no-verify=x"`
  // (a message token that merely STARTS WITH the flag name), widening that rule's
  // false-positive surface; only flags that actually take an `=`-joinable value opt in.
  if (stage[0] !== rule.tokens[0]) return false;
  if (rule.tokens.length > 1 && stage[1] !== rule.tokens[1]) return false;
  if (rule.flags.some((f) => stage.includes(f))) return true;
  return (rule.flags_with_value ?? []).some((f) => stage.some((tok) => tok.startsWith(f + "=")));
}

// eval-tool source-text scan: the needle (rule.tokens[0]) must appear as an isolated
// command-name token in a string literal — preceded by an opening quote at a call-argument
// position (start of source, whitespace, comma, `(`, or `[`) and followed by a closing quote,
// space, or backtick-continuation. This matches `execSync("osascript …")`,
// `Bun.spawn(["osascript", …])`, `os.system("osascript …")` while NOT matching a bare mention
// in a comment ("// avoid osascript") that never opens a quote immediately before it.
function matchesCodePattern(rule: Rule, code: string): boolean {
  const needle = rule.tokens[0].replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp("(^|[\\s,(\\[])[\"'`]" + needle + "([\"'` ]|$)");
  return re.test(code);
}

// ── signal safety (rig-cli#701) ──────────────────────────────────────────────────────────
// 2026-10-15 `P=$(pgrep -f "^gh ship 247" | head -1); kill -TERM $(pstree -p $P | …)` ran
// with an empty $P: `pstree -p` printed the whole system tree and SIGTERM reached
// systemd --user, ending every omp session and the desktop. These matchers judge WHAT a
// kill-class command signals: its operands, the `$(…)`/xargs source feeding it, and — for a
// literal PID — what /proc says that process is.

// Where literal PIDs resolve: the real /proc and the omp process running this guard. Tests and
// the check CLI pass a faked tree through `evaluateBash`; nothing reads an env override (the
// in-process bash tool could set one).
export interface Host { procRoot: string; selfPid: number; }
const LIVE_HOST: Host = { procRoot: "/proc", selfPid: process.pid };
export interface Finding { kind: "deny" | "ask"; rule: Rule; }

// Rendered from riglib.permissions.SIGNAL_MATCHERS — the same names ruleHits dispatches on.
const SIGNAL_MATCHERS = new Set(%%SIGNAL_MATCHERS%%);
// Shell words that run the command after them, so `if true; then kill $P; fi`,
// `sudo kill -9 -1` or `(kill $P)` still reach the kill.
const SHELL_KEYWORDS = new Set(["if", "elif", "then", "else", "do", "while", "until", "!", "{", "(", "time"]);
const EXEC_PREFIX = new Set(["builtin", "exec"]);
// Privilege wrappers and their own options (`sudo -u root kill`, `doas -u root kill`).
const PRIV_OPTS = new Map<string, [Set<string>, Set<string>]>([
  ["sudo", [GLOBAL_OPTS_WITH_VALUE.sudo, GLOBAL_OPTS_BOOL.sudo]],
  ["doas", [new Set(["-u", "-C", "-a"]), new Set(["-n", "-s", "-L", "--"])]],
]);
// Runners that exec their trailing command after their own options and `operands` leading
// values: `systemd-run --user --pipe --wait kill …` even runs it OUTSIDE omp's scope, `flock
// <file> cmd` is how this machine queues heavy jobs, `setsid kill …` is how agents detach.
interface Runner { values: Set<string>; operands: number; }
const RUNNERS = new Map<string, Runner>([
  ["systemd-run", { operands: 0, values: new Set(["-u", "--unit", "-p", "--property", "--description",
    "--slice", "-E", "--setenv", "--uid", "--gid", "--nice", "--working-directory", "-M", "--machine",
    "-H", "--host", "--on-active", "--on-boot", "--on-startup", "--on-unit-active", "--on-unit-inactive",
    "--on-calendar", "--timer-property", "--path-property", "--socket-property", "--service-type"]) }],
  ["flock", { operands: 1, values: new Set(["-w", "--timeout", "-E", "--conflict-exit-code"]) }],
  ["setsid", { operands: 0, values: new Set<string>() }],
  ["stdbuf", { operands: 0, values: new Set(["-i", "-o", "-e", "--input", "--output", "--error"]) }],
  ["ionice", { operands: 0, values: new Set(["-c", "-n", "--class", "--classdata"]) }],
  ["chrt", { operands: 1, values: new Set(["-T", "-P", "-D", "--sched-runtime", "--sched-period",
    "--sched-deadline"]) }],
  ["taskset", { operands: 1, values: new Set<string>() }],
  ["strace", { operands: 0, values: new Set(["-o", "-e", "-s", "-u", "-E", "-O", "-I", "-b", "-a", "-P",
    "-X"]) }],
  ["ltrace", { operands: 0, values: new Set(["-o", "-e", "-s", "-u", "-a", "-n", "-l"]) }],
]);

function progName(tok: string): string {
  return tok.slice(tok.lastIndexOf("/") + 1);
}

// One step down a prefix chain (`sudo`, `env`, `timeout`, `/usr/bin/env`, `flock f`, `then`, a
// leading `(`, …) — null when `words` does not start with a prefix.
function stripPrefix(words: string[]): string[] | null {
  const first = words[0];
  const name = progName(first);
  if (name !== first && (PRIV_OPTS.has(name) || TRANSPARENT_PREFIX.has(name) || EXEC_PREFIX.has(name))) {
    return [name, ...words.slice(1)]; // /usr/bin/env kill -1 is env kill -1
  }
  if (SHELL_KEYWORDS.has(first) || EXEC_PREFIX.has(first)) return words.slice(1);
  if (first.length > 1 && first.startsWith("(")) return [first.replace(/^\(+/, ""), ...words.slice(1)];
  const priv = PRIV_OPTS.get(first);
  if (priv) return words.slice(skipOpts(words, 1, priv[0], priv[1]));
  if (TRANSPARENT_PREFIX.has(first)) {
    const rest = stripGlobalOpts(words);
    return rest !== words ? rest : null;
  }
  const runner = RUNNERS.get(name);
  if (runner) {
    let i = 1;
    while (i < words.length && words[i].startsWith("-")) {
      if (words[i] === "--") { i++; break; }
      i += runner.values.has(words[i]) ? 2 : 1;
    }
    return words.slice(i + runner.operands);
  }
  return null;
}

// Strips at most MAX_PREFIX_HOPS prefixes; a longer chain is left with a prefix still in front
// (prefixOverflow) and the signal matchers fail CLOSED on it rather than guess what it runs.
const MAX_PREFIX_HOPS = 16;
function commandWords(argv: string[]): string[] {
  let words = stripEnv(argv);
  for (let hop = 0; hop < MAX_PREFIX_HOPS && words.length; hop++) {
    const next = stripPrefix(words);
    if (next === null) break;
    words = stripEnv(next);
  }
  return words;
}
function prefixOverflow(words: string[]): boolean {
  return words.length > 0 && stripPrefix(words) !== null;
}

// A command STRING another command runs — `sh -c '…'`, `bash -lc "…"`, `eval …`, `env -S '…'`,
// `find … -exec … ;`, `watch …`, a shell reading `<<< '…'` or `echo '…' |` — behind any prefix
// chain. evaluateBash re-parses it like a top-level command: being denied `kill -1` must not
// leave `bash -c 'kill -1'` one keystroke away.
const SHELLS = new Set(["sh", "bash", "zsh", "dash", "ksh", "ash"]);
const SHELL_OPTS_WITH_VALUE = new Set(["-o", "+o", "-O", "+O", "--rcfile", "--init-file"]);
const FIND_EXEC = new Set(["-exec", "-execdir", "-ok", "-okdir"]);
function nestedCommand(st: Stage[], idx: number): string | null {
  let words = stripEnv(st[idx].argv);
  for (let hop = 0; hop < 16 && words.length; hop++) {
    const name = progName(words[0]);
    if (name === "eval") return words.slice(1).join(" ");
    if (name === "watch") {
      const i = skipOpts(words, 1, new Set(["-n", "--interval"]), new Set(["-t", "-x", "-e", "-g", "-b", "-c", "-d"]));
      return words.slice(i).join(" ");
    }
    if (name === "find") {
      const at = words.findIndex((w) => FIND_EXEC.has(w));
      if (at < 0) return null;
      const end = words.findIndex((w, k) => k > at && (w === ";" || w === "+"));
      return words.slice(at + 1, end < 0 ? words.length : end).join(" ");
    }
    if (name === "env") {
      for (let i = 1; i < words.length && words[i].startsWith("-") && words[i] !== "--"; i++) {
        const t = words[i];
        if (t === "-S" || t === "--split-string") return words.slice(i + 1).join(" ");
        if (t.startsWith("--split-string=")) return [t.slice("--split-string=".length), ...words.slice(i + 1)].join(" ");
        if (/^-S./.test(t)) return [t.slice(2), ...words.slice(i + 1)].join(" ");
        if (GLOBAL_OPTS_WITH_VALUE.env.has(t)) i++;
      }
    }
    if (name === "xargs") { // `… | xargs sh -c '…'`
      let i = 1;
      while (i < words.length && words[i].startsWith("-")) i += XARGS_OPTS_WITH_VALUE.has(words[i]) ? 2 : 1;
      words = stripEnv(words.slice(i));
      continue;
    }
    if (SHELLS.has(name)) return shellCommandString(words, st, idx);
    const next = stripPrefix(words);
    if (next === null) return null;
    words = stripEnv(next);
  }
  return null;
}
// What a shell invocation will run as a command string: the `-c` operand, a `<<<` here-string,
// or — when it reads its script from stdin — what an `echo`/`printf` stage pipes into it.
function shellCommandString(words: string[], st: Stage[], idx: number): string | null {
  const here = words.findIndex((w, k) => w === "<" && words[k + 1] === "<" && words[k + 2] === "<");
  if (here >= 0) return words[here + 3] ?? null;
  let hasC = false;
  let fromStdin = false;
  let i = 1;
  for (; i < words.length; i++) {
    const t = words[i];
    if (t === "--") { i++; break; }
    if (t.length < 2 || !/^[-+]/.test(t)) break;
    if (/^-[A-Za-z]*c[A-Za-z]*$/.test(t)) hasC = true; // `-c` makes the first operand the string
    else if (/^-[A-Za-z]*s[A-Za-z]*$/.test(t)) fromStdin = true;
    else if (SHELL_OPTS_WITH_VALUE.has(t)) i++;
  }
  if (hasC) return words[i] ?? null;
  if (!fromStdin && i < words.length && !REDIRECTS.has(words[i])) return null; // runs a script file
  if (idx === 0 || st[idx - 1].next !== "|") return null;
  const feeder = commandWords(st[idx - 1].argv);
  const prog = progName(feeder[0] ?? "");
  if (prog === "echo") return feeder.slice(1).filter((w) => !/^-[neE]+$/.test(w)).join(" ");
  if (prog === "printf") return feeder.slice(1).join(" ").replace(/\\n/g, "\n");
  return null;
}

// The tokenizer splits `2>/dev/null` into `2`, `>`, `/dev/null`; none of those are operands.
const REDIRECTS = new Set([">", ">>", "<"]);
function dropRedirects(args: string[]): string[] {
  const out: string[] = [];
  for (let i = 0; i < args.length; i++) {
    if (REDIRECTS.has(args[i])) { i++; continue; }
    if (/^\d$/.test(args[i]) && REDIRECTS.has(args[i + 1])) continue;
    out.push(args[i]);
  }
  return out;
}

interface SignalCall { prog: string; args: string[]; viaXargs: boolean; overflow: boolean; }
const XARGS_OPTS_WITH_VALUE = new Set(["-I", "-L", "-n", "-P", "-s", "-d", "-E", "-a", "--arg-file",
  "--delimiter", "--max-args", "--max-procs", "--max-lines", "--max-chars", "--eof", "--replace",
  "--process-slot-var"]);
function signalCall(argv: string[]): SignalCall | null {
  let words = commandWords(argv);
  let viaXargs = false;
  if (words.length && progName(words[0]) === "xargs") {
    let i = 1;
    while (i < words.length && words[i].startsWith("-")) i += XARGS_OPTS_WITH_VALUE.has(words[i]) ? 2 : 1;
    words = commandWords(words.slice(i));
    viaXargs = true;
  }
  if (!words.length) return null;
  return { prog: progName(words[0]), args: dropRedirects(words.slice(1)), viaXargs, overflow: prefixOverflow(words) };
}

// `kill [-s SIG | -n NUM | -SIG] [--] pid…`. The first `-X` is the signal (bash reads `kill -1`
// as SIGHUP with no pid); after it, a `-N` operand is a process group.
interface KillArgs { signal: string | null; list: boolean; operands: string[]; }
function parseKill(args: string[]): KillArgs {
  let signal: string | null = null;
  let i = 0;
  for (; i < args.length; i++) {
    const t = args[i];
    if (t === "--") { i++; break; }
    if (t === "-l" || t === "-L" || t === "--list" || t === "--table") return { signal, list: true, operands: [] };
    if (t === "-s" || t === "-n" || t === "--signal") { signal = args[++i] ?? ""; continue; }
    if (t.startsWith("--signal=")) { signal = t.slice("--signal=".length); continue; }
    if (signal === null && t.length > 1 && t.startsWith("-")) { signal = t.slice(1); continue; }
    break;
  }
  return { signal, list: false, operands: operandWords(args.slice(i)) };
}
// `(`/`)` depth and backtick state across tokens: the tokenizer leaves `$(pgrep -f x)` as the
// tokens `$(pgrep`, `-f`, `x)`, and splits it into stages at an inner `;` or `|`.
interface Nesting { depth: number; tick: boolean; }
function nest(state: Nesting, tok: string): boolean { // true while still inside a substitution
  for (const c of tok) {
    if (c === "(") state.depth++;
    else if (c === ")" && state.depth > 0) state.depth--;
    else if (c === "`") state.tick = !state.tick;
  }
  return state.depth > 0 || state.tick;
}
// Tokens INSIDE an unquoted `$(… …)`/backtick substitution are not operands (`kill $(pgrep -f x)`
// must not read `-f` as a process group); the opener token stands for the whole substitution.
// A stray closer (`(kill $P)`) is trimmed.
function operandWords(raw: string[]): string[] {
  const out: string[] = [];
  const state: Nesting = { depth: 0, tick: false };
  for (const t of raw) {
    if (state.depth === 0 && !state.tick) out.push(t);
    nest(state, t);
  }
  return out.map((o) => (o.includes("(") ? o : o.replace(/\)+$/, "")));
}
function isProbe(signal: string | null): boolean {
  return signal !== null && signal.toUpperCase().replace(/^SIG/, "") === "0";
}

// The words of a `$(…)`/backtick substitution opened at st[j].argv[t], followed to its closing
// paren ACROSS stages: the tokenizer splits `$(true; pstree -p $P | grep …)` at the inner `;`
// and `|`, and the taint must not stop there.
function substitutionSpan(st: Stage[], j: number, t: number): string[] {
  const words: string[] = [];
  const state: Nesting = { depth: 0, tick: false };
  for (let s = j; s < st.length; s++) {
    for (let k = s === j ? t : 0; k < st[s].argv.length; k++) {
      words.push(st[s].argv[k]);
      if (!nest(state, st[s].argv[k])) return words;
    }
  }
  return words;
}
function stripSubstitution(w: string): string {
  return w.replace(/^[A-Za-z_][A-Za-z0-9_]*=/, "").replace(/^(\$\(|`)+/, "").replace(/[)`]+$/, "");
}
// Plain words of substitution text: a QUOTED `"$(pgrep -x omp)"` stays one token, so split it.
function wordsOf(tokens: string[]): string[] {
  return tokens.map(stripSubstitution).flatMap((w) => w.split(/\s+/)).map(stripSubstitution).filter(Boolean);
}
function isOpener(t: string): boolean {
  return t.includes("$(") || t.includes("`");
}
// Every substitution in stage idx — a harmless `$(true)` first must not hide a later
// `$(pgrep -x omp)` (empty when the stage holds none).
function substitutionWords(st: Stage[], idx: number): string[] {
  const argv = st[idx].argv;
  const words: string[] = [];
  for (let k = 0; k < argv.length; k++) {
    if (!isOpener(argv[k])) continue;
    const span = substitutionSpan(st, idx, k);
    words.push(...span);
    k += span.length - 1; // past this substitution (beyond the stage when it spans several)
  }
  return wordsOf(words);
}
// The stages piping INTO an `xargs kill` stage.
function feederWords(st: Stage[], idx: number): string[] {
  const words: string[] = [];
  for (let j = idx - 1; j >= 0 && st[j].next === "|"; j--) words.unshift(...st[j].argv);
  return words;
}
// The latest earlier assignment of NAME before stage idx — `NAME=…` (also after `export`/
// `declare`/`local`/`readonly`/`typeset`) or `for NAME in …`: the words it draws from, and its
// value when that is a plain literal (`P=-1`, `P=12345`).
const ASSIGN_BUILTINS = new Set(["export", "declare", "local", "readonly", "typeset"]);
interface Assignment { words: string[]; literal: string | null; }
function assignment(st: Stage[], idx: number, name: string): Assignment | null {
  for (let j = idx - 1; j >= 0; j--) {
    const raw = st[j].argv;
    const builtin = ASSIGN_BUILTINS.has(raw[0]);
    let tok: string | undefined;
    for (let k = builtin ? 1 : 0; k < raw.length; k++) {
      if (raw[k].startsWith(name + "=")) { tok = raw[k]; break; }
      if (!builtin && !/^[A-Za-z_][A-Za-z0-9_]*=/.test(raw[k])) break; // past the leading assignments
    }
    const argv = commandWords(raw);
    if (tok === undefined && !(argv[0] === "for" && argv[1] === name && argv[2] === "in")) continue;
    const value = tok === undefined ? null : tok.slice(name.length + 1);
    return {
      words: [...wordsOf(raw), ...substitutionWords(st, j)],
      literal: value !== null && !value.includes("$") && !value.includes("`") ? value : null,
    };
  }
  return null;
}
function assignedWords(st: Stage[], idx: number, name: string): string[] {
  return assignment(st, idx, name)?.words ?? [];
}
// A `$VAR` operand bound to a plain literal earlier in the command reads as that literal.
function literalOperand(o: string, st: Stage[], idx: number): string {
  const v = varRef(o);
  return (v && assignment(st, idx, v.name)?.literal) || o;
}

function isPstree(w: string): boolean {
  return progName(w) === "pstree";
}
// Session infrastructure by process name (pkill/killall patterns, pgrep/pidof inside a
// substitution). Word-bounded so `test_omp_guard.py` or `systemd-run` never match.
const PROTECTED_PROC_RE = /(?:^|[\s\/^*.|(\[])(?:systemd|gnome-shell|gnome-session(?:-binary|-b|-manager)?|gdm(?:3|-(?:wayland|x)-ses(?:sion)?|-session-worker)?|Xwayland|tmux|omp)(?=$|[\s$|)\]\\:'"@])/;
const PROCESS_FINDERS = new Set(["pgrep", "pidof", "ps", "pstree"]);
function findsProtected(words: string[]): boolean {
  return words.some((w) => PROCESS_FINDERS.has(progName(w))) && words.some((w) => PROTECTED_PROC_RE.test(w));
}

function varRef(op: string): { name: string; required: boolean } | null {
  let m = /^\$\{([A-Za-z_][A-Za-z0-9_]*|\$)(:?\?[^}]*)?\}$/.exec(op);
  if (m) return { name: m[1], required: m[2] !== undefined };
  m = /^\$([A-Za-z_][A-Za-z0-9_]*|\$|!)$/.exec(op);
  return m ? { name: m[1], required: false } : null;
}
// `$$`/`$PPID` in omp's in-process bash tool are the omp session itself and its parent.
const SELF_VARS = new Set(["$", "PPID", "BASHPID"]);

// `[ -n "$P" ]` / `[[ -n $P ]]` / `test -n "$P"` (or `-z` for the `||` form) on NAME.
function testsVar(argv: string[], name: string, flag: string): boolean {
  let i = 0;
  while (i < argv.length && (argv[i] === "if" || argv[i] === "elif" || argv[i] === "while")) i++;
  if (!["[", "[[", "test"].includes(argv[i])) return false;
  for (let k = i + 1; k < argv.length - 1; k++) {
    if (argv[k] === flag && (argv[k + 1] === "$" + name || argv[k + 1] === "${" + name + "}")) return true;
  }
  return false;
}
function allNext(st: Stage[], from: number, to: number, op: string): boolean {
  for (let j = from; j < to; j++) if (st[j].next !== op) return false;
  return true;
}
// A VISIBLE non-empty check of NAME that every path to the kill at stage idx passes through:
// `[ -n "$P" ] && … && kill $P`, `[ -z "$P" ] || kill $P`, or `if [ -n "$P" ]; then kill $P`.
function guarded(st: Stage[], idx: number, name: string): boolean {
  for (let g = idx - 1; g >= 0; g--) {
    const argv = st[g].argv;
    if (testsVar(argv, name, "-n")) {
      if (allNext(st, g, idx, "&&")) return true;
      if (["if", "elif", "while"].includes(argv[0])) {
        let entered = false;
        let left = false;
        for (let j = g + 1; j <= idx; j++) {
          const w = st[j].argv[0];
          if (w === "then" || w === "do") entered = true;
          if (w === "else" || w === "elif" || w === "fi" || w === "done") left = true;
        }
        if (entered && !left) return true;
      }
    }
    if (testsVar(argv, name, "-z") && st[g].next === "||" && allNext(st, g + 1, idx, "&&")) return true;
  }
  return false;
}

// One evaluation's view of /proc: each file read at most once, the guard's own ancestry walked
// once — a `kill 9001 … 9100` must not re-read the same files per operand.
interface ProcView { host: Host; files: Map<string, string | null>; ancestry: Set<number> | null; }
function readProc(view: ProcView, pid: number, file: string): string | null {
  const key = pid + "/" + file;
  if (!view.files.has(key)) {
    let text: string | null = null;
    try { text = readFileSync(view.host.procRoot + "/" + key, "utf8"); } catch { /* gone, or no procfs */ }
    view.files.set(key, text);
  }
  return view.files.get(key) ?? null;
}
function procStat(view: ProcView, pid: number): { ppid: number; sid: number } | null {
  const s = readProc(view, pid, "stat");
  if (s === null) return null;
  const f = s.slice(s.lastIndexOf(")") + 2).trim().split(/\s+/); // comm may hold spaces/parens
  return { ppid: Number(f[1]), sid: Number(f[3]) };
}
function procCgroup(view: ProcView, pid: number): string[] {
  const lines = (readProc(view, pid, "cgroup") ?? "").split("\n").filter(Boolean);
  const line = lines.find((l) => l.startsWith("0::")) ?? lines[0] ?? "";
  return line.split(":").slice(2).join(":").split("/").filter(Boolean);
}
// This omp process and every ancestor (pane shell, tmux server, …).
function selfAndAncestors(view: ProcView): Set<number> {
  if (view.ancestry === null) {
    const chain = new Set<number>([view.host.selfPid]);
    for (let p = view.host.selfPid, hop = 0; p > 1 && hop < 64; hop++) {
      const st = procStat(view, p);
      if (!st) break;
      chain.add(st.ppid);
      p = st.ppid;
    }
    view.ancestry = chain;
  }
  return view.ancestry;
}
function ompScope(segs: string[]): string | undefined {
  return segs.find((s) => /^omp-.*\.scope$/.test(s));
}
// comm is truncated to 15 bytes by the kernel ("gnome-session-b", "gdm-wayland-ses").
const PROTECTED_COMM = new Set(["systemd", "gnome-shell", "gnome-session-b", "gnome-session",
  "Xwayland", "tmux: server", "gdm-wayland-ses", "gdm-x-session"]);
// A literal PID is protected when it is pid 1, this omp process or one of its ancestors, session
// infrastructure by comm, a logind session leader, or anything in ANOTHER omp scope. An unknown
// PID (gone, or no procfs) is not ours to judge: the kill itself fails.
function protectedPid(pid: number, view: ProcView): boolean {
  if (pid === 1 || selfAndAncestors(view).has(pid)) return true;
  const comm = readProc(view, pid, "comm");
  if (comm === null) return false;
  const name = comm.trim();
  if (PROTECTED_COMM.has(name)) return true;
  const segs = procCgroup(view, pid);
  const stat = procStat(view, pid);
  if (stat && stat.sid === pid && segs.some((s) => /^session-.*\.scope$/.test(s))) return true;
  const selfScope = ompScope(procCgroup(view, view.host.selfPid));
  const scope = ompScope(segs);
  const foreignScope = selfScope === undefined || scope !== selfScope; // unscoped guard: all foreign
  if (scope !== undefined && foreignScope) return true; // a process of another omp session
  return name === "omp" && foreignScope; // an omp outside our scope (unscoped session)
}

// pkill/killall: option parsing just deep enough to find the PATTERN operands and the
// user/session selectors. A leading `-SIG` is the signal, never a flag cluster.
const SIGNAL_FLAG_RE = /^-(?:SIG)?(?:HUP|INT|QUIT|ILL|TRAP|ABRT|IOT|BUS|FPE|KILL|USR1|SEGV|USR2|PIPE|ALRM|TERM|STKFLT|CHLD|CONT|STOP|TSTP|TTIN|TTOU|URG|XCPU|XFSZ|VTALRM|PROF|WINCH|IO|POLL|PWR|SYS|RTMIN[+-]?\d*|RTMAX[+-]?\d*|\d+)$/;
const PKILL_OPTS_WITH_VALUE = new Set(["-s", "-g", "-G", "-u", "-U", "-P", "-t", "-F", "-O", "-r",
  "--signal", "--session", "--pgroup", "--group", "--euid", "--uid", "--parent", "--terminal",
  "--pidfile", "--older", "--runstates", "--ns", "--nslist", "--env"]);
const PKILL_WIDE_SELECTORS = new Set(["-s", "-g", "-G", "-u", "-U", "-t", "--session", "--pgroup",
  "--group", "--euid", "--uid", "--terminal"]);
const KILLALL_OPTS_WITH_VALUE = new Set(["-s", "--signal", "-u", "--user", "-o", "--older-than", "-y",
  "--younger-than", "-n", "--ns", "-Z", "--context"]);
function parsePatternKill(args: string[], withValue: Set<string>): { patterns: string[]; opts: Set<string> } {
  const patterns: string[] = [];
  const opts = new Set<string>();
  for (let i = 0; i < args.length; i++) {
    const t = args[i];
    if (t === "--") { patterns.push(...args.slice(i + 1)); break; }
    if (t.length < 2 || !t.startsWith("-")) { patterns.push(t); continue; }
    if (SIGNAL_FLAG_RE.test(t)) continue;
    if (t.startsWith("--")) {
      const eq = t.indexOf("=");
      const name = eq > 0 ? t.slice(0, eq) : t;
      opts.add(name);
      if (eq < 0 && withValue.has(name)) i++;
      continue;
    }
    for (let c = 1; c < t.length; c++) { // short cluster: -fx, -u1000, -u 1000
      const flag = "-" + t[c];
      opts.add(flag);
      if (withValue.has(flag)) { if (c === t.length - 1) i++; break; }
    }
  }
  return { patterns, opts };
}

// systemctl/loginctl: positional words after the options (verb first, then units).
function positional(args: string[], withValue: Set<string>): string[] {
  const out: string[] = [];
  for (let i = 0; i < args.length; i++) {
    const t = args[i];
    if (t === "--") { out.push(...args.slice(i + 1)); break; }
    if (t.length > 1 && t.startsWith("-")) { if (withValue.has(t)) i++; continue; }
    out.push(t);
  }
  return out;
}
const SYSTEMCTL_OPTS_WITH_VALUE = new Set(["-t", "--type", "-s", "--signal", "-p", "--property", "-H",
  "--host", "-M", "--machine", "--kill-whom", "--kill-value", "--root", "-n", "--lines", "-o",
  "--output", "--state", "--job-mode", "--what", "--timestamp", "--reboot-argument", "--message",
  "--when", "--drop-in", "--preset-mode", "--image", "--boot-loader-menu", "--boot-loader-entry",
  "--check-inhibitors"]);
// Verbs that end the whole manager or session no matter the unit.
const SYSTEMCTL_TERMINAL_VERBS = new Set(["exit", "halt", "poweroff", "reboot", "kexec", "soft-reboot",
  "isolate", "switch-root", "emergency", "rescue", "default"]);
const SYSTEMCTL_STOPPING_VERBS = new Set(["stop", "kill", "restart", "try-restart", "reload-or-restart",
  "try-reload-or-restart", "condrestart", "force-reload", "freeze", "start", "mask", "disable"]);
// Units whose stop/kill takes sessions down: the manager (init.scope), any slice, user@/session
// units, omp scopes (by pid or glob), GNOME, gdm, tmux, dbus, the shutdown-class targets, and a
// glob with no literal prefix. `run-*.scope` (agent jobs) stays allowed.
const PROTECTED_UNIT_RE = /^(?:init\.scope|.*\.slice|user@.*|user-runtime-dir@.*|session-.*|omp-[0-9*].*|org\.gnome\..*|gnome-.*|gdm.*|tmux.*|dbus.*|(?:exit|shutdown|halt|poweroff|reboot|kexec|soft-reboot|rescue|emergency|graphical-session|graphical-session-pre|default|basic)\.target|[*?\[].*)$/;
const LOGINCTL_OPTS_WITH_VALUE = new Set(["-s", "--signal", "--kill-whom", "-H", "--host", "-M",
  "--machine", "-p", "--property", "-n", "--lines", "-o", "--output"]);
const LOGINCTL_KILL_VERBS = new Set(["terminate-session", "terminate-user", "terminate-seat",
  "kill-session", "kill-user", "poweroff", "reboot", "halt"]);
const TMUX_OPTS_WITH_VALUE = new Set(["-L", "-S", "-f", "-c", "-T"]);
// True when tmux targets a PRIVATE server: `-L name`/`-S path` other than the default socket
// (`-L default` and `-S …/default` are the user's own server).
function tmuxPrivateServer(args: string[]): boolean {
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    const socket = a === "-L" || a === "-S" ? args[i + 1] ?? "" : /^-[LS]./.test(a) ? a.slice(2) : null;
    if (socket !== null) return progName(socket) !== "default";
  }
  return false;
}

// A kill that actually signals: null for `kill -l` listings and `kill -0` probes.
function liveKill(call: SignalCall): KillArgs | null {
  if (call.prog !== "kill") return null;
  const k = parseKill(call.args);
  return k.list || isProbe(k.signal) ? null : k;
}

function pstreeFed(call: SignalCall, st: Stage[], idx: number): boolean {
  const k = liveKill(call);
  if (!k) return false;
  if (call.viaXargs && feederWords(st, idx).some(isPstree)) return true;
  if (substitutionWords(st, idx).some(isPstree)) return true;
  return k.operands.some((o) => {
    const v = varRef(o);
    return v !== null && assignedWords(st, idx, v.name).some(isPstree);
  });
}

function broadcast(call: SignalCall, st: Stage[], idx: number): boolean {
  if (call.prog === "kill") {
    const k = liveKill(call);
    if (!k) return false;
    if (k.signal === "1" && !k.operands.length && !call.viaXargs) return true; // bare `kill -1`
    return k.operands.some((o) => { // `P=-1; [ -n "$P" ] && kill -- "$P"` is still `kill -- -1`
      const lit = literalOperand(o, st, idx);
      return lit === "0" || lit.startsWith("-");
    });
  }
  if (call.prog === "pkill") {
    const p = parsePatternKill(call.args, PKILL_OPTS_WITH_VALUE);
    if (p.opts.has("-v") || p.opts.has("--inverse")) return true;
    return !p.patterns.length && [...p.opts].some((o) => PKILL_WIDE_SELECTORS.has(o));
  }
  if (call.prog === "killall") {
    const p = parsePatternKill(call.args, KILLALL_OPTS_WITH_VALUE);
    return !p.patterns.length && (p.opts.has("-u") || p.opts.has("--user"));
  }
  return false;
}

function protectedTarget(call: SignalCall, st: Stage[], idx: number, view: ProcView): boolean {
  switch (call.prog) {
    case "kill": {
      const k = liveKill(call);
      if (!k) return false;
      for (const o of k.operands) {
        const lit = literalOperand(o, st, idx);
        if (/^\d+$/.test(lit) && protectedPid(Number(lit), view)) return true;
        const v = varRef(o);
        if (v && SELF_VARS.has(v.name)) return true;
        if (v && findsProtected(assignedWords(st, idx, v.name))) return true;
      }
      if (findsProtected(substitutionWords(st, idx))) return true;
      return call.viaXargs && findsProtected(feederWords(st, idx));
    }
    case "pkill":
    case "killall": {
      const withValue = call.prog === "pkill" ? PKILL_OPTS_WITH_VALUE : KILLALL_OPTS_WITH_VALUE;
      if (parsePatternKill(call.args, withValue).patterns.some((p) => PROTECTED_PROC_RE.test(p))) return true;
      // `echo omp | xargs pkill -x`: the pattern arrives on stdin, from the feeding stages
      return call.viaXargs && feederWords(st, idx).some((w) => PROTECTED_PROC_RE.test(w));
    }
    case "systemctl": {
      const [verb, ...units] = positional(call.args, SYSTEMCTL_OPTS_WITH_VALUE);
      if (verb === undefined) return false;
      if (SYSTEMCTL_TERMINAL_VERBS.has(verb)) return true;
      if (!SYSTEMCTL_STOPPING_VERBS.has(verb)) return false;
      if (units.some((u) => PROTECTED_UNIT_RE.test(u))) return true;
      return call.viaXargs && feederWords(st, idx).some((w) => PROTECTED_UNIT_RE.test(w));
    }
    case "loginctl":
      return LOGINCTL_KILL_VERBS.has(positional(call.args, LOGINCTL_OPTS_WITH_VALUE)[0] ?? "");
    case "tmux": { // the DEFAULT server holds the user's panes; a private -L/-S server is a test's
      if (tmuxPrivateServer(call.args)) return false;
      const verb = positional(call.args, TMUX_OPTS_WITH_VALUE)[0] ?? "";
      if (verb.startsWith("kill-ser")) return true;
      // `kill-session -a` kills every OTHER session on the server — the user's included
      const after = call.args.slice(call.args.indexOf(verb) + 1);
      return verb === "kill-session" && after.some((a) => /^-[A-Za-z]*a/.test(a));
    }
  }
  return false;
}

// A kill-class target computed at runtime. For kill: `$(…)`, backticks, a glob, an unchecked
// `$VAR`, or PIDs from xargs. For pkill/killall/systemctl stop|kill: patterns or units from xargs
// (the argv-prefix pkill/killall asks never see an `xargs pkill` stage).
function computedTarget(call: SignalCall, st: Stage[], idx: number): boolean {
  if (call.viaXargs && (call.prog === "pkill" || call.prog === "killall")) return true;
  if (call.viaXargs && call.prog === "systemctl") {
    return SYSTEMCTL_STOPPING_VERBS.has(positional(call.args, SYSTEMCTL_OPTS_WITH_VALUE)[0] ?? "");
  }
  const k = liveKill(call);
  if (!k) return false;
  if (call.viaXargs) return true;
  return k.operands.some((o) => {
    if (/^\d+$/.test(o) || o.startsWith("%")) return false;
    const v = varRef(o);
    if (v && literalOperand(o, st, idx) !== o) return false; // bound to a literal: judged as one
    if (v) return !(v.name === "!" || v.required || guarded(st, idx, v.name));
    return o.includes("$") || o.includes("`") || /[*?[]/.test(o);
  });
}

// One bash command's evaluation state: its stages, the shared /proc view, and each stage's
// parsed kill-class call (the four signal rules share it).
interface Evaluation { st: Stage[]; proc: ProcView; calls: Map<number, SignalCall | null>; }
function callAt(ev: Evaluation, idx: number): SignalCall | null {
  if (!ev.calls.has(idx)) ev.calls.set(idx, signalCall(ev.st[idx].argv));
  return ev.calls.get(idx) ?? null;
}

function ruleHits(rule: Rule, ev: Evaluation, idx: number): boolean {
  if (!SIGNAL_MATCHERS.has(rule.matcher)) return matches(rule, ev.st[idx].argv);
  const call = callAt(ev, idx);
  if (!call) return false;
  // a prefix chain deeper than MAX_PREFIX_HOPS hides what finally runs: deny, never allow
  if (call.overflow) return rule.matcher === "signal_protected";
  if (rule.matcher === "kill_pstree_fed") return pstreeFed(call, ev.st, idx);
  if (rule.matcher === "kill_broadcast") return broadcast(call, ev.st, idx);
  if (rule.matcher === "signal_protected") return protectedTarget(call, ev.st, idx, ev.proc);
  if (rule.matcher === "kill_computed_pid") return computedTarget(call, ev.st, idx);
  return true; // a registry matcher with no branch here: fail CLOSED, never silently inert
}

// Every deny/ask finding for one bash command, in stage order: per stage, a nested command
// string (`sh -c`, `eval`, `env -S`) first, then the deny rules (the first deny ends the scan —
// it blocks regardless of earlier asks), then each matching ask. Recursion always ends (each
// nested string is shorter than its parent) but each level re-tokenizes its whole string, so a
// crafted pyramid costs O(N²) on omp's event loop: past MAX_NEST_DEPTH it fails CLOSED, like
// prefixOverflow — no real command nests shell strings that deep.
const MAX_NEST_DEPTH = 16;
export function evaluateBash(command: string, host: Host = LIVE_HOST): Finding[] {
  return evaluate(command, { host, files: new Map(), ancestry: null }, 0);
}
function evaluate(command: string, proc: ProcView, depth: number): Finding[] {
  const findings: Finding[] = [];
  const ev: Evaluation = { st: stages(tokenize(command)), proc, calls: new Map() };
  for (let i = 0; i < ev.st.length; i++) {
    const nested = nestedCommand(ev.st, i);
    if (nested !== null) {
      if (depth >= MAX_NEST_DEPTH) {
        findings.push({ kind: "deny", rule: DENY.find((r) => r.matcher === "signal_protected")! });
        return findings;
      }
      for (const f of evaluate(nested, proc, depth + 1)) {
        findings.push(f);
        if (f.kind === "deny") return findings;
      }
    }
    for (const rule of DENY) {
      if (rule.matcher === "code_pattern") continue; // eval-only rules never apply to bash argv
      if (ruleHits(rule, ev, i)) { findings.push({ kind: "deny", rule }); return findings; }
    }
    for (const rule of ASK) {
      if (rule.matcher !== "code_pattern" && ruleHits(rule, ev, i)) findings.push({ kind: "ask", rule });
    }
  }
  return findings;
}

// The incompatible marker lives NEXT TO THIS FILE (import.meta.dir), so it lands wherever
// rig actually installed the guard — including PI_CODING_AGENT_DIR / PI_CONFIG_DIR roots —
// which is exactly where rig's drift check looks for it.
const MARKER = "%%MARKER%%";
// The install dir is BAKED at render time (rig resolved it, honoring PI_* overrides); the
// import.meta.dir / HOME forms are fallbacks for a hand-copied file only.
const BAKED_DIR = %%MARKER_DIR%%;
function markerDir(): string {
  if (BAKED_DIR) return BAKED_DIR;
  const anyMeta: any = import.meta;
  if (anyMeta && typeof anyMeta.dir === "string") return anyMeta.dir;
  return (process.env.HOME ?? ".") + "/.omp/agent/extensions";
}
async function writeMarker(why: string): Promise<void> {
  try {
    const fs = await import("node:fs");
    const path = await import("node:path");
    fs.writeFileSync(path.join(markerDir(), MARKER), "rig guard incompatible: " + why + " — blocking fail-closed; re-run `rig apply`\n");
  } catch { /* best effort — the block decision is the enforcement */ }
}
async function clearMarker(): Promise<void> {
  try {
    const fs = await import("node:fs");
    const path = await import("node:path");
    fs.rmSync(path.join(markerDir(), MARKER), { force: true });
  } catch { /* best effort */ }
}

export default function (pi: any) {
  pi.setLabel("rig permissions guard");

  // Positive heartbeat: the guard only fires on a tool NAMED "bash" — if omp renamed the
  // tool, no per-event check can see it, so verify the registry once per session and drop
  // the loud marker rig status surfaces (and clear a stale one when healthy again).
  pi.on("session_start", async (_event: any, ctx: any) => {
    try {
      const tools = typeof pi.getAllTools === "function" ? pi.getAllTools() : [];
      const names = new Set((tools ?? []).map((t: any) => t && t.name));
      if (names.size > 0 && !names.has("bash")) {
        // a bash-LESS registry is NOT incompatible: restricted toolsets are legitimate (no
        // shell exists to guard). Notify only — the per-call fail-closed checks below cover
        // a renamed toolName field; writing the marker here would flag restricted profiles
        // as permanent false-positive drift.
        if (ctx.hasUI && ctx.ui && typeof ctx.ui.notify === "function") {
          ctx.ui.notify("rig guard: no 'bash' tool in this session — nothing to guard", "info");
        }
      }
      // no clearMarker here: a session_start heartbeat can't contradict a tool_call shape
      // failure — only the tool_call path below clears (and rig apply is the operator path).
    } catch { /* registry probe failed — the per-call checks below still apply */ }
  });

  pi.on("tool_call", async (event: any, ctx: any) => {
    // Runtime event-shape self-check, BEFORE the fast path: if omp dropped the toolName
    // field, fail CLOSED (block + loud marker) instead of silently enforcing nothing.
    if (typeof event.toolName !== "string") {
      await writeMarker("tool_call event shape changed: toolName is not a string");
      return { block: true, reason: "rig guard: incompatible omp event shape (toolName) — blocking fail-closed until `rig apply` regenerates" };
    }
    // eval-tool code scan: a SEPARATE surface from bash argv — `code_pattern` rules (scope
    // ["eval"]) match the eval cell's source text instead of a tokenized command. Missing/
    // non-string `code` is NOT a shape failure (many eval calls carry other fields); it is
    // simply nothing to scan.
    if (event.toolName === "eval") {
      const code = event.input && typeof event.input.code === "string" ? event.input.code : null;
      if (code === null) return;
      for (const rule of DENY) {
        if (rule.matcher === "code_pattern" && rule.scope.includes("eval") && matchesCodePattern(rule, code)) {
          return { block: true, reason: "rig guard deny " + rule.id + ": " + rule.hint };
        }
      }
      for (const rule of ASK) {
        if (!(rule.matcher === "code_pattern" && rule.scope.includes("eval") && matchesCodePattern(rule, code))) continue;
        if (ctx.hasUI && ctx.ui && typeof ctx.ui.confirm === "function") {
          const ok = await ctx.ui.confirm("rig guard", rule.hint + "\n\nAllow this eval cell?");
          if (!ok) return { block: true, reason: "rig guard ask " + rule.id + " (rejected): " + rule.hint };
        } else {
          return { block: true, reason: "rig guard ask " + rule.id + " (headless — no confirm UI): " + rule.hint };
        }
      }
      return;
    }
    if (event.toolName !== "bash") return; // fast-path: only shell commands and eval code are guarded
    const input = event.input;
    if (!input || typeof input.command !== "string") {
      await writeMarker("tool_call event shape changed: input.command is not a string");
      return { block: true, reason: "rig guard: incompatible omp event shape (input.command) — blocking fail-closed until `rig apply` regenerates" };
    }
    await clearMarker(); // a healthy shape check is the evidence that contradicts the marker
    for (const finding of evaluateBash(input.command)) {
      const rule = finding.rule;
      if (finding.kind === "deny") return { block: true, reason: "rig guard deny " + rule.id + ": " + rule.hint };
      if (ctx.hasUI && ctx.ui && typeof ctx.ui.confirm === "function") {
        const ok = await ctx.ui.confirm("rig guard", rule.hint + "\n\nAllow this command?");
        if (!ok) return { block: true, reason: "rig guard ask " + rule.id + " (rejected): " + rule.hint };
        // confirmed — keep walking the findings (a later stage may still deny)
      } else {
        return { block: true, reason: "rig guard ask " + rule.id + " (headless — no confirm UI): " + rule.hint };
      }
    }
  });
}

// Check entrypoint: `bun rig-permissions-guard.ts '<command>'` prints the verdict the bash tool
// would get for that command — `deny <id>: <hint>` (exit 2), `ask <id>: <hint>` (exit 1) or
// `allow` (exit 0). A deny anywhere wins: confirming the asks would still hit it. Inert when omp
// imports this file as an extension (import.meta.main is false then).
if ((import.meta as any).main) {
  const findings = evaluateBash(process.argv[2] ?? "");
  const hit = findings.find((f) => f.kind === "deny") ?? findings[0];
  process.stdout.write((hit ? hit.kind + " " + hit.rule.id + ": " + hit.rule.hint : "allow") + "\n");
  process.exit(hit ? (hit.kind === "deny" ? 2 : 1) : 0);
}
"""


def guard_provenance(content: str) -> dict | None:
    """Parse the provenance header back out of a generated file. ``None`` for a foreign file."""
    for line in content.splitlines()[:5]:
        if line.startswith(_PROVENANCE_PREFIX):
            try:
                prov = json.loads(line[len(_PROVENANCE_PREFIX):])
            except json.JSONDecodeError:
                return None
            return prov if isinstance(prov, dict) and "template" in prov else None
    return None
