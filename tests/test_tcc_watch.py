"""tcc-watch dialog-watcher daemon — config, pure plist/script rendering, install, detection.

Phase 1 (detection-only) of the defense-in-depth design behind the eval-tool TCC deny-list
(``riglib/permissions.py``, PR #488) — see :mod:`riglib.tcc_watch`'s module docstring for the
full phased design and :mod:`riglib.tcc_watch_daemon` for the detection logic under test here.

Mirrors :mod:`test_tg_ctl`'s shape: stdlib-only pure render tests for the plist/script, HOME-
isolated install/idempotency/conflict tests that NEVER touch the real ``~/Library/LaunchAgents``
or run a real ``launchctl`` (the gui-domain seams are stubbed per test), plus a dedicated section
replaying CANNED/sample ``log stream`` output through the daemon's parsing + watch-loop logic —
no real macOS permission dialog is ever triggered or required.
"""

from __future__ import annotations

import dataclasses
import json
import os
import plistlib
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from riglib import tcc_watch
from riglib import tcc_watch_daemon as daemon
from riglib.actions import runner
from riglib.config import ConfigError, validate

# captured at import (before any monkeypatch runs) — the genuine handler.
_REAL_PROVISION = runner._do_provision_tcc_watch


@pytest.fixture(autouse=True)
def _real_tcc_watch(monkeypatch, live_launchd_home):
    """Restore the REAL provision_tcc_watch handler for THIS module's tests.

    conftest's autouse ``_isolate_scheduler`` stubs ``_do_provision_tcc_watch`` (+ the
    ``_HANDLERS`` entry) to a no-op so e2e tests can't touch the host launchd. These dedicated
    tests EXERCISE the real install logic — with HOME-isolated tmp dirs + stubbed launchctl
    seams — so they restore the real implementation.
    """
    monkeypatch.setattr(runner, "_do_provision_tcc_watch", _REAL_PROVISION)
    monkeypatch.setitem(runner._HANDLERS, "provision_tcc_watch", _REAL_PROVISION)
    # These tests assert on the LIVE (non-dry) provisioning path by default. A leaked ambient
    # RIG_TCC_WATCH_DRY_RUN would silently route them through the dry-run branch.
    monkeypatch.delenv("RIG_TCC_WATCH_DRY_RUN", raising=False)


def _force_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")


def _isolate_home(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


# ── config validation ───────────────────────────────────────────────────────────────────
def test_tcc_watch_block_accepted():
    validate({"version": 1, "tcc_watch": {"enabled": True}})


def test_tcc_watch_block_empty_ok():
    validate({"version": 1, "tcc_watch": {}})


def test_tcc_watch_full_block_accepted():
    validate({
        "version": 1,
        "tcc_watch": {
            "enabled": True,
            "label": "ai.hyperide.tcc-watch",
            "config_dir": "~/.config/rig/tcc-watch",
            "python_bin": "/usr/bin/python3",
        },
    })


def test_tcc_watch_unknown_key_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tcc_watch": {"nope": 1}})


def test_tcc_watch_enabled_must_be_bool():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tcc_watch": {"enabled": "yes"}})


def test_tcc_watch_enabled_null_is_accepted():
    """`enabled: null` (explicit None) is valid and is NOT treated as disabled (default-on)."""
    validate({"version": 1, "tcc_watch": {"enabled": None}})


@pytest.mark.parametrize("key", ["label", "config_dir", "python_bin"])
def test_tcc_watch_string_keys_reject_non_string(key):
    with pytest.raises(ConfigError):
        validate({"version": 1, "tcc_watch": {key: 123}})


def test_tcc_watch_not_a_mapping_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tcc_watch": "nope"})


# ── pure plist rendering ─────────────────────────────────────────────────────────────────
def _plan(home="/Users/ultra", **over):
    over.setdefault("python_bin", str(Path(home) / "bin" / "python3"))
    return tcc_watch.build_tcc_watch(home=Path(home), **over)


def test_render_is_well_formed_and_labelled():
    p = _plan()
    body = p.render_plist()
    parsed = plistlib.loads(body.encode("utf-8"))
    assert parsed["Label"] == tcc_watch.DEFAULT_LABEL
    assert parsed["ProgramArguments"] == [str(p.python_bin), str(p.watcher_script_path), str(p.store_path)]
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True


def test_render_preserves_key_order_not_sorted():
    body = _plan().render_plist()
    assert body.index("<key>Label</key>") < body.index("<key>ProgramArguments</key>")
    assert body.index("<key>RunAtLoad</key>") < body.index("<key>StandardOutPath</key>")


def test_render_is_deterministic():
    assert _plan().render_plist() == _plan().render_plist()


def test_label_is_configurable():
    p = _plan(label="com.me.tcc-watch")
    assert plistlib.loads(p.render_plist().encode("utf-8"))["Label"] == "com.me.tcc-watch"
    assert p.plist_path.name == "com.me.tcc-watch.plist"


def test_paths_resolve_against_injected_home():
    p = _plan(home="/home/u", python_bin="/usr/bin/python3")
    assert p.plist_path == Path("/home/u/Library/LaunchAgents/ai.hyperide.tcc-watch.plist")
    assert p.config_dir == Path("/home/u/.config/rig/tcc-watch")
    assert p.store_path == Path("/home/u/.config/rig/tcc-watch/tcc-watch.sqlite3")
    assert p.watcher_script_path == Path("/home/u/.config/rig/tcc-watch/watch.py")


def test_explicit_config_dir_is_honored():
    p = _plan(config_dir="/var/tcc-watch")
    assert p.config_dir == Path("/var/tcc-watch")
    assert p.out_log_path == Path("/var/tcc-watch/launchd.tcc-watch.out.log")


def test_python_bin_falls_back_to_discovered_when_not_given(monkeypatch):
    monkeypatch.setattr(tcc_watch.shutil, "which", lambda name: "/opt/homebrew/bin/python3")
    p = tcc_watch.build_tcc_watch(home=Path("/home/u"))
    assert p.python_bin == Path("/opt/homebrew/bin/python3")


def test_python_bin_falls_back_to_system_path_when_not_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(tcc_watch.shutil, "which", lambda name: None)
    fake_sys_python = tmp_path / "usr_bin_python3"
    fake_sys_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(tcc_watch, "_PYTHON3_FALLBACK_PATHS", (str(fake_sys_python),))
    p = tcc_watch.build_tcc_watch(home=Path("/home/u"))
    assert p.python_bin == fake_sys_python


# ── watcher script rendering: one source of truth ────────────────────────────────────────
def test_render_watcher_script_matches_daemon_module_source():
    """The rendered script is byte-for-byte the SAME source this test file imports and exercises
    directly — no drift between the tested detection logic and the deployed daemon."""
    p = _plan()
    assert p.render_watcher_script() == Path(daemon.__file__).read_text(encoding="utf-8")


def test_render_watcher_script_is_valid_python():
    compile(_plan().render_watcher_script(), "<watch.py>", "exec")


# ── detection logic: parse_observation, replayed against CAPTURED log-stream lines ─────────
# Every record below has the exact shape `log stream --style ndjson --debug` emitted on a real
# Mac (rig-cli#607); only bundle ids, paths and pids are neutralised. No real permission prompt
# is triggered to run these tests.

_TCCD = "/System/Library/PrivateFrameworks/TCC.framework/Support/tccd"
_TS = "2026-09-15 16:58:21.810739+0200"


def _tcc_line(message: str, *, fmt: str | None = "%{public}s", subsystem: str = "com.apple.TCC",
              pid: int = 672, process: str = _TCCD, ts: str = _TS, category: str = "access") -> str:
    record = {
        "timestamp": ts,
        "subsystem": subsystem,
        "category": category,
        "eventMessage": message,
        "processID": pid,
        "processImagePath": process,
    }
    if fmt is not None:
        record["formatString"] = fmt
    return json.dumps(record)


_REAL_PROMPT_MESSAGES = [
    ("prompting", "kTCCServiceMediaLibrary",
     "AUTHREQ_PROMPTING: msgID=399.123, service=kTCCServiceMediaLibrary, "
     "subject=Sub:{com.example.agent}Resp:{TCCDProcess: identifier=com.example.agent, pid=64810, "
     "auid=501, euid=501, binary_path=/Applications/Example.app/Contents/MacOS/Example},"),
    ("prompt_displayed", "kTCCServiceSystemPolicyDesktopFolder",
     "display_prompt: called for <private> for service kTCCServiceSystemPolicyDesktopFolder"),
    ("prompt_answered", "kTCCServiceMediaLibrary",
     "CFUserNotification response: 0x1; service kTCCServiceMediaLibrary and subject com.example.agent"),
    ("denied_without_prompt", "kTCCServicePhotos",
     "Policy disallows prompt for Sub:{com.example.agent}Resp:{TCCDProcess: "
     "identifier=com.example.agent, pid=64810, auid=501, euid=501, "
     "binary_path=/Applications/Example.app/Contents/MacOS/Example}; access to kTCCServicePhotos denied"),
    ("access_record_updated", "kTCCServiceAccessibility",
     "Update Access Record: kTCCServiceAccessibility for com.example.term to Denied (System Set) (v1) "
     "at 1790242997 (2026-09-24 09:43:17 +0000)\n\t CodeReq: identifier \"com.example.term\" and "
     "anchor apple generic\n\t Indirect : Unused"),
]

# The routine tccd chatter that made up 99.99% of the 2.1 GB — counted, never stored in full.
_ROUTINE_MESSAGES = [
    "AttributionChain: requesting={TCCDProcess: identifier=com.example.chat, pid=8864, auid=501, "
    "euid=501, binary_path=/Applications/Chat.app/Contents/MacOS/Chat},",
    "SEND: 0/7 synchronous to com.apple.tccd: request: msgID=66412.232, function=TCCAccessRequest, "
    "service=kTCCServiceAddressBook,",
    "SEND: 0/7 synchronous to com.apple.tccd.system: request: msgID=412.9, "
    "function=TCCAccessCopyInformation, service=kTCCServiceAccessibility,",
    "REQUEST_MSG: msgID=517.35048, msg={\n\trequire_purpose=<xpc_null>\n\t"
    "service=\"kTCCServiceDeveloperTool\"\n\tfunction=\"TCCAccessRequest\"\n\tpreflight=true\n}",
    "AUTHREQ_CTX: msgID=8864.1, function=<private>, service=kTCCServiceListenEvent, preflight=yes, "
    "query=1, client_dict=(null), daemon_dict=<private>",
    "<TCCDProcess: identifier=com.apple.syspolicyd, pid=517> has the "
    "com.apple.private.tcc.manager.check-by-audit-token entitlement for service "
    "kTCCServiceDeveloperTool (composed to parent: (null))",
    "Service kTCCServiceDeveloperTool does not allow prompting; returning denied.",
    "Prompting policy for hardened runtime; allow prompt: Allow",
    "handle_TCCAccessCopyInformation(): failed to find an Application URL for bundle ID: com.example.gone.",
    "Update Access Record: checking child service kTCCServicePostEvent of kTCCServiceAccessibility",
    "REPLY_MSG: msg={\n\tservices=\n\t[0] = {\n\t\tservice=\"kTCCServiceLiverpool\"\n\t\tgranted=true\n\t}\n}",
]


@pytest.mark.parametrize(("kind", "service", "message"), _REAL_PROMPT_MESSAGES)
def test_parse_classifies_real_prompts_and_denials(kind, service, message):
    obs = daemon.parse_observation(_tcc_line(message, pid=4242))
    assert obs is not None
    assert obs.kind == kind and obs.is_prompt
    assert obs.service == service
    assert obs.pid == 4242
    assert obs.process == "tccd" and obs.process_path == _TCCD
    assert obs.message == message


@pytest.mark.parametrize("message", _ROUTINE_MESSAGES)
def test_parse_classifies_routine_chatter_as_noise(message):
    """rig-cli#607: preflight/attribution/entitlement/read-only lines are NOT prompts."""
    obs = daemon.parse_observation(_tcc_line(message))
    assert obs is not None
    assert obs.kind == daemon.NOISE and not obs.is_prompt


def test_parse_keys_a_line_by_its_format_string_and_extracts_the_call():
    fmt = ("SEND: %d/%d synchronous to %{public}s: request: msgID=%{public}s, function=%{public}s, "
           "service=%{public}@,")
    obs = daemon.parse_observation(_tcc_line(_ROUTINE_MESSAGES[2], fmt=fmt, process="/usr/libexec/ua"))
    assert obs.template == fmt
    assert obs.process == "ua"
    assert obs.service == "kTCCServiceAccessibility"
    assert obs.function == "TCCAccessCopyInformation"
    assert obs.key == ("ua", "access", fmt, "kTCCServiceAccessibility", "TCCAccessCopyInformation", "", "noise")


@pytest.mark.parametrize(("message", "function"), [
    (_ROUTINE_MESSAGES[3], "TCCAccessRequest preflight"),  # REQUEST_MSG, quoted function
    (_ROUTINE_MESSAGES[4], "preflight"),  # AUTHREQ_CTX, function=<private>
    (_ROUTINE_MESSAGES[1], "TCCAccessRequest"),
    (_ROUTINE_MESSAGES[0], ""),
])
def test_parse_marks_the_tcc_call_and_preflight(message, function):
    assert daemon.parse_observation(_tcc_line(message)).function == function


@pytest.mark.parametrize(("message", "subject"), [
    (_ROUTINE_MESSAGES[0], "com.example.chat"),
    (_REAL_PROMPT_MESSAGES[3][2], "com.example.agent"),
    ("Policy disallows prompt for Sub:{/usr/libexec/routined}Resp:{...}; access to kTCCServiceWillow denied",
     "/usr/libexec/routined"),
    (_ROUTINE_MESSAGES[1], ""),
])
def test_parse_extracts_the_subject_app(message, subject):
    assert daemon.parse_observation(_tcc_line(message)).subject == subject


def test_parse_without_a_format_string_falls_back_to_the_leading_token():
    obs = daemon.parse_observation(_tcc_line(_ROUTINE_MESSAGES[5], fmt=None))
    assert obs.template == "<TCCDProcess"
    assert daemon.parse_observation(_tcc_line("", fmt=None)).template == "(empty)"


def test_parse_drops_malformed_timestamp_and_pid_fields():
    line = json.dumps({
        "subsystem": "com.apple.TCC", "eventMessage": "display_prompt: called for <private>",
        "timestamp": {"not": "a string"}, "processID": True,
    })
    obs = daemon.parse_observation(line)
    assert obs.epoch is None and obs.timestamp == ""
    assert obs.pid is None
    assert obs.process == "?" and obs.process_path is None


def test_parse_timestamp_to_epoch():
    assert daemon.parse_observation(_tcc_line("x", ts="2026-09-15 16:58:21.810739+0200")).epoch == 1789484301
    assert daemon.parse_observation(_tcc_line("x", ts="2026-09-15 16:58:21+0200")).epoch == 1789484301
    assert daemon.parse_observation(_tcc_line("x", ts="yesterday")).epoch is None


@pytest.mark.parametrize("line", [
    "", "   \n", "{not valid json", "[1, 2, 3]", '"just a string"',
    'Filtering the log data using "subsystem == \\"com.apple.TCC\\""',
    _tcc_line("display_prompt: x", subsystem="com.apple.something.else"),
])
def test_parse_ignores_anything_that_is_not_a_tcc_record(line):
    assert daemon.parse_observation(line) is None


def test_parse_caps_an_oversize_message():
    obs = daemon.parse_observation(_tcc_line("display_prompt: " + "x" * 20000))
    assert len(obs.message) == daemon.MAX_MESSAGE_CHARS


def test_log_stream_reads_the_whole_tcc_subsystem():
    """Everything is counted at the source now — the predicate no longer drops the chatter."""
    cmd = daemon.LOG_STREAM_CMD
    assert cmd[:2] == ["log", "stream"]
    assert "--debug" in cmd
    assert cmd[cmd.index("--predicate") + 1] == 'subsystem == "com.apple.TCC"'


def test_spawn_log_stream_decodes_leniently(monkeypatch):
    """A non-UTF-8 byte in a log message must not raise out of the read loop."""
    seen: dict = {}

    def fake_popen(cmd, **kwargs):
        seen.update(cmd=cmd, **kwargs)
        return "proc"

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)
    assert daemon._spawn_log_stream() == "proc"
    assert seen["cmd"] == daemon.LOG_STREAM_CMD
    assert seen["text"] is True
    assert seen["errors"] == "replace"


