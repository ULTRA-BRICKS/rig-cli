"""``rig forge login|status`` — log in to the Forgejo host with one browser click.

``login`` resolves the host (``--host`` → the repo/global ``forge.host`` → the https ``origin``
remote), makes sure the machine is provisioned exactly as ``rig apply`` would (fj installed,
``client_ids``, ``FJ_FALLBACK_HOST``, GCM settings — :func:`riglib.forge.apply_changes`), then runs
``fj auth login`` (browser → *Authorize*) and, when git uses Git Credential Manager, asks GCM for
a credential once so git gets its own browser login too. Headless machines use ``--token-stdin``
(a Forgejo access token on stdin; never on argv).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import forge


def add_parser(sub: "argparse._SubParsersAction") -> None:
    fp = sub.add_parser("forge", help="Forgejo: one-click fj + git login, fj install, default host")
    fp.set_defaults(_forge_parser=fp)
    fsub = fp.add_subparsers(dest="forge_command", metavar="<login|status>")

    lp = fsub.add_parser("login", help="log fj and git in to the Forgejo host (opens the browser)")
    lp.add_argument("--host", help="Forgejo host (default: forge.host from rig.yaml / global config, else origin)")
    lp.add_argument("-C", "--cwd", default=".", help="repo whose config/remote picks the host (default: cwd)")
    lp.add_argument("--token-stdin", action="store_true",
                    help="headless: read a Forgejo access token from stdin instead of the browser")
    lp.add_argument("--no-git", action="store_true", help="log in fj only; leave git credentials alone")
    lp.add_argument("--force", action="store_true", help="log in again even when fj already is")

    stp = fsub.add_parser("status", help="show fj, login and default-host state for the host")
    stp.add_argument("--host", help="Forgejo host (default: as for login)")
    stp.add_argument("-C", "--cwd", default=".", help="repo whose config/remote picks the host (default: cwd)")


def run(args: argparse.Namespace) -> int:
    if args.forge_command == "login":
        return _login(args)
    if args.forge_command == "status":
        return _status(args)
    args._forge_parser.print_help()
    return 0


# ── host + options ──────────────────────────────────────────────────────────────────────────


def _origin_host(repo: Path) -> str | None:
    """Host of an https ``origin`` remote (an ssh remote's host is the ssh endpoint, not the web
    host fj/OAuth talk to, so it is not guessed)."""
    try:
        url = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not url.startswith(("https://", "http://")):
        return None
    try:
        return forge.normalize_host(url)
    except ValueError:
        return None


def _resolve(args: argparse.Namespace) -> tuple[str, dict]:
    from .config import load
    from .plan import _global_forge_host

    repo = Path(args.cwd).resolve()
    loaded = load(repo)
    block = loaded.data.get("forge") if isinstance(loaded.data.get("forge"), dict) else {}
    host = args.host or block.get("host") or _origin_host(repo)
    if not host:
        raise SystemExit(
            "rig forge: no Forgejo host — pass --host, or set one for this machine:\n"
            "  rig config set --global forge.host git.hyperide.ai --commit"
        )
    host = forge.normalize_host(str(host))
    options = forge.forge_options_from_config(
        {**block, "host": host}, fallback_host=_global_forge_host(loaded) or host
    )
    return host, options


def _fj(options: dict) -> str:
    fj = forge.find_fj(options["bin_dir"])
    if not fj:
        raise SystemExit("rig forge: fj is still not on PATH — see the install error above")
    return fj


def _whoami(fj: str, host: str) -> str | None:
    try:
        res = subprocess.run([fj, "-H", host, "whoami"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = res.stdout.strip()
    return out if res.returncode == 0 and out else None


# ── commands ────────────────────────────────────────────────────────────────────────────────


def _login(args: argparse.Namespace) -> int:
    host, options = _resolve(args)
    done, errors = forge.apply_changes(options)
    for line in done:
        print(f"rig forge: {line}")
    for line in errors:
        print(f"rig forge: ERROR {line}", file=sys.stderr)
    fj = _fj(options)

    who = None if args.force else _whoami(fj, host)
    if who:
        print(f"rig forge: fj already logged in to {host} — {who}")
    elif args.token_stdin:
        token = sys.stdin.readline().strip()
        if not token:
            print("rig forge: --token-stdin got an empty line", file=sys.stderr)
            return 2
        if args.force:
            subprocess.run([fj, "auth", "logout", host], capture_output=True, timeout=30)
        res = subprocess.run([fj, "-H", host, "auth", "add-token"], input=token + "\n",
                             capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            print(f"rig forge: fj auth add-token failed: {res.stderr.strip()}", file=sys.stderr)
            return 1
    else:
        if args.force:
            subprocess.run([fj, "auth", "logout", host], capture_output=True, timeout=30)
        print(f"rig forge: opening the browser — press Authorize on {host}")
        if subprocess.run([fj, "-H", host, "auth", "login"]).returncode != 0:
            print("rig forge: fj auth login failed", file=sys.stderr)
            return 1

    who = _whoami(fj, host)
    if not who:
        print(f"rig forge: fj is not logged in to {host} (fj whoami failed)", file=sys.stderr)
        return 1
    print(f"rig forge: fj → {host}: {who}")

    if not args.no_git and options["git_credentials"] and not args.token_stdin:
        if forge.git_uses_gcm():
            print(f"rig forge: git — Git Credential Manager may open the browser once for https://{host}")
            query = f"protocol=https\nhost={host}\n\n"
            fill = subprocess.run(["git", "credential", "fill"], input=query, capture_output=True,
                                  text=True, timeout=600)
            if fill.returncode == 0 and "password=" in fill.stdout:
                subprocess.run(["git", "credential", "approve"], input=fill.stdout, capture_output=True,
                               text=True, timeout=60)
                print(f"rig forge: git → https://{host}: credentials stored by GCM")
            else:
                print(f"rig forge: git login did not complete: {fill.stderr.strip()}", file=sys.stderr)
                return 1
        else:
            print("rig forge: git — the credential helper is not Git Credential Manager; "
                  "git asks for a login on the first push (or use ssh)")
    return 1 if errors else 0


def _status(args: argparse.Namespace) -> int:
    host, options = _resolve(args)
    todo = forge.pending_changes(options)
    fj = forge.find_fj(options["bin_dir"])
    print(f"host:         {host}" + (f"  (FJ_FALLBACK_HOST → {options['fallback_host']})"))
    print(f"fj:           {fj or 'not installed'}")
    print(f"fj login:     {(_whoami(fj, host) or 'not logged in') if fj else '—'}")
    print(f"git via GCM:  {'yes' if forge.git_uses_gcm() else 'no (credential helper is not GCM)'}")
    for direction, item, target, detail in todo:
        print(f"  {direction:8} {item}: {detail}")
    if todo:
        print("run `rig forge login` (or `rig apply commit`) to fix")
    return 0
