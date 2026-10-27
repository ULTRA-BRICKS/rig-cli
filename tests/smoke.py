#!/usr/bin/env python3
"""Real CLI/catalog smoke checks, with isolated machine state.

--fast is the PRE-COMMIT subset: CLI, clean/removed/non-git status and complete
real-catalog planning. Heavy apply, macOS tg-ctl provisioning and pytest run only
without --fast. Set RIG_AGENT_TOOLS_SOURCE to select a real catalog checkout.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
RIG = [sys.executable, str(ROOT / "bin" / "rig")]


class SmokeFailure(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def passed(message: str) -> None:
    print(f"  ✔ {message}", flush=True)


def skipped(message: str) -> None:
    print(f"  ○ skip {message}", flush=True)


def run(args, env, *, cwd=None, check=True):
    result = subprocess.run(args, cwd=cwd or ROOT, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if check and result.returncode:
        raise SmokeFailure(f"{' '.join(map(str, args))}: exit {result.returncode}\n{result.stdout[-5000:]}")
    return result


def rig(env, *args, check=True):
    return run([*RIG, *map(str, args)], env, check=check)


def isolated(parent: Path, inherited: dict[str, str]) -> dict[str, str]:
    home = parent / "home"
    home.mkdir(parents=True, exist_ok=True)
    xdg = home / ".config"
    return dict(inherited, HOME=str(home), XDG_CONFIG_HOME=str(xdg),
                GH_CONFIG_DIR=str(xdg / "gh"), GIT_CONFIG_GLOBAL=str(home / ".gitconfig"),
                GIT_CONFIG_SYSTEM=os.devnull, RIG_TMUX_DRY_RUN="1")


def write_config(path: Path, source: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"version: 1\nagent_tools_source: {json.dumps(str(source))}\n{body}")


def init_repo(path: Path, env) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-q", str(path)], env)
    run(["git", "-C", str(path), "config", "user.email", "smoke@rig.test"], env)
    run(["git", "-C", str(path), "config", "user.name", "rig-smoke"], env)


def source_checkout(env) -> Path | None:
    home = Path(env.get("HOME", str(Path.home())))
    candidates = [env.get("RIG_AGENT_TOOLS_SOURCE", ""),
                  *(str(home / name) for name in ("xp/agent-tools", "work/agent-tools", "agent-tools"))]
    for candidate in candidates:
        path = Path(candidate) if candidate else None
        if path and (path / "skills").is_dir() and (path / "agent-hooks").is_dir():
            return path.resolve()
    return None


def disabled_config() -> str:
    return """skills: { enabled: false }
agent_hooks: { enabled: false }
ci: { enabled: false }
git_hooks: { dispatcher: { enabled: false } }
mcp: { enabled: false }
agents_md: { enabled: false }
ship_delegator: { enabled: false }
gitignore: { enabled: false }
tg_ctl: { enabled: false }
tgctl_reaper: { enabled: false }
sysmon: { enabled: false }
permissions: { enabled: false }
lint_checks: { enabled: false }
"""


def global_fixture(env) -> None:
    # A repository opt-out cannot disable machine defaults for other repositories.
    # These are isolated test settings, never the operator's real global file.
    path = Path(env["XDG_CONFIG_HOME"]) / "rig" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("version: 1\nlint_checks: { enabled: false }\nmodels: { enabled: false }\n")


def surface_checks(env) -> None:
    rig(env, "--help")
    rig(env, "--version")
    passed("rig --help / --version")
    rig(env, "doctor", check=False)
    passed("rig doctor")
    out = rig(env, "setup").stdout
    require("rig config get" in out and "non-interactive" in out,
            "rig setup non-interactive: missing usage pointer")
    passed("rig setup (no TTY) → usage for init/apply/config")


def status_checks(tmp: Path, source: Path, env, stack: ExitStack) -> None:
    dead = tmp / "dead.yaml"
    write_config(dead, source, """skills: { enabled: false }
agent_hooks: { enabled: false }
ci: { enabled: false }
git_hooks: { dispatcher: { enabled: false } }
mcp:
  items:
    review: { enabled: true, command: "review --mcp" }