# ── the store ─────────────────────────────────────────────────────────────────────────────
_T0 = 1789484400  # an hour boundary: 2026-09-15 15:00:00 UTC


def _obs(message: str, *, epoch: int = _T0, fmt: str = "%{public}s", process: str = _TCCD):
    obs = daemon.parse_observation(_tcc_line(message, fmt=fmt, process=process))
    return dataclasses.replace(obs, epoch=epoch)


@pytest.fixture
def store(tmp_path):
    s = daemon.TccStore(tmp_path / "tcc-watch" / daemon.STORE_NAME, now=lambda: _T0)
    yield s
    s.db.close()


def _counts(s) -> list[tuple]:
    return s.db.execute(
        "SELECT c.bucket, c.span, t.format, t.subject, c.n FROM counts c JOIN templates t ON t.id = c.template_id"
        " ORDER BY c.bucket, t.format, t.subject"
    ).fetchall()


def test_store_counts_lines_per_hour_per_template(store):
    for _ in range(3):
        store.observe(_obs(_ROUTINE_MESSAGES[0], epoch=_T0 + 10))
    store.observe(_obs(_ROUTINE_MESSAGES[0], epoch=_T0 + 3600 + 5))
    store.observe(_obs("SEND: other", epoch=_T0 + 20, fmt="SEND: %d"))
    assert _counts(store) == []  # buffered until flush
    store.flush()
    assert _counts(store) == [
        (_T0, 3600, "%{public}s", "com.example.chat", 3),
        (_T0, 3600, "SEND: %d", "", 1),
        (_T0 + 3600, 3600, "%{public}s", "com.example.chat", 1),
    ]
    example = store.db.execute("SELECT example FROM templates WHERE format = '%{public}s'").fetchone()[0]
    assert example == _ROUTINE_MESSAGES[0]


def test_store_counts_accumulate_across_flushes_and_reopens(store, tmp_path):
    store.observe(_obs(_ROUTINE_MESSAGES[1]))
    store.flush()
    store.observe(_obs(_ROUTINE_MESSAGES[1]))
    store.close()
    again = daemon.TccStore(store.path)
    again.observe(_obs(_ROUTINE_MESSAGES[1]))
    again.flush()
    assert again.totals() == (3, 0)
    assert again.db.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 1
    again.db.close()


def test_store_records_its_schema_version(store):
    row = store.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row == (str(daemon.SCHEMA_VERSION),)


def test_store_keeps_prompts_in_full_and_commits_them_at_once(store):
    kind, service, message = _REAL_PROMPT_MESSAGES[0]
    store.observe(_obs(message))
    other = sqlite3.connect(str(store.path))
    row = other.execute("SELECT ts, kind, service, process_path, pid, message FROM prompts").fetchone()
    other.close()
    assert row == (_TS, kind, service, _TCCD, 672, message)


