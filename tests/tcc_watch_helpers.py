"""Shared builders, fakes and the real-handler restore for the tcc-watch test modules."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import sys
import time
from pathlib import Path

from riglib import tcc_watch
from riglib import tcc_watch_daemon as daemon
from riglib.actions import runner
from riglib.actions import service_ctl as service_ctl_mod

REAL_PROVISION = runner._do_provision_tcc_watch



def _force_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")


def _isolate_home(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home



def _plan(home="/Users/ultra", **over):
    over.setdefault("python_bin", str(Path(home) / "bin" / "python3"))
    return tcc_watch.build_tcc_watch(home=Path(home), **over)



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


_T0 = 1789484400  # an hour boundary: 2026-09-15 15:00:00 UTC


def _obs(message: str, *, epoch: int = _T0, fmt: str = "%{public}s", process: str = _TCCD):
    obs = daemon.parse_observation(_tcc_line(message, fmt=fmt, process=process))
    return dataclasses.replace(obs, epoch=epoch)


def open_store(tmp_path):
    """Generator body of the per-module ``store`` fixture: a TccStore on a fixed clock, closed after."""
    s = daemon.TccStore(tmp_path / "tcc-watch" / daemon.STORE_NAME, now=lambda: _T0)
    yield s
    s.db.close()


def _counts(s) -> list[tuple]:
    return s.db.execute(
        "SELECT c.bucket, c.span, t.format, t.subject, c.n FROM counts c JOIN templates t ON t.id = c.template_id"
        " ORDER BY c.bucket, t.format, t.subject"
    ).fetchall()



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



class _TerminatingDb(_FailingDb):
    def execute(self, sql, *args):
        if self._fail_on in sql:
            daemon._terminate(15, None)
        return self._real.execute(sql, *args)



class _WriteThenTerminateDb(_FailingDb):
    def executemany(self, sql, *args):
        result = self._real.executemany(sql, *args)
        if self._fail_on in sql:
            daemon._terminate(15, None)
        return result



def _seeded_store(tmp_path) -> Path:
    path = _store_path(tmp_path)
    now = int(time.time())
    s = daemon.TccStore(path)
    for _ in range(5):
        s.observe(_obs(_ROUTINE_MESSAGES[0], epoch=now))
    s.observe(_obs(_REAL_PROMPT_MESSAGES[0][2], epoch=now))
    s.close()
    return path



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

    monkeypatch.setattr(service_ctl_mod, "_launchctl_bootout", _bootout)
    monkeypatch.setattr(service_ctl_mod, "_launchctl_bootstrap", _bootstrap)
    monkeypatch.setattr(service_ctl_mod, "_launchctl_gui_loaded", lambda label: loaded)
    return calls



def _cfg(data, repo_root):
    from riglib.config import LoadedConfig

    return LoadedConfig(data=data, repo_root=repo_root)


def _build(data, repo_root, fake_agent_tools):
    from riglib.catalog import Catalog
    from riglib.plan import build

    cat = Catalog(fake_agent_tools)
    return build(_cfg(data, repo_root), cat, project_type="unknown")



def restore_real_tcc_watch(monkeypatch):
    """Restore the REAL provision_tcc_watch handler (conftest stubs it) and clear the dry-run env."""
    monkeypatch.setattr(runner, "_do_provision_tcc_watch", REAL_PROVISION)
    monkeypatch.setitem(runner._HANDLERS, "provision_tcc_watch", REAL_PROVISION)
    monkeypatch.delenv("RIG_TCC_WATCH_DRY_RUN", raising=False)