""")
    result = rig(env, "status", "-C", tmp, "--config", dead, check=False)
    out = result.stdout
    require(result.returncode == 4 and "removed mcp slot" in out.lower() and "#32" in out
            and re.search(r"remove .*mcp.items.review", out, re.I) and str(dead) in out,
            f"removed-slot: missing WHAT/WHY/FIX, file or exit 4\n{out}")
    passed("rig status: removed slot → 3-part error + exit 4")
    clean_env = isolated(tmp / "clean-machine", env)
    global_fixture(clean_env)
    clean_repo = tmp / "clean-repo"
    init_repo(clean_repo, clean_env)
    write_config(clean_repo / "rig.yaml", source, disabled_config())
    rig(clean_env, "status", "-C", clean_repo)
    passed("rig status: clean sample → exit 0")
    nongit = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="rig-smoke-nongit-")))
    out = rig(clean_env, "status", "-C", nongit, check=False).stdout
    require("should be committed" not in out.lower() and "not a git repository" in out.lower(),
            f"non-git status contract failed\n{out}")
    passed("rig status: non-git dir → no 'should be committed', repo layer N/A")


def catalog_coverage(tmp: Path, source: Path, env) -> None:
    cov_env = isolated(tmp / "cov-machine", env)
    repo = tmp / "cov-repo"
    init_repo(repo, cov_env)
    kinds = sorted(p.name for p in (source / "skills" / "by-type").glob("*") if p.is_dir())
    config = repo / "full-coverage.yaml"
    write_config(config, source, f"""skills:
  universal: {{ all: true }}
  by_type: {{ enable: {json.dumps(kinds)} }}
agent_hooks: {{ all: true }}
ci: {{ all: true }}
mcp: {{ enabled: true }}
git_hooks:
  dispatcher: {{ enabled: true }}
github: {{ ruleset: {{ enabled: false }}, merge: {{ enabled: false }}, ghas: {{ enabled: false }}, actions: {{ enabled: false }}, browser: {{ enabled: false }} }}
tmux: {{ enabled: false }}
tg_ctl: {{ enabled: false }}
models: {{ enabled: false }}
gitignore: {{ enabled: false }}
agents_md: {{ enabled: false }}
ship_delegator: {{ enabled: false }}
permissions: {{ enabled: false }}
""")
    out = rig(cov_env, "apply", "-C", repo, "--config", config, "--dry-run", "--plan").stdout
    require(not re.search(r"unknown .*item|unknown .*slot|unknown ci", out, re.I),
            f"real-catalog coverage: unknown item/slot\n{out}")
    require("nothing applied" in out, "real-catalog coverage: dry-run claims it wrote something")
    (repo / "full-coverage.plan").write_text(out)
    slots = sorted(p.name for p in (source / "ci").glob("*") if p.is_dir())
    require(bool(slots), "real-catalog coverage: NO ci slots found; coverage would be vacuous")
    for slot in slots:
        require(f"• ci/{slot} " in out, f"real-catalog coverage: ci slot '{slot}' missing (scanner dropped it?)")
    passed(f"rig dry-run-plans the FULL real catalog (zero unknown items; {len(slots)} ci slots covered)")


def unchanged_summary(output: str, label: str) -> str:
    summary = "\n".join(line for line in output.splitlines() if line.startswith("Summary:"))
    require(not re.search(r"(created|updated|backed_up)=[1-9]", summary),
            f"{label} was not idempotent: {summary}")
    return summary


def permissions_check(home: Path) -> None:
    sys.path.insert(0, str(ROOT))
    from riglib.permissions import DEFAULT_TOOLS
    path = home / ".claude" / "settings.json"
    require(path.is_file(), "permissions: harness settings.json not written")
    permissions = json.loads(path.read_text()).get("permissions", {})
    missing = {f"Bash({tool}:*)" for tool in DEFAULT_TOOLS} - set(permissions.get("allow", []))
    require(not missing, f"permissions: missing allow tools {sorted(missing)}")
    require("Bash(sudo rm:*)" in permissions.get("deny", []), "permissions: deny baseline missing")
    require("Bash(pkill:*)" in permissions.get("ask", []), "permissions: ask baseline missing")
    passed("rig init --yes pre-allowed our CLIs + dev tools and asserted the deny/ask baselines")


def installed_artifacts(tmp: Path, env) -> Path:
    home = Path(env["HOME"])
    require((home / ".agents/skills").is_dir(), "skills not installed")
    require((tmp / ".github/workflows/secret-scan.yml").is_file(), "secret-scan workflow not written")
    require(os.access(home / ".config/git/run-global-hooks", os.X_OK), "dispatcher runner not installed")
    skills = [p for p in (home / ".agents/skills").iterdir() if p.is_dir()]
    require(bool(skills), "no installed skills")
    linked = home / ".claude/skills" / skills[0].name
    require(linked.is_symlink() and (linked / "SKILL.md").is_file(), "harness skill link does not resolve")
    passed("rig init --yes --apply installed skills + CI + dispatcher + harness skill links")
    permissions_check(home)
    tmux = home / ".config/rig/tmux"
    boot = (tmux / "tmux-boot.sh").read_text()
    config = (tmux / "rig.tmux.conf").read_text()
    require(any("new-session -d" in line and not line.lstrip().startswith("#") for line in boot.splitlines()),
            "tmux: boot script must use new-session -d")
    require(re.search(r"^\s*set -g default-command", config, re.M) is not None, "tmux: login-shell directive missing")
    passed("rig init --yes generated tmux v2 config + boot script (login-shell, new-session -d)")
    val = run(["git", "config", "--global", "core.excludesfile"], env).stdout.strip()
    require(val == "~/.config/git/ignore", f"core.excludesfile unexpected: {val}")
    excludes = home / ".config/git/ignore"
    text = excludes.read_text()
    require("rig-managed" in text and ".claude/worktrees/" in text and ".metadata_never_index" in text.splitlines(),
            "global excludes required entries missing")
    passed("rig init --yes set core.excludesfile + wrote the rig-managed global-excludes block")
    return excludes


def alias_check(source: Path, env) -> None:
    if not shutil.which("gh", path=env.get("PATH")) or not (source / "ci/ship/ship.sh").is_file():
        skipped("gh ship alias check — no gh on PATH or no ci/ship/ship.sh in the checkout")
        return
    code = """import sys