def test_store_maintain_rolls_old_hours_into_days_and_keeps_the_total(store):
    old_day = _T0 - 20 * 86400 - _T0 % 86400
    for hour in (1, 2, 5):
        for _ in range(hour):
            store.observe(_obs(_ROUTINE_MESSAGES[0], epoch=old_day + hour * 3600))
    store.observe(_obs(_ROUTINE_MESSAGES[0], epoch=_T0))
    store.flush()
    assert store.totals() == (9, 0)
    store.maintain()
    assert store.totals() == (9, 0)
    assert _counts(store) == [
        (old_day, 86400, "%{public}s", "com.example.chat", 8),
        (_T0, 3600, "%{public}s", "com.example.chat", 1),
    ]
    # a second rollup of the same day (more late hours) merges into the same daily row
    store.observe(_obs(_ROUTINE_MESSAGES[0], epoch=old_day + 7 * 3600))
    store.maintain()
    assert _counts(store)[0] == (old_day, 86400, "%{public}s", "com.example.chat", 9)


def test_store_maintain_trims_prompts_and_diagnostics(store, monkeypatch):
    monkeypatch.setattr(daemon, "MAX_PROMPT_ROWS", 3)
    monkeypatch.setattr(daemon, "MAX_DIAGNOSTIC_ROWS", 2)
    for i in range(6):
        store.observe(_obs(f"display_prompt: called for <private> for service kTCCServicePhotos #{i}"))
        store.diagnostic(f"note {i}")
    store.maintain()
    msgs = [r[0] for r in store.db.execute("SELECT message FROM prompts ORDER BY id")]
    assert [m[-2:] for m in msgs] == ["#3", "#4", "#5"]
    assert [r[0] for r in store.db.execute("SELECT message FROM diagnostics ORDER BY id")] == ["note 4", "note 5"]


def test_store_size_is_bounded_by_templates_not_by_volume(store):
    """The 2.1 GB of #607 was ~180 templates repeated: 100k lines must stay a few KB of rows."""
    for i in range(100_000):
        store.observe(_obs(_ROUTINE_MESSAGES[i % len(_ROUTINE_MESSAGES)], epoch=_T0 + i % 3600))
    store.flush()
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert store.totals() == (100_000, 0)
    assert store.db.execute("SELECT COUNT(*) FROM counts").fetchone()[0] == len({_obs(m).key for m in _ROUTINE_MESSAGES})
    assert store.path.stat().st_size < 64 * 1024


def test_store_summary_answers_what_fills_the_log(store):
    for i in range(90):
        store.observe(_obs(_ROUTINE_MESSAGES[0], process="/usr/libexec/syspolicyd"))
    for i in range(9):
        store.observe(_obs(_ROUTINE_MESSAGES[1], process="/usr/sbin/contactsd"))
    store.observe(_obs(_REAL_PROMPT_MESSAGES[1][2]))
    summary = store.summary(since=_T0)
    assert summary["lines"] == 100
    assert summary["daily_utc"] == [{"day": "2026-09-15", "lines": 100}]
    assert summary["by_kind"][0] == {"kind": "noise", "lines": 99}
    assert {"kind": "prompt_displayed", "lines": 1} in summary["by_kind"]
    assert summary["by_process"][0] == {"process": "syspolicyd", "lines": 90}
    top = summary["top_templates"][0]
    assert (top["process"], top["subject"], top["lines"]) == ("syspolicyd", "com.example.chat", 90)
    assert summary["top_templates"][1]["function"] == "TCCAccessRequest"
    assert summary["recent_prompts"][0]["kind"] == "prompt_displayed"


# ── one-time import of the pre-#607 line log ───────────────────────────────────────────────
def _legacy_row(message: str, i: int = 0, *, pid: int | None = 672, process: str | None = _TCCD) -> dict:
    return {"message": message, "pid": pid, "process": process,
            "timestamp": f"2026-09-10 12:{i // 60 % 60:02d}:{i % 60:02d}.000000+0200"}


def _mirror(row: dict) -> str:
    """The line the pre-#607 daemon printed to stderr for each recorded row."""
    return (f"tcc-watch: permission-prompt event detected: {row['message']!r} "
            f"(pid={row['pid']}, process={row['process']})\n")


def _seed_legacy(directory: Path, name: str, rows: list[dict]) -> Path:
    path = directory / name
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
    return path


def _legacy_rows(n_chatter: int = 50) -> list[dict]:
    rows = []
    for i, (_, _, message) in enumerate(_REAL_PROMPT_MESSAGES):
        rows += [_legacy_row(_ROUTINE_MESSAGES[j % len(_ROUTINE_MESSAGES)], i) for j in range(n_chatter)]
        rows.append(_legacy_row(message, i))
    return rows


def test_import_legacy_family_counts_every_line_and_keeps_every_prompt(store):
    d = store.path.parent
    older, newer = _legacy_rows(), _legacy_rows(10)
    _seed_legacy(d, "events.jsonl.1", older)
    _seed_legacy(d, "events.jsonl", newer)
    notes = daemon.import_legacy_logs(store, d)
    assert not (d / "events.jsonl").exists() and not (d / "events.jsonl.1").exists()
    assert store.totals() == (len(older) + len(newer), 2 * len(_REAL_PROMPT_MESSAGES))
    assert notes[0].startswith(f"imported events.jsonl.1: {len(older)} lines (5 prompts/denials)")
    kinds = [r[0] for r in store.db.execute("SELECT kind FROM prompts ORDER BY id")]
    assert kinds == [k for k, _, _ in _REAL_PROMPT_MESSAGES] * 2
    # counted per UTC day, under the legacy template
    assert store.db.execute("SELECT DISTINCT span FROM counts").fetchall() == [(86400,)]
    assert store.db.execute("SELECT DISTINCT category FROM templates").fetchall() == [("legacy",)]
    meta = json.loads(store.db.execute("SELECT value FROM meta WHERE key = 'legacy_import:events.jsonl'").fetchone()[0])
    assert (meta["lines"], meta["prompts"]) == (len(newer), 5)


def test_import_rolls_back_and_keeps_the_file_when_totals_do_not_verify(store, monkeypatch):
    d = store.path.parent
    path = _seed_legacy(d, "events.jsonl", _legacy_rows())
    real_totals = daemon.TccStore.totals
    calls = {"n": 0}

    def off_by_one(self):
        calls["n"] += 1
        lines, prompts = real_totals(self)
        return (lines + 1, prompts) if calls["n"] == 2 else (lines, prompts)

    monkeypatch.setattr(daemon.TccStore, "totals", off_by_one)
    notes = daemon.import_legacy_logs(store, d)
    monkeypatch.setattr(daemon.TccStore, "totals", real_totals)
    assert path.exists()
    assert store.totals() == (0, 0)
    assert store.db.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0
    assert "kept it" in notes[0]


def test_import_does_not_double_count_a_file_it_could_not_remove(store):
    d = store.path.parent
    rows = _legacy_rows()
    path = _seed_legacy(d, "events.jsonl", rows)
    backup = d / "keep"
    backup.write_bytes(path.read_bytes())
    stat_before = path.stat()
    daemon.import_legacy_logs(store, d)
    backup.rename(path)  # as if the unlink had failed: same bytes, same mtime
    os.utime(path, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    notes = daemon.import_legacy_logs(store, d)
    assert notes == ["events.jsonl was already imported; removed it"]
    assert store.totals() == (len(rows), len(_REAL_PROMPT_MESSAGES))


def test_the_mirror_format_matches_a_line_the_pre_607_daemon_really_printed():
    """Captured from the owner's launchd.tcc-watch.err.log on 2026-09-26 (the pre-#607 daemon
    printed f"tcc-watch: permission-prompt event detected: {event.message!r} (pid=..., process=...)").
    Stripping is verified against this format, so it must match history, not just itself."""
    captured = ("tcc-watch: permission-prompt event detected: 'SEND: 0/7 synchronous to com.apple.tccd.system:"
                " request: msgID=517.35048, function=TCCAccessRequest, service=kTCCServiceDeveloperTool,"
                " target_token={pid:8864, auid:501, euid:501},' (pid=517, process=/usr/libexec/syspolicyd)")
    row = {"message": "SEND: 0/7 synchronous to com.apple.tccd.system: request: msgID=517.35048, "
                      "function=TCCAccessRequest, service=kTCCServiceDeveloperTool, "
                      "target_token={pid:8864, auid:501, euid:501},",
           "pid": 517, "process": "/usr/libexec/syspolicyd"}
    assert daemon._LEGACY_MIRROR.format(**row) == captured
    assert _mirror(row) == captured + "\n"


def test_an_interrupted_strip_survives_err_log_having_been_deleted(store):
    d = store.path.parent
    (d / f"{daemon.LEGACY_ERR_LOG_NAME}.kept").write_text("kept line\n", encoding="utf-8")
    daemon.import_legacy_logs(store, d)
    assert (d / daemon.LEGACY_ERR_LOG_NAME).read_text(encoding="utf-8") == "kept line\n"


def test_import_strips_err_log_mirror_lines_only_once_all_are_verified(store):
    d = store.path.parent
    rows = _legacy_rows(3)
    _seed_legacy(d, "events.jsonl", rows)
    err = d / daemon.LEGACY_ERR_LOG_NAME
    traceback = "Traceback (most recent call last):\nOSError: [Errno 28] No space left on device\n"
    # the last row has no mirror line: the old daemon died between the append and the print
    err.write_text("".join(_mirror(r) for r in rows[:10]) + traceback + "".join(_mirror(r) for r in rows[10:-1]),
                   encoding="utf-8")
    notes = daemon.import_legacy_logs(store, d)
    assert err.read_text(encoding="utf-8") == traceback
    assert notes[-1] == f"err.log: dropped {len(rows) - 1} verified duplicate event lines, kept 2 other lines"


def test_import_leaves_err_log_alone_when_a_mirror_line_has_no_row(store):
    d = store.path.parent
    rows = _legacy_rows(3)
    _seed_legacy(d, "events.jsonl", rows)
    err = d / daemon.LEGACY_ERR_LOG_NAME
    stray = _mirror(_legacy_row("AttributionChain: never recorded"))
    content = "".join(_mirror(r) for r in rows[:5]) + stray + "".join(_mirror(r) for r in rows[5:])
    err.write_text(content, encoding="utf-8")
    notes = daemon.import_legacy_logs(store, d)
    assert err.read_text(encoding="utf-8") == content
    assert notes[-1].startswith(f"err.log: 5 of {len(rows) + 1} old event lines matched")


@pytest.mark.parametrize("value", ["3", "null", "not json{", "[1, 2]"])
def test_a_malformed_import_record_never_crashes_the_import(store, value):
    d = store.path.parent
    rows = _legacy_rows(2)
    path = _seed_legacy(d, "events.jsonl", rows)
    store.db.execute("INSERT INTO meta (key, value) VALUES ('legacy_import:events.jsonl', ?)", (value,))
    store.db.commit()
    notes = daemon.import_legacy_logs(store, d)
    assert notes == [f"imported events.jsonl: {len(rows)} lines (5 prompts/denials); removed it"]
    assert not path.exists()


def test_an_interrupted_first_start_is_resumed_and_the_strip_finished_later(store):
    """First start imports events.jsonl.1 and is stopped before events.jsonl: the verified
    mirror count is committed with .1, so the next start resumes there and strips err.log."""
    d = store.path.parent
    older, newer = _legacy_rows(2), _legacy_rows(3)
    _seed_legacy(d, "events.jsonl.1", older)
    _seed_legacy(d, "events.jsonl", newer)
    err = d / daemon.LEGACY_ERR_LOG_NAME
    err.write_text("".join(_mirror(r) for r in older) + "keep me\n" + "".join(_mirror(r) for r in newer),
                   encoding="utf-8")
    parked = d / "parked"
    (d / "events.jsonl").rename(parked)  # the first start never got to it
    notes = daemon.import_legacy_logs(store, d)
    assert notes[-1].endswith("left it as is") and "keep me" in err.read_text(encoding="utf-8")
    assert daemon._verified_mirror_lines(store) == len(older)
    parked.rename(d / "events.jsonl")
    notes = daemon.import_legacy_logs(store, d)
    assert err.read_text(encoding="utf-8") == "keep me\n"
    assert notes[-1] == f"err.log: dropped {len(older) + len(newer)} verified duplicate event lines, kept 1 other line"
    assert daemon._verified_mirror_lines(store) == 0
    assert store.totals() == (len(older) + len(newer), 2 * len(_REAL_PROMPT_MESSAGES))


def test_a_verified_strip_that_failed_is_retried_with_no_legacy_files_left(store):
    d = store.path.parent
    rows = _legacy_rows(2)
    err = d / daemon.LEGACY_ERR_LOG_NAME
    err.write_text("".join(_mirror(r) for r in rows) + "Traceback ...\n", encoding="utf-8")
    store.db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (daemon._MIRROR_VERIFIED_KEY, str(len(rows))))
    store.db.commit()
    notes = daemon.import_legacy_logs(store, d)
    assert err.read_text(encoding="utf-8") == "Traceback ...\n"
    assert notes[-1].startswith(f"err.log: dropped {len(rows)} verified duplicate event lines")


def test_a_file_growing_during_its_import_is_not_imported(store, monkeypatch):
    d = store.path.parent
    path = _seed_legacy(d, "events.jsonl", _legacy_rows())
    real = daemon._file_identity
    calls = {"n": 0}

    def growing(p):
        calls["n"] += 1
        identity = real(p)
        return {**identity, "size": identity["size"] + 100} if calls["n"] > 1 else identity

    monkeypatch.setattr(daemon, "_file_identity", growing)
    notes = daemon.import_legacy_logs(store, d)
    assert notes == ["could not import events.jsonl (the file changed while it was being imported);"
                     " kept it and the files after it"]
    assert path.exists() and store.totals() == (0, 0)


def test_a_failed_file_stops_the_import_so_no_unimported_line_is_ever_verified(store, monkeypatch):
    """.3 walks its mirror lines and then rolls back: .2 must not commit a verified count that
    includes them, or a later start (with .3 gone) would strip lines whose rows were never stored."""
    d = store.path.parent
    oldest, older = _legacy_rows(2), _legacy_rows(3)
    _seed_legacy(d, "events.jsonl.3", oldest)
    _seed_legacy(d, "events.jsonl.2", older)
    err = d / daemon.LEGACY_ERR_LOG_NAME
    content = "".join(_mirror(r) for r in oldest + older)
    err.write_text(content, encoding="utf-8")
    real_totals = daemon.TccStore.totals
    calls = {"n": 0}

    def first_file_off_by_one(self):
        calls["n"] += 1
        lines, prompts = real_totals(self)
        return (lines + 1, prompts) if calls["n"] == 2 else (lines, prompts)

    monkeypatch.setattr(daemon.TccStore, "totals", first_file_off_by_one)
    notes = daemon.import_legacy_logs(store, d)
    monkeypatch.setattr(daemon.TccStore, "totals", real_totals)
    assert notes[0] == ("could not import events.jsonl.3 (store totals do not match the file);"
                        " kept it and the files after it")
    assert (d / "events.jsonl.3").exists() and (d / "events.jsonl.2").exists()  # .2 not attempted
    assert daemon._verified_mirror_lines(store) == 0
    (d / "events.jsonl.3").unlink()  # even if the failed file then disappears...
    daemon.import_legacy_logs(store, d)
    assert err.read_text(encoding="utf-8") == content  # ...its lines are never "verified" away


def test_readonly_store_never_creates_a_file(tmp_path):
    missing = tmp_path / "gone" / daemon.STORE_NAME
    with pytest.raises(sqlite3.OperationalError):
        daemon.TccStore(missing, readonly=True)
    assert not missing.exists() and not missing.parent.exists()


def test_import_with_no_legacy_files_is_a_no_op(store):
    assert daemon.import_legacy_logs(store, store.path.parent) == []


# ── the watch loop, fully injected (no subprocess, no real log stream) ─────────────────────
class _FakeStreamProc:
    """A fake `log stream` subprocess handle: replays canned lines, then reports an exit code."""

    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self):
        return self.returncode


def _store_path(tmp_path) -> Path:
    d = tmp_path / "tcc-watch"
    d.mkdir(exist_ok=True)
    return d / daemon.STORE_NAME


def _reopen(path):
    return daemon.TccStore(path)


def test_run_counts_every_line_stores_prompts_and_never_echoes_them(tmp_path, capsys):
    """rig-cli#607: every event used to be printed to stderr too (launchd's err.log, 2.0 GB)."""
    path = _store_path(tmp_path)
    lines = [_tcc_line(m) for m in _ROUTINE_MESSAGES] * 20
    lines += [_tcc_line(m) for _, _, m in _REAL_PROMPT_MESSAGES]
    lines += ["Filtering the log data using ...", "{bad"]
    seen = daemon.run(path, stream_factory=lambda: _FakeStreamProc(lines), sleep=lambda _s: None, max_reconnects=1)
    assert seen == 20 * len(_ROUTINE_MESSAGES) + len(_REAL_PROMPT_MESSAGES)
    s = _reopen(path)
    assert s.totals() == (seen, len(_REAL_PROMPT_MESSAGES))
    s.db.close()
    err = capsys.readouterr().err
    assert err.count("\n") == 2  # "started" + "log stream exited" — diagnostics only
    for _, _, message in _REAL_PROMPT_MESSAGES:
        assert message[:30] not in err


def test_run_imports_the_legacy_log_before_the_stream_starts(tmp_path):
    path = _store_path(tmp_path)
    rows = _legacy_rows(2)
    legacy = _seed_legacy(path.parent, "events.jsonl", rows)
    at_spawn = []

    def factory():
        at_spawn.append(legacy.exists())
        return _FakeStreamProc([_tcc_line(_ROUTINE_MESSAGES[0])])

    daemon.run(path, stream_factory=factory, sleep=lambda _s: None, max_reconnects=1)
    assert at_spawn == [False]
    s = _reopen(path)
    assert s.totals() == (len(rows) + 1, len(_REAL_PROMPT_MESSAGES))
    notes = [r[0] for r in s.db.execute("SELECT message FROM diagnostics ORDER BY id")]
    s.db.close()
    assert any(n.startswith("imported events.jsonl:") for n in notes)


def test_run_flushes_buffered_counts_when_terminated(tmp_path):
    """SIGTERM (launchd bootout) raises SystemExit mid-stream; the counts still land."""
    path = _store_path(tmp_path)

    def stream():
        yield from [_tcc_line(_ROUTINE_MESSAGES[0])] * 7
        daemon._terminate(15, None)

    with pytest.raises(SystemExit):
        daemon.run(path, stream_factory=lambda: _FakeStreamProc(stream()), sleep=lambda _s: None)
    s = _reopen(path)
    assert s.totals() == (7, 0)
    s.db.close()


def test_run_reconnects_after_the_stream_exits(tmp_path):
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return _FakeStreamProc([_tcc_line(_REAL_PROMPT_MESSAGES[0][2])] if calls["n"] == 2 else [])

    sleeps: list[float] = []
    seen = daemon.run(_store_path(tmp_path), stream_factory=factory, sleep=sleeps.append, max_reconnects=2)
    assert calls["n"] == 2
    assert seen == 1
    assert sleeps