sys.path.insert(0, sys.argv[1])
from riglib.gh_ship_alias import gh_config_path, resolve_gh_ship_alias
assert str(gh_config_path()).startswith(sys.argv[2]), 'gh config escaped isolation'
assert resolve_gh_ship_alias().state == 'ok', 'gh ship alias not in sync'
"""
    run([sys.executable, "-c", code, str(ROOT), env["GH_CONFIG_DIR"]], env)
    passed("rig apply set the gh ship alias in the isolated gh config")


def heavy_apply(tmp: Path, source: Path, env) -> None:
    config = tmp / "rig.yaml"
    # This fixture covers pre-existing provisioning areas; dedicated lint acceptance tests cover
    # the default-on analyzer. No network download/CI release pin is needed for this smoke leg.
    global_fixture(env)
    write_config(config, source, """skills:
  universal: { all: true }
  by_type: { enable: [cli] }
agent_hooks: { all: true }
git_hooks:
  dispatcher: { enabled: true }
ci:
  items:
    secret-scan: { enabled: true, tier: block }
    ship: { enabled: true, install_to: ~/bin, gh_alias: false }
tmux:
  enabled: true
  apply: import
  resurrect: { processes: [ssh, psql], capture_pane_contents: true }
  continuum: { restore: true, save_interval: 15, boot: true }
  cc_restore: { enabled: true }
  anti_sprawl: { enabled: true, session: main }
  boot: { enabled: true }
  login_shell: { enabled: true }