def test_run_backs_off_exponentially_on_spawn_failure(tmp_path):
    attempts = {"n": 0}

    def factory():
        attempts["n"] += 1
        raise OSError("log: command not found")

    sleeps: list[float] = []
    daemon.run(_store_path(tmp_path), stream_factory=factory, sleep=sleeps.append, max_reconnects=8)
    assert attempts["n"] == 8
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_run_backs_off_when_the_stream_keeps_exiting_immediately(tmp_path):
    """A crash-looping `log stream` must not write a diagnostic line every second."""
    sleeps: list[float] = []
    daemon.run(
        _store_path(tmp_path),
        stream_factory=lambda: _FakeStreamProc([], returncode=64),
        sleep=sleeps.append,
        clock=lambda: 0.0,
        max_reconnects=5,
    )
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_run_resets_the_backoff_after_a_healthy_stream(tmp_path):
    ticks = iter(range(0, 10_000, 100))  # every clock() call is 100s later
    sleeps: list[float] = []
    daemon.run(
        _store_path(tmp_path),
        stream_factory=lambda: _FakeStreamProc([]),
        sleep=sleeps.append,
        clock=lambda: float(next(ticks)),
        max_reconnects=4,
    )
    assert sleeps == [1.0, 1.0, 1.0, 1.0]


def test_run_survives_a_failing_store_write(tmp_path, monkeypatch, capsys):
    def locked(self, obs, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(daemon.TccStore, "observe", locked)
    seen = daemon.run(
        _store_path(tmp_path),
        stream_factory=lambda: _FakeStreamProc([_tcc_line(_REAL_PROMPT_MESSAGES[0][2])]),
        sleep=lambda _s: None,
        max_reconnects=1,
    )
    assert seen == 0
    err = capsys.readouterr().err
    assert "failed to record a prompting line (kTCCServiceMediaLibrary): database is locked" in err
    assert "AUTHREQ_PROMPTING" not in err  # prompt text never goes to launchd's err.log


# ── diagnostics: store always, stderr while small, never truncated ─────────────────────────
def test_log_err_records_in_the_store_and_on_stderr(store, tmp_path):
    err = tmp_path / "err.log"
    with err.open("a", encoding="utf-8") as fh:
        daemon._log_err("hello", store, stream=fh)
    assert err.read_text(encoding="utf-8") == "tcc-watch: hello\n"
    assert store.db.execute("SELECT message FROM diagnostics").fetchall() == [("hello",)]


def test_log_err_stops_writing_to_a_full_stderr_file_but_never_truncates_it(store, tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "MAX_STDERR_BYTES", 100)
    err = tmp_path / "err.log"
    err.write_text("x" * 150, encoding="utf-8")
    with err.open("a", encoding="utf-8") as fh:
        daemon._log_err("dropped from stderr", store, stream=fh)
    assert err.read_text(encoding="utf-8") == "x" * 150
    assert store.db.execute("SELECT message FROM diagnostics").fetchall() == [("dropped from stderr",)]


def test_stderr_has_room_for_pipes(tmp_path):
    r, w = os.pipe()
    with os.fdopen(w, "w") as pipe:
        assert daemon.stderr_has_room(pipe, 0) is True
    os.close(r)


def test_log_err_never_raises(tmp_path, monkeypatch, capsys):
    class _FullDisk:
        def fileno(self):
            raise OSError(28, "No space left on device")

        def write(self, _text):
            raise OSError(28, "No space left on device")

        def flush(self):
            raise OSError(28, "No space left on device")

    daemon._log_err("boom", stream=_FullDisk())
    closed = (tmp_path / "closed.log").open("a", encoding="utf-8")
    closed.close()
    daemon._log_err("boom", stream=closed)
    broken = daemon.TccStore(tmp_path / "s.sqlite3")
    broken.db.close()  # any write now raises sqlite3.ProgrammingError (a sqlite3.Error)
    daemon._log_err("boom", broken, stream=closed)
    monkeypatch.setattr(sys, "stderr", None)
    daemon._log_err("nowhere to go")
    assert capsys.readouterr().out == ""


# ── hardening: bounded cardinality, all-or-nothing writes, crash-safe upgrade ───────────────
def test_store_folds_new_templates_past_the_cap_into_one_overflow_row(store, monkeypatch):
    """A process logging under com.apple.TCC with non-constant format strings must not grow the
    store without bound: past MAX_TEMPLATES, new keys share one overflow template per kind."""
    monkeypatch.setattr(daemon, "MAX_TEMPLATES", 3)
    for i in range(50):
        store.observe(_obs(f"dynamic line {i}", fmt=f"dynamic format {i}"))
    store.flush()
    formats = [r[0] for r in store.db.execute("SELECT format FROM templates ORDER BY id")]
    assert formats == ["dynamic format 0", "dynamic format 1", "dynamic format 2", daemon.OVERFLOW_TEMPLATE]
    assert store.totals() == (50, 0)
    overflow = store.db.execute(
        "SELECT SUM(c.n) FROM counts c JOIN templates t ON t.id = c.template_id WHERE t.format = ?",
        (daemon.OVERFLOW_TEMPLATE,),
    ).fetchone()[0]
    assert overflow == 47


def test_legacy_template_folds_digits():
    assert daemon._legacy_template("SEND: 0/7 synchronous") == "SEND"
    assert daemon._legacy_template("msgID 12345 from pid 42") == "msgID # from pid #"


class _FailingDb:
    """Forwards to a real sqlite3 connection, but raises on SQL containing ``fail_on``."""

    def __init__(self, real, fail_on):
        self._real = real
        self._fail_on = fail_on

    def execute(self, sql, *args):
        if self._fail_on in sql:
            raise sqlite3.OperationalError("disk I/O error")
        return self._real.execute(sql, *args)

    def executemany(self, sql, *args):
        if self._fail_on in sql:
            raise sqlite3.OperationalError("disk I/O error")
        return self._real.executemany(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_a_failed_maintain_rolls_back_and_never_double_counts(store):
    old_day = _T0 - 20 * 86400 - _T0 % 86400
    for _ in range(4):
        store.observe(_obs(_ROUTINE_MESSAGES[0], epoch=old_day + 3600))
    store.flush()
    real = store.db
    store.db = _FailingDb(real, "DELETE FROM counts")  # the rollup INSERT succeeds, the DELETE fails
    with pytest.raises(sqlite3.OperationalError):
        store.maintain()
    store.db = real
    store.diagnostic("a later commit must not persist the half-done rollup")
    assert store.totals() == (4, 0)
    store.maintain()
    assert store.totals() == (4, 0)
    assert _counts(store) == [(old_day, 86400, "%{public}s", "com.example.chat", 4)]


def test_a_failed_count_write_is_rolled_back_not_replayed(store):
    store.observe(_obs(_ROUTINE_MESSAGES[0]))
    real = store.db
    store.db = _FailingDb(real, "INSERT INTO counts")
    with pytest.raises(sqlite3.OperationalError):
        store.flush()
    store.db = real
    store.flush()  # nothing left to replay: the failed batch was dropped, not half-applied
    assert store.totals() == (0, 0)
    store.observe(_obs(_ROUTINE_MESSAGES[0]))
    store.flush()
    assert store.totals() == (1, 0)


def test_run_survives_a_failing_startup_import(tmp_path, monkeypatch, capsys):
    def broken(*_args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(daemon, "import_legacy_logs", broken)
    seen = daemon.run(
        _store_path(tmp_path),
        stream_factory=lambda: _FakeStreamProc([_tcc_line(_ROUTINE_MESSAGES[0])]),
        sleep=lambda _s: None,
        max_reconnects=1,
    )
    assert seen == 1
    assert "startup import/maintenance failed: database is locked" in capsys.readouterr().err


def test_import_keeps_a_file_that_vanishes_mid_way(store, monkeypatch):
    d = store.path.parent
    path = _seed_legacy(d, "events.jsonl", _legacy_rows())

    def gone(_path):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(daemon, "_file_identity", gone)
    notes = daemon.import_legacy_logs(store, d)
    assert path.exists()
    assert notes == ["could not import events.jsonl ([Errno 2] No such file or directory); kept it and the files after it"]
    assert store.totals() == (0, 0)


def test_import_from_a_second_connection_sees_the_first_import(store):
    """Two daemons on one store: the identity check runs inside the IMMEDIATE transaction."""
    d = store.path.parent
    rows = _legacy_rows()
    path = _seed_legacy(d, "events.jsonl", rows)
    copy = d / "copy"
    copy.write_bytes(path.read_bytes())
    identity = path.stat()
    daemon.import_legacy_logs(store, d)
    copy.rename(path)
    os.utime(path, ns=(identity.st_atime_ns, identity.st_mtime_ns))
    other = daemon.TccStore(store.path)
    assert daemon.import_legacy_logs(other, d) == ["events.jsonl was already imported; removed it"]
    assert other.totals() == (len(rows), len(_REAL_PROMPT_MESSAGES))
    other.db.close()


def test_an_unreadable_err_log_is_reported_and_left_alone(store, monkeypatch):
    d = store.path.parent
    _seed_legacy(d, "events.jsonl", _legacy_rows(2))
    err = d / daemon.LEGACY_ERR_LOG_NAME
    err.write_text("tcc-watch: permission-prompt event detected: 'x' (pid=1, process=None)\n", encoding="utf-8")
    real_open = Path.open

    def deny(self, *args, **kwargs):
        if self == err:
            raise PermissionError(13, "Permission denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny)
    notes = daemon.import_legacy_logs(store, d)
    monkeypatch.setattr(Path, "open", real_open)
    assert notes[0].startswith("err.log: could not read it")
    assert err.read_text(encoding="utf-8").startswith("tcc-watch: permission-prompt event detected")


def test_an_interrupted_strip_is_finished_on_the_next_start(store):
    d = store.path.parent
    err = d / daemon.LEGACY_ERR_LOG_NAME
    err.write_text("", encoding="utf-8")  # killed right after the truncate
    (d / f"{daemon.LEGACY_ERR_LOG_NAME}.kept").write_text("Traceback ...\nOSError: [Errno 28]\n", encoding="utf-8")
    notes = daemon.import_legacy_logs(store, d)
    assert err.read_text(encoding="utf-8") == "Traceback ...\nOSError: [Errno 28]\n"
    assert not (d / f"{daemon.LEGACY_ERR_LOG_NAME}.kept").exists()
    assert notes == ["err.log: finished an interrupted strip of verified duplicate event lines"]


def test_a_strip_killed_before_the_truncate_leaves_err_log_untouched(store):
    d = store.path.parent
    err = d / daemon.LEGACY_ERR_LOG_NAME
    err.write_text("original\n", encoding="utf-8")
    (d / f"{daemon.LEGACY_ERR_LOG_NAME}.stripping").write_text("partial", encoding="utf-8")
    assert daemon.import_legacy_logs(store, d) == []
    assert err.read_text(encoding="utf-8") == "original\n"
    assert not (d / f"{daemon.LEGACY_ERR_LOG_NAME}.stripping").exists()


def test_the_template_cache_stays_bounded_past_the_cap(store, monkeypatch):
    monkeypatch.setattr(daemon, "MAX_TEMPLATES", 3)
    for i in range(500):
        store.observe(_obs(f"dynamic line {i}", fmt=f"dynamic format {i}"))
    assert len(store._template_ids) == 4  # 3 real keys + the overflow key, not 500


def test_a_prompt_flood_is_trimmed_between_maintain_runs(store, monkeypatch):
    monkeypatch.setattr(daemon, "MAX_PROMPT_ROWS", 10)
    monkeypatch.setattr(daemon, "PROMPT_TRIM_EVERY", 5)
    for i in range(100):
        store.observe(_obs(f"Policy disallows prompt for Sub:{{x}}; access to kTCCServicePhotos denied #{i}"))
    assert store.db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] <= 10 + 5
    newest = store.db.execute("SELECT message FROM prompts ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert newest.endswith("#99")


def test_sigterm_during_the_legacy_import_commits_nothing(store, monkeypatch):
    """SystemExit (SIGTERM) is not an Exception: the import must still roll back, or run()'s
    `finally: store.close()` would commit half a file and the retry would count it twice."""
    d = store.path.parent
    path = _seed_legacy(d, "events.jsonl", _legacy_rows())
    calls = {"n": 0}
    real = daemon._legacy_template

    def killed_midway(message):
        calls["n"] += 1
        if calls["n"] == 40:
            daemon._terminate(15, None)
        return real(message)

    monkeypatch.setattr(daemon, "_legacy_template", killed_midway)
    with pytest.raises(SystemExit):
        daemon.import_legacy_logs(store, d)
    store.close()
    again = daemon.TccStore(store.path)
    assert again.totals() == (0, 0)
    assert again.db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'legacy_import:%'").fetchone()[0] == 0
    again.db.close()
    assert path.exists()


class _TerminatingDb(_FailingDb):
    def execute(self, sql, *args):
        if self._fail_on in sql:
            daemon._terminate(15, None)
        return self._real.execute(sql, *args)


def test_sigterm_during_maintain_commits_no_half_rollup(store):
    old_day = _T0 - 20 * 86400 - _T0 % 86400
    for _ in range(4):
        store.observe(_obs(_ROUTINE_MESSAGES[0], epoch=old_day + 3600))
    store.flush()
    real = store.db
    store.db = _TerminatingDb(real, "DELETE FROM counts")
    with pytest.raises(SystemExit):
        store.maintain()
    store.db = real
    store.close()  # what run()'s `finally` does
    again = daemon.TccStore(store.path)
    assert again.totals() == (4, 0)
    assert again.db.execute("SELECT span FROM counts").fetchall() == [(3600,)]
    again.db.close()


class _WriteThenTerminateDb(_FailingDb):
    def executemany(self, sql, *args):
        result = self._real.executemany(sql, *args)
        if self._fail_on in sql:
            daemon._terminate(15, None)
        return result


def test_sigterm_right_after_a_count_write_does_not_replay_it(store):
    for _ in range(3):
        store.observe(_obs(_ROUTINE_MESSAGES[0]))
    real = store.db
    store.db = _WriteThenTerminateDb(real, "INSERT INTO counts")
    with pytest.raises(SystemExit):
        store.flush()
    store.db = real
    store.flush()  # run()'s `finally`
    assert store.totals() == (3, 0)


def test_err_log_is_kept_unless_every_legacy_file_was_imported_in_this_run(store, monkeypatch):
    d = store.path.parent
    older, newer = _legacy_rows(2), _legacy_rows(3)
    _seed_legacy(d, "events.jsonl.1", older)
    _seed_legacy(d, "events.jsonl", newer)
    err = d / daemon.LEGACY_ERR_LOG_NAME
    content = "".join(_mirror(r) for r in older + newer)
    err.write_text(content, encoding="utf-8")
    real_totals = daemon.TccStore.totals
    calls = {"n": 0}

    def second_file_off_by_one(self):
        calls["n"] += 1
        lines, prompts = real_totals(self)
        return (lines + 1, prompts) if calls["n"] == 4 else (lines, prompts)

    monkeypatch.setattr(daemon.TccStore, "totals", second_file_off_by_one)
    notes = daemon.import_legacy_logs(store, d)
    assert (d / "events.jsonl").exists() and not (d / "events.jsonl.1").exists()
    assert err.read_text(encoding="utf-8") == content
    assert notes[-1].endswith("left it as is")


def test_report_reads_while_the_daemon_holds_the_write_lock(tmp_path, capsys):
    """`report` during a long legacy import must answer, not wait 30 s and traceback."""
    path = _seeded_store(tmp_path)
    writer = daemon.TccStore(path)
    writer.db.execute("BEGIN IMMEDIATE")
    writer.db.execute("INSERT INTO diagnostics (ts, message) VALUES ('t', 'importing')")
    started = time.monotonic()
    assert daemon.main(["report", "--store", str(path), "--json"]) == 0
    assert time.monotonic() - started < 5
    assert json.loads(capsys.readouterr().out)["lines"] == 6
    writer.rollback()
    writer.db.close()


def test_run_maintains_the_store_on_its_hourly_cadence(tmp_path, monkeypatch):
    calls = []
    real = daemon.TccStore.maintain

    def counting(self):
        calls.append(1)
        real(self)

    monkeypatch.setattr(daemon.TccStore, "maintain", counting)
    ticks = iter(range(0, 10**7, 4000))  # every clock() call is 4000 s later
    daemon.run(
        _store_path(tmp_path),
        stream_factory=lambda: _FakeStreamProc([_tcc_line(_ROUTINE_MESSAGES[0])] * 3),
        sleep=lambda _s: None,
        clock=lambda: float(next(ticks)),
        max_reconnects=1,
    )
    assert len(calls) == 1 + 3  # once at startup, then on every line an hour+ apart


def test_file_names_agree_with_the_provisioning_side():
    """The daemon cannot import riglib, so the shared names are pinned here instead."""
    assert daemon.STORE_NAME == tcc_watch.STORE_NAME
    assert daemon.LEGACY_ERR_LOG_NAME == tcc_watch.ERR_LOG_NAME
    assert daemon.OUT_LOG_NAME == tcc_watch.OUT_LOG_NAME
    assert tcc_watch.WATCHER_SCRIPT_NAME == "watch.py"


def test_an_import_with_more_prompts_than_the_cap_still_verifies(store, monkeypatch):
    """The per-insert trim must not fire inside the import's own transaction (it would make the
    prompt count never match and the file un-importable forever); maintain() trims afterwards."""
    monkeypatch.setattr(daemon, "MAX_PROMPT_ROWS", 10)
    monkeypatch.setattr(daemon, "PROMPT_TRIM_EVERY", 5)
    d = store.path.parent
    rows = [_legacy_row(f"display_prompt: called for <private> for service kTCCServicePhotos #{i}", i)
            for i in range(30)]
    path = _seed_legacy(d, "events.jsonl", rows)
    notes = daemon.import_legacy_logs(store, d)
    assert notes == ["imported events.jsonl: 30 lines (30 prompts/denials); removed it"]
    assert not path.exists()
    store.maintain()
    assert store.db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 10
    assert store.totals()[0] == 30  # every line is still counted


def test_run_reports_a_failing_flush_as_such_and_keeps_going(tmp_path, monkeypatch, capsys):
    def broken_flush(self):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(daemon.TccStore, "flush", broken_flush)
    ticks = iter(range(0, 10**7, 100))
    seen = daemon.run(
        _store_path(tmp_path),
        stream_factory=lambda: _FakeStreamProc([_tcc_line(_ROUTINE_MESSAGES[0])] * 2),
        sleep=lambda _s: None,
        clock=lambda: float(next(ticks)),
        max_reconnects=1,
    )
    assert seen == 2
    err = capsys.readouterr().err
    assert "store flush/maintenance failed: disk I/O error" in err
    assert "failed to record" not in err


@pytest.mark.parametrize("argv", [["--verbose"], ["--store", "x"], ["a.sqlite3", "b.sqlite3"]])
def test_daemon_rejects_flags_and_extra_arguments(tmp_path, monkeypatch, capsys, argv):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(daemon, "run", lambda _p: pytest.fail("must not start a watcher"))
    assert daemon.main(argv) == 2
    assert "usage: watch.py" in capsys.readouterr().err
    assert list(cwd.iterdir()) == []


def test_daemon_help_prints_usage_and_creates_nothing(tmp_path, monkeypatch, capsys):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    assert daemon.main(["--help"]) == 0
    assert "watch.py report" in capsys.readouterr().out
    assert list(cwd.iterdir()) == []


def test_default_store_lives_in_the_provisioned_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    expected = tcc_watch.build_tcc_watch(home=tmp_path, python_bin="/usr/bin/python3").store_path
    assert daemon._default_store() == expected


def test_a_literal_overflow_format_string_cannot_bypass_the_cap(store, monkeypatch):
    monkeypatch.setattr(daemon, "MAX_TEMPLATES", 2)
    for i in range(2):
        store.observe(_obs(f"line {i}", fmt=f"format {i}"))
    for i in range(50):
        store.observe(_obs(f"identifier=com.example.app{i} x", fmt=daemon.OVERFLOW_TEMPLATE))
    assert store.db.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 3
    assert len(store._template_ids) == 3


def test_template_key_fields_are_length_capped():
    obs = daemon.parse_observation(_tcc_line("identifier=" + "a" * 5000, fmt="F" * 5000, category="c" * 5000))
    assert len(obs.template) == daemon.MAX_KEY_CHARS
    assert len(obs.category) == daemon.MAX_KEY_CHARS
    assert len(obs.subject) == daemon.MAX_KEY_CHARS


def test_store_files_are_owner_only(tmp_path):
    path = tmp_path / "s" / daemon.STORE_NAME
    path.parent.mkdir()
    path.write_bytes(b"")
    os.chmod(path, 0o644)  # a store created before the fix
    s = daemon.TccStore(path)
    s.observe(_obs(_ROUTINE_MESSAGES[0]))
    s.flush()
    daemon._owner_only(path)
    for member in path.parent.iterdir():
        assert member.stat().st_mode & 0o777 == 0o600, member.name
    s.db.close()


def test_a_diagnostic_inside_an_open_import_does_not_commit_it(store):
    store.db.execute("BEGIN IMMEDIATE")
    store.observe(_obs(_ROUTINE_MESSAGES[0]), commit=False)
    store.write_pending()
    store.diagnostic("progress note in the middle of an import")
    store.rollback()  # the import fails: nothing of it — the note included — may survive
    assert store.totals() == (0, 0)
    assert store.db.execute("SELECT COUNT(*) FROM diagnostics").fetchone()[0] == 0


def test_report_on_an_unopenable_store_exits_cleanly(tmp_path, monkeypatch, capsys):
    path = _seeded_store(tmp_path)

    def unopenable(*_args, **_kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(daemon.sqlite3, "connect", unopenable)
    assert daemon.main(["report", "--store", str(path)]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_diagnostics_are_stamped_with_the_store_clock(store):
    store.diagnostic("hello")
    assert store.db.execute("SELECT ts FROM diagnostics").fetchone() == ("2026-09-15T15:00:00+00:00",)


def test_report_on_a_corrupt_store_fails_cleanly(tmp_path, capsys):
    path = tmp_path / daemon.STORE_NAME
    path.write_text("this is not a database, it is an events.jsonl renamed by hand\n" * 50, encoding="utf-8")
    assert daemon.main(["report", "--store", str(path)]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_report_clamps_negative_arguments(tmp_path, capsys):
    path = _seeded_store(tmp_path)
    assert daemon.main(["report", "--store", str(path), "--json", "--top", "-5", "--days", "-1"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert len(summary["top_templates"]) == 1
    assert summary["lines"] == 6


def test_a_prompt_and_noise_sharing_a_format_are_counted_apart(store):
    """kind is part of the template key: a generic formatString must not blur the per-kind share."""
    store.observe(_obs("display_prompt: called for <private>", fmt="%{public}s"))
    store.observe(_obs("display_prompt-like, but not a prompt", fmt="%{public}s"))
    summary = store.summary(since=_T0)
    assert {r["kind"]: r["lines"] for r in summary["by_kind"]} == {"prompt_displayed": 1, "noise": 1}


def test_overflow_is_one_row_per_kind_whatever_the_sender_varies(store, monkeypatch):
    """Process name and category are sender-controlled too: they must not mint overflow rows."""
    monkeypatch.setattr(daemon, "MAX_TEMPLATES", 2)
    for i in range(2):
        store.observe(_obs(f"line {i}", fmt=f"format {i}"))
    for i in range(60):
        obs = _obs(f"line {i}", fmt=f"more {i}", process=f"/tmp/proc{i}")
        store.observe(dataclasses.replace(obs, category=f"cat{i}"))
    store.observe(_obs(_REAL_PROMPT_MESSAGES[0][2], fmt="prompt fmt"))
    rows = store.db.execute("SELECT process, category, format, kind FROM templates ORDER BY id").fetchall()
    assert rows[2:] == [("*", "", daemon.OVERFLOW_TEMPLATE, "noise"), ("*", "", daemon.OVERFLOW_TEMPLATE, "prompting")]
    assert store.db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 1  # the prompt is still kept in full


def test_open_store_moves_a_non_database_aside_and_starts_fresh(tmp_path, capsys):
    path = _store_path(tmp_path)
    path.write_text("an events.jsonl renamed by hand\n" * 100, encoding="utf-8")
    # WAL files left by the store that used to be here: SQLite would pair them with a new file
    (path.parent / f"{path.name}-wal").write_bytes(b"\x37\x7f\x06\x82" + b"\0" * 60)
    (path.parent / f"{path.name}-shm").write_bytes(b"\0" * 64)
    sleeps: list[float] = []
    s = daemon.open_store(path, sleep=sleeps.append)
    assert s.totals() == (0, 0)
    s.db.close()
    assert sleeps == [1.0]  # backed off once, never spun
    # the old WAL files are either moved aside with it or were already dropped by SQLite; either
    # way they are not paired with the new store (which just opened cleanly)
    aside = [p.name for p in path.parent.iterdir() if ".unreadable-" in p.name]
    assert set(n.split(".unreadable-")[0] for n in aside) <= {path.name, f"{path.name}-wal", f"{path.name}-shm"}
    kept = next(p for p in path.parent.iterdir() if p.name.startswith(f"{path.name}.unreadable-"))
    assert kept.read_text(encoding="utf-8").startswith("an events.jsonl")
    assert "starting a new store" in capsys.readouterr().err


def test_open_store_retries_a_locked_or_failing_store_with_backoff(tmp_path, monkeypatch):
    real = daemon.TccStore
    attempts = {"n": 0}

    def flaky(path):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return real(path)

    monkeypatch.setattr(daemon, "TccStore", flaky)
    sleeps: list[float] = []
    s = daemon.open_store(_store_path(tmp_path), sleep=sleeps.append)
    assert sleeps == [1.0, 2.0]
    s.db.close()


def test_the_store_directory_and_launchd_logs_are_owner_only(tmp_path):
    d = tmp_path / "tcc-watch"
    d.mkdir(mode=0o755)
    err = d / daemon.LEGACY_ERR_LOG_NAME
    err.write_text("x\n", encoding="utf-8")
    os.chmod(err, 0o644)
    s = daemon.TccStore(d / daemon.STORE_NAME)
    assert d.stat().st_mode & 0o777 == 0o700
    assert err.stat().st_mode & 0o777 == 0o600
    s.db.close()


# ── main + report ──────────────────────────────────────────────────────────────────────────
def test_main_maps_an_old_events_jsonl_argument_to_the_store(tmp_path, monkeypatch):
    """A plist written before #607 still passes <config_dir>/events.jsonl."""
    got = []
    monkeypatch.setattr(daemon, "run", lambda path: got.append(path))
    handled = []
    monkeypatch.setattr(daemon.signal, "signal", lambda signum, _handler: handled.append(signum))
    umasks = []
    monkeypatch.setattr(daemon.os, "umask", umasks.append)  # never the test process's own umask
    daemon.main([str(tmp_path / "events.jsonl")])
    daemon.main([str(tmp_path / "custom.sqlite3")])
    assert got == [tmp_path / daemon.STORE_NAME, tmp_path / "custom.sqlite3"]
    assert umasks == [0o077, 0o077]
    assert handled == [daemon.signal.SIGTERM, daemon.signal.SIGINT] * 2  # Ctrl-C exits as cleanly as bootout


def _seeded_store(tmp_path) -> Path:
    path = _store_path(tmp_path)
    now = int(time.time())
    s = daemon.TccStore(path)
    for _ in range(5):
        s.observe(_obs(_ROUTINE_MESSAGES[0], epoch=now))
    s.observe(_obs(_REAL_PROMPT_MESSAGES[0][2], epoch=now))
    s.close()
    return path


def test_report_reads_the_store_beside_the_deployed_script(tmp_path, monkeypatch):
    """With a custom config_dir the deployed watch.py and its store sit together there."""
    custom = tmp_path / "custom-config-dir"
    custom.mkdir()
    (custom / daemon.STORE_NAME).write_bytes(b"")
    monkeypatch.setattr(daemon, "__file__", str(custom / "watch.py"))
    assert daemon._report_store() == custom / daemon.STORE_NAME
    (custom / daemon.STORE_NAME).unlink()
    assert daemon._report_store() == daemon._default_store()


def test_trim_refuses_any_other_table(store):
    with pytest.raises(ValueError):
        store._trim("templates; DROP TABLE counts", 1)


def test_a_crash_is_reported_through_the_size_gated_writer(tmp_path, monkeypatch):
    def boom(_path):
        raise TypeError("a bug nobody foresaw")

    monkeypatch.setattr(daemon, "run", boom)
    monkeypatch.setattr(daemon.signal, "signal", lambda *_a: None)
    monkeypatch.setattr(daemon.os, "umask", lambda _m: 0)
    err = tmp_path / "err.log"
    with err.open("a", encoding="utf-8") as fh:
        monkeypatch.setattr(sys, "stderr", fh)
        assert daemon.main([str(tmp_path / "s.sqlite3")]) == 1
    assert "crashed: Traceback" in err.read_text(encoding="utf-8")
    assert "TypeError: a bug nobody foresaw" in err.read_text(encoding="utf-8")
    # once err.log is past the cap, a crash-looping bug adds nothing more
    monkeypatch.setattr(daemon, "MAX_STDERR_BYTES", 10)
    size = err.stat().st_size
    with err.open("a", encoding="utf-8") as fh:
        monkeypatch.setattr(sys, "stderr", fh)
        assert daemon.main([str(tmp_path / "s.sqlite3")]) == 1
    assert err.stat().st_size == size


def test_a_store_with_an_unknown_layout_is_moved_aside_not_misread(tmp_path):
    path = _store_path(tmp_path)
    s = daemon.TccStore(path)
    s.db.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    s.db.commit()
    s.db.close()
    fresh = daemon.open_store(path, sleep=lambda _s: None)
    assert fresh.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() == ("1",)
    fresh.db.close()
    assert any(p.name.startswith(f"{daemon.STORE_NAME}.unreadable-") for p in path.parent.iterdir())


def test_run_flushes_counts_on_its_minute_cadence(tmp_path):
    path = _store_path(tmp_path)
    ticks = iter(range(0, 10**7, 61))  # every clock() call is 61 s later
    seen_at_spawn = []

    def stream():
        yield _tcc_line(_ROUTINE_MESSAGES[0])
        yield _tcc_line(_ROUTINE_MESSAGES[0])  # this line's timer check flushes both
        reader = daemon.TccStore(path, readonly=True)
        seen_at_spawn.append(reader.totals())
        reader.close()

    daemon.run(path, stream_factory=lambda: _FakeStreamProc(stream()), sleep=lambda _s: None,
               clock=lambda: float(next(ticks)), max_reconnects=1)
    assert seen_at_spawn == [(2, 0)]  # visible to another connection before the stream ended


def test_a_line_without_a_timestamp_is_counted_now(store):
    obs = daemon.parse_observation(_tcc_line(_ROUTINE_MESSAGES[0], ts="garbage"))
    store.observe(obs)
    store.flush()
    assert _counts(store)[0][0] == _T0  # the store clock's hour


def test_report_prints_what_fills_the_log(tmp_path, capsys):
    path = _seeded_store(tmp_path)
    assert daemon.main(["report", "--store", str(path), "--days", "2"]) == 0
    out = capsys.readouterr().out
    assert "TCC log lines since" in out and ": 6" in out
    assert "noise" in out and "prompting" in out
    assert "com.example.chat" in out
    assert "AUTHREQ_PROMPTING" in out


def test_report_json(tmp_path, capsys):
    path = _seeded_store(tmp_path)
    assert daemon.main(["report", "--store", str(path), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["lines"] == 6
    assert summary["recent_prompts"][0]["kind"] == "prompting"


def test_report_without_a_store_fails_cleanly(tmp_path, capsys):
    assert daemon.main(["report", "--store", str(tmp_path / "missing.sqlite3")]) == 1
    assert "no store" in capsys.readouterr().err
    assert not (tmp_path / "missing.sqlite3").exists()


# ── install (runner) — write + (re)load, idempotent, conflict ─────────────────────────────
def _action(home, **over):
    from riglib.plan import Action

    return Action(
        kind="provision_tcc_watch",
        category="tcc_watch",
        item="watch",
        source=home,
        target=Path(tcc_watch.DEFAULT_LABEL),
        options={"label": None, "config_dir": None, "python_bin": str(home / "bin" / "python3"), **over},
    )


def _stub_launchctl(monkeypatch, *, loaded=False, spy=None):
    calls = spy if spy is not None else []

    def _bootout(plist_path):
        calls.append(("bootout", plist_path))
        return 0

    def _bootstrap(plist_path):
        calls.append(("bootstrap", plist_path))
        return 0

    monkeypatch.setattr(runner, "_launchctl_bootout", _bootout)
    monkeypatch.setattr(runner, "_launchctl_bootstrap", _bootstrap)
    monkeypatch.setattr(runner, "_launchctl_gui_loaded", lambda label: loaded)
    return calls


def test_apply_writes_artifacts_and_bootstraps(monkeypatch, tmp_path):
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    spy = _stub_launchctl(monkeypatch, loaded=False)
    res = runner._do_provision_tcc_watch(_action(home), "backup")
    assert res.status in ("created", "backed_up"), res.detail
    plist = home / "Library" / "LaunchAgents" / f"{tcc_watch.DEFAULT_LABEL}.plist"
    script = home / ".config" / "rig" / "tcc-watch" / "watch.py"
    assert plist.is_file()
    assert script.is_file()
    assert script.read_text(encoding="utf-8") == Path(daemon.__file__).read_text(encoding="utf-8")
    assert ("bootstrap", str(plist)) in spy


def test_apply_is_idempotent_when_loaded(monkeypatch, tmp_path):
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    _stub_launchctl(monkeypatch, loaded=False)
    runner._do_provision_tcc_watch(_action(home), "backup")
    _stub_launchctl(monkeypatch, loaded=True)
    res2 = runner._do_provision_tcc_watch(_action(home), "backup")
    assert res2.status == "skipped", res2.detail


def test_reapply_against_identical_artifacts_does_not_rewrite(monkeypatch, tmp_path):
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    _stub_launchctl(monkeypatch, loaded=False)
    runner._do_provision_tcc_watch(_action(home), "backup")
    script = home / ".config" / "rig" / "tcc-watch" / "watch.py"
    before = script.read_text(encoding="utf-8")
    _stub_launchctl(monkeypatch, loaded=True)
    runner._do_provision_tcc_watch(_action(home), "backup")
    assert script.read_text(encoding="utf-8") == before


def test_apply_backs_up_a_differing_plist(monkeypatch, tmp_path):
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    plist = home / "Library" / "LaunchAgents" / f"{tcc_watch.DEFAULT_LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("OLD HAND-EDITED", encoding="utf-8")
    _stub_launchctl(monkeypatch, loaded=False)
    res = runner._do_provision_tcc_watch(_action(home), "backup")
    assert res.status == "backed_up", res.detail
    assert res.backup is not None and res.backup.is_file()
    assert "ai.hyperide.tcc-watch" in plist.read_text() and "OLD HAND-EDITED" not in plist.read_text()


def test_apply_off_darwin_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    home = _isolate_home(monkeypatch, tmp_path)
    spy = _stub_launchctl(monkeypatch, loaded=False)
    res = runner._do_provision_tcc_watch(_action(home), "backup")
    assert res.status == "skipped"
    assert spy == []
    assert not (home / "Library").exists()


def test_dry_run_writes_artifacts_but_skips_launchctl(monkeypatch, tmp_path):
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setenv("RIG_TCC_WATCH_DRY_RUN", "1")
    spy = _stub_launchctl(monkeypatch, loaded=False)
    res = runner._do_provision_tcc_watch(_action(home), "backup")
    assert (home / "Library" / "LaunchAgents" / f"{tcc_watch.DEFAULT_LABEL}.plist").is_file()
    assert spy == []
    assert "RIG_TCC_WATCH_DRY_RUN" in res.detail


# ── full apply path through run_plan ─────────────────────────────────────────────────────
def test_run_plan_provisions_tcc_watch(monkeypatch, tmp_path):
    from riglib.actions.runner import run_plan
    from riglib.plan import InstallPlan

    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    _stub_launchctl(monkeypatch, loaded=False)
    report = run_plan(InstallPlan(actions=[_action(home)]))
    assert not report.errors
    assert report.changed == 1


# ── plan building ────────────────────────────────────────────────────────────────────────
def _cfg(data, repo_root):
    from riglib.config import LoadedConfig

    return LoadedConfig(data=data, repo_root=repo_root)


def _build(data, repo_root, fake_agent_tools):
    from riglib.catalog import Catalog
    from riglib.plan import build

    cat = Catalog(fake_agent_tools)
    return build(_cfg(data, repo_root), cat, project_type="unknown")


def test_plan_provisions_tcc_watch_by_default(fake_agent_tools, tmp_path):
    """tcc_watch is default-on: an ABSENT block still emits a provision_tcc_watch action."""
    plan = _build({}, tmp_path, fake_agent_tools)
    assert [a for a in plan.actions if a.kind == "provision_tcc_watch"]


def test_plan_no_tcc_watch_when_disabled(fake_agent_tools, tmp_path):
    plan = _build({"tcc_watch": {"enabled": False}}, tmp_path, fake_agent_tools)
    assert not [a for a in plan.actions if a.kind == "provision_tcc_watch"]


def test_plan_emits_single_tcc_watch_action(fake_agent_tools, tmp_path):
    plan = _build({"tcc_watch": {"enabled": True}}, tmp_path, fake_agent_tools)
    acts = [a for a in plan.actions if a.kind == "provision_tcc_watch"]
    assert len(acts) == 1
    assert acts[0].category == "tcc_watch" and acts[0].item == "watch"


def test_plan_carries_knobs(fake_agent_tools, tmp_path):
    plan = _build(
        {"tcc_watch": {"config_dir": "/var/tcc-watch", "label": "com.me.tcc-watch"}},
        tmp_path, fake_agent_tools,
    )
    a = [a for a in plan.actions if a.kind == "provision_tcc_watch"][0]
    assert a.options["config_dir"] == "/var/tcc-watch"
    assert a.options["label"] == "com.me.tcc-watch"


def test_plan_label_null_resolves_to_default(fake_agent_tools, tmp_path):
    plan = _build({"tcc_watch": {"label": None}}, tmp_path, fake_agent_tools)
    a = [a for a in plan.actions if a.kind == "provision_tcc_watch"][0]
    tw = runner.tcc_watch_plan_from_action(a)
    assert tw.label == tcc_watch.DEFAULT_LABEL