gitignore: { enabled: true }
lint_checks: { enabled: false }
""")
    if (source / "mcp/review").is_dir():
        with config.open("a") as stream:
            stream.write('mcp:\n  items:\n    review: { enabled: true, command: "review --mcp" }\n')
    rig(env, "init", "-C", tmp, "--config", config, "--yes", "--dry-run")
    home = Path(env["HOME"])
    require(not (home / ".agents/skills").exists() and not (home / ".claude/skills").exists(),
            "dry-run wrote skills or harness links")
    passed("rig init --dry-run wrote nothing")
    live_alias = dict(env)
    live_alias.pop("RIG_GH_ALIAS_DRY_RUN", None)
    rig(live_alias, "init", "-C", tmp, "--config", config, "--yes", "--apply")
    excludes = installed_artifacts(tmp, live_alias)
    summary = unchanged_summary(rig(live_alias, "apply", "commit", "-C", tmp, "--config", config).stdout,
                                "second apply")
    passed(f"rig apply is idempotent ({summary})")
    require(sum("rig-managed" in line for line in excludes.read_text().splitlines()) == 2,
            "global-excludes block churned/duplicated on re-apply")
    passed("global-excludes block is byte-stable across re-apply (markers=2)")
    alias_check(source, live_alias)
    rig(live_alias, "status", "-C", tmp, "--config", config)
    passed("rig status: in sync")


def tg_ctl_check(source: Path | None, env, fast: bool, stack: ExitStack) -> None:
    if fast:
        skipped("tg-ctl leg — --fast (apply leg; runs in CI)")
        return
    if platform.system() != "Darwin":
        skipped("tg-ctl leg — not macOS (launchd-only)")
        return
    if source is None:
        skipped("tg-ctl leg — no agent-tools checkout (set RIG_AGENT_TOOLS_SOURCE)")
        return
    tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="rig-smoke-tg-")))
    own = isolated(tmp, env)
    global_fixture(own)
    init_repo(tmp, own)
    home = Path(own["HOME"])
    config = tmp / "rig.yaml"
    body = disabled_config().replace("tg_ctl: { enabled: false }\n", "")
    body += f"github: {{ ruleset: {{ enabled: false }} }}\nmodels: {{ enabled: false }}\ntg_ctl:\n  enabled: true\n  bun_path: /usr/bin/true\n  tg_ctl_path: {json.dumps(str(home / '.files/bin/tg-ctl'))}\n  config_dir: {json.dumps(str(home / '.config/tg-cli'))}\n"
    write_config(config, source, body)
    rig(own, "apply", "commit", "-C", tmp, "--config", config)
    plist = home / "Library/LaunchAgents/ai.hyperide.tg-ctl.plist"
    require(plist.is_file() and "<string>ai.hyperide.tg-ctl</string>" in plist.read_text(),
            "tg-ctl plist missing or missing Label")
    unchanged_summary(rig(own, "apply", "commit", "-C", tmp, "--config", config).stdout, "tg-ctl second apply")
    passed("rig provisions tg-ctl boot LaunchAgent (dry-run, isolated, idempotent)")


def main(argv: list[str]) -> int:
    if any(arg in ("-h", "--help") for arg in argv):
        print(__doc__)
        return 0
    bad = [arg for arg in argv if arg != "--fast"]
    if bad:
        print(f"smoke.sh: unknown argument '{bad[0]}' (use --fast or --help)", file=sys.stderr)
        return 2
    fast = "--fast" in argv
    env = dict(os.environ, RIG_TG_CTL_DRY_RUN="1", RIG_GH_ALIAS_DRY_RUN="1")
    source = source_checkout(env)
    print(f"rig smoke — {ROOT}", flush=True)
    try:
        surface_checks(env)
        with ExitStack() as stack:
            if source is None:
                skipped("apply smoke — no agent-tools checkout found (set RIG_AGENT_TOOLS_SOURCE)")
            else:
                tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="rig-smoke-")))
                own = isolated(tmp, env)
                init_repo(tmp, own)
                if not fast:
                    heavy_apply(tmp, source, own)
                status_checks(tmp, source, own, stack)
                catalog_coverage(tmp, source, own)
            tg_ctl_check(source, env, fast, stack)
        if fast:
            skipped("pytest — --fast (full unit suite runs in CI)")
            print("smoke OK (--fast)")
            return 0
        print("running pytest…", flush=True)
        env.pop("GH_CONFIG_DIR", None)
        cmd = ["uv", "run", "--with", "pytest", "python"] if shutil.which("uv") else [sys.executable]
        result = subprocess.run([*cmd, "-m", "pytest", "-q", str(ROOT / "tests")], cwd=ROOT, env=env)
        require(result.returncode == 0, "pytest")
        passed("pytest")
        print("smoke OK")
        return 0
    except (SmokeFailure, OSError, ValueError) as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
