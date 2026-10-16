"""Runpod spend rules of the generated omp guard (rig-cli#714), EXECUTED under bun.

Agents must rent Runpod compute only through ``runpod-run`` (its budget, pod cap and TTL). The
guard denies every direct creation path an agent has — ``runpodctl``/``flash`` argv, raw
HTTP/GraphQL writes to ``*.runpod.io``, the Runpod MCP create/deploy/scale-up devices, GraphQL
from an eval cell — and every agent write to runpod-run's budget ledger or its installed
script/skill (write/edit/apply_patch tool calls and bash write shapes).

Like ``tests/test_omp_guard_signals.py``, these tests run the GENERATED TS, not a Python
re-implementation: one table goes through the exported ``evaluateToolCall`` AND the real
``tool_call`` handler, both of which must agree. Paths resolve against a FAKE ``$HOME`` (bun runs
with ``HOME`` pointed at a temp tree that carries the same symlinks as this machine:
``~/.local/bin/runpod-run`` → the installed skill copy).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from riglib.omp_guard import GUARD_TEMPLATE_VERSION, render_guard_ts
from riglib.permissions import (
    MATCHER_SCOPES,
    OMP_GUARD_ASK_RULES,
    OMP_GUARD_DENY_RULES,
    GuardRule,
)
from tests.test_omp_guard import _bun_bin

RUNPOD_RULE_IDS = (
    "runpodctl-pod-create",
    "runpodctl-pods-create",
    "runpodctl-serverless-create",
    "runpodctl-sls-create",
    "runpodctl-cluster-create",
    "runpodctl-create-pod",
    "runpodctl-create-pods",
    "flash-deploy",
    "runpod-http-create",
    "runpod-graphql-create",
    "runpod-mcp-create",
    "runpod-mcp-scale-up",
    "runpod-budget-write",
    "runpod-run-script-write",
    "eval-runpod-graphql-deploy",
    "eval-runpod-graphql-rent",
)

ALLOW = "allow"
BUDGET = "deny:runpod-budget-write"
SCRIPT = "deny:runpod-run-script-write"
HTTP = "deny:runpod-http-create"
GRAPHQL = "deny:runpod-graphql-create"
MCP = "deny:runpod-mcp-create"
SCALE = "deny:runpod-mcp-scale-up"


def bash(command: str) -> tuple[str, dict]:
    return "bash", {"command": command}


def write(path, content="{}") -> tuple[str, dict]:
    return "write", {"path": path, "content": content}


def edit(text, **extra) -> tuple[str, dict]:
    return "edit", {"input": text, **extra}


def mcp(tool: str, content="{}") -> tuple[str, dict]:
    return write(f"xd://{tool}", content)


def hashline(path: str) -> str:
    return f"[{path}#ABCD]\nPUT 1.=1:\n+x\n"


_GQL_CREATE = '{"query":"mutation { podFindAndDeployOnDemand(input: {gpuCount: 1}) { id } }"}'
_GQL_READ = '{"query":"query { myself { pods { id } } }"}'
_EVAL_FETCH = (
    'await fetch("https://api.runpod.io/graphql", {method: "POST", '
    'body: JSON.stringify({query: "mutation { %s(input: {}) { id } }"})});'
)

# (name, (toolName, input), expected). `{HOME}` is replaced by the fake home's absolute path; the
# cwd of every call is `{HOME}/work`.
CASES: list[tuple[str, tuple[str, dict], str]] = [
    # ── runpodctl / flash argv ────────────────────────────────────────────────────────────
    ("runpodctl pod create", bash("runpodctl pod create --gpu-id x --image y"), "deny:runpodctl-pod-create"),
    ("runpodctl pods create alias", bash("runpodctl pods create --gpu-id x"), "deny:runpodctl-pods-create"),
    ("runpodctl serverless create", bash("runpodctl serverless create --template-id t"), "deny:runpodctl-serverless-create"),
    ("runpodctl sls create alias", bash("runpodctl sls create --template-id t"), "deny:runpodctl-sls-create"),
    ("runpodctl cluster create", bash("runpodctl cluster create --pods 2"), "deny:runpodctl-cluster-create"),
    ("runpodctl create pod (deprecated)", bash("runpodctl create pod --gpuType x"), "deny:runpodctl-create-pod"),
    ("runpodctl create pods (deprecated)", bash("runpodctl create pods --podCount 4"), "deny:runpodctl-create-pods"),
    ("flash deploy", bash("flash deploy --env prod"), "deny:flash-deploy"),
    ("bash -c pod create (nested re-parse)", bash("bash -c 'runpodctl pod create --gpuType x'"), "deny:runpodctl-pod-create"),
    ("sudo path-qualified pod create", bash("sudo /usr/local/bin/runpodctl pod create"), "deny:runpodctl-pod-create"),
    ("global -o flag before pod create", bash("runpodctl -o json pod create --gpu-id x"), "deny:runpodctl-pod-create"),
    ("env + timeout wrapped pod create", bash("FOO=1 timeout 60 runpodctl pod create"), "deny:runpodctl-pod-create"),
    ("pod create in a later stage", bash("runpodctl pod list && runpodctl pod create"), "deny:runpodctl-pod-create"),
    ("runpodctl pod list", bash("runpodctl pod list"), ALLOW),
    ("runpodctl pod get", bash("runpodctl pod get abc123"), ALLOW),
    ("runpodctl pod stop", bash("runpodctl pod stop abc123"), ALLOW),
    ("runpodctl pod delete", bash("runpodctl pod delete abc123"), ALLOW),
    ("runpodctl user", bash("runpodctl user"), ALLOW),
    ("runpodctl billing", bash("runpodctl billing pods --bucket-size day"), ALLOW),
    ("runpodctl doctor", bash("runpodctl doctor"), ALLOW),
    ("flash --help", bash("flash --help"), ALLOW),
    ("runpod-run dry run", bash("runpod-run --dry-run -- pytest tests"), ALLOW),
    ("runpod-run status", bash("runpod-run --status"), ALLOW),
    ("echo mentions pod create", bash("echo runpodctl pod create"), ALLOW),
    ("commit message mentions pod create", bash("git commit -m 'deny runpodctl pod create'"), ALLOW),
    # ── raw HTTP writes to *.runpod.io ────────────────────────────────────────────────────
    ("curl GET pods", bash('curl -s https://rest.runpod.io/v1/pods -H "Authorization: Bearer k"'), ALLOW),
    ("curl -d pods", bash("curl -s https://rest.runpod.io/v1/pods -H 'Authorization: Bearer k' -d '{\"name\":\"x\"}'"), HTTP),
    ("curl -X POST endpoints", bash("curl -X POST https://rest.runpod.io/v1/endpoints"), HTTP),
    ("curl -X PATCH endpoint", bash("curl -X PATCH https://rest.runpod.io/v1/endpoints/abc -d '{\"workersMax\":3}'"), HTTP),
    ("curl --request PUT v2 cluster", bash("curl --request PUT https://api.runpod.io/v2/clusters/c1"), HTTP),
    ("curl clustered -sXPOST", bash("curl -sXPOST https://rest.runpod.io/v1/pods"), HTTP),
    ("curl -T upload is PUT", bash("curl -T body.json https://rest.runpod.io/v1/pods"), HTTP),
    ("curl --json without scheme", bash("curl --json '{}' rest.runpod.io/v1/pods"), HTTP),
    ("curl unresolved method", bash('curl -X "$M" https://rest.runpod.io/v1/pods'), HTTP),
    ("curl --next second request writes", bash("curl https://rest.runpod.io/v1/pods --next -d x https://rest.runpod.io/v1/pods"), HTTP),
    ("curl POST pod start sub-path", bash("curl -X POST https://rest.runpod.io/v1/pods/abc/start"), HTTP),
    ("curl doubled slash", bash("curl -d x https://rest.runpod.io/v1//pods"), HTTP),
    ("curl percent-encoded path", bash("curl -d x https://rest.runpod.io/v1/%70ods"), HTTP),
    ("curl userinfo port and case", bash("curl -d x https://u@REST.RunPod.io:443/v1/pods"), HTTP),
    ("curl query string", bash("curl -d x 'https://rest.runpod.io/v1/pods?x=1'"), HTTP),
    ("sh -c curl POST", bash("sh -c 'curl -d x https://rest.runpod.io/v1/pods'"), HTTP),
    ("sudo path-qualified curl POST", bash("sudo /usr/bin/curl -d x https://rest.runpod.io/v1/pods"), HTTP),
    ("wget --post-data", bash("wget --post-data='{}' https://rest.runpod.io/v1/pods"), HTTP),
    ("wget --method=POST", bash("wget --method=POST https://rest.runpod.io/v1/pods"), HTTP),
    ("httpie POST word", bash("http POST https://rest.runpod.io/v1/pods name=x"), HTTP),
    ("xh implied POST from item", bash("xh https://rest.runpod.io/v1/pods name=x"), HTTP),
    ("curl DELETE pod stays allowed", bash("curl -X DELETE https://rest.runpod.io/v1/pods/abc"), ALLOW),
    ("curl -G data is a GET", bash("curl -G -d 'x=1' https://rest.runpod.io/v1/pods"), ALLOW),
    ("curl -I is a HEAD", bash("curl -I https://rest.runpod.io/v1/pods"), ALLOW),
    ("curl POST other host", bash("curl -d x https://example.com/v1/pods"), ALLOW),
    ("curl POST lookalike host", bash("curl -d x https://notrunpod.io/v1/pods"), ALLOW),
    ("curl POST templates", bash("curl -d x https://rest.runpod.io/v1/templates"), ALLOW),
    ("curl POST podsx path", bash("curl -d x https://rest.runpod.io/v1/podsx"), ALLOW),
    ("wget GET pods", bash("wget -qO- https://rest.runpod.io/v1/pods"), ALLOW),
    ("httpie GET pods", bash("http https://rest.runpod.io/v1/pods Authorization:'Bearer k'"), ALLOW),
    # a request read from a file the guard cannot see before it runs fails closed
    ("curl -K config file", bash("curl -K /tmp/curlrc"), HTTP),
    ("curl --config= config file", bash("curl --config=/tmp/curlrc -s"), HTTP),
    ("curl expanded {{var}} URL write", bash("curl --variable host@/tmp/h --expand-url 'https://{{host}}/v1/pods' -d x"), HTTP),
    ("wget -i write", bash("wget --post-data='{}' -i /tmp/urls"), HTTP),
    ("wget -e method=POST", bash("wget -e method=POST https://rest.runpod.io/v1/pods"), HTTP),
    ("wget -i GET", bash("wget -i /tmp/urls"), ALLOW),
    ("wget -e robots=off mirror", bash("wget -e robots=off -r https://example.com/docs/"), ALLOW),
    # ── GraphQL ───────────────────────────────────────────────────────────────────────────
    ("graphql podFindAndDeployOnDemand", bash(f"curl -X POST https://api.runpod.io/graphql -d '{_GQL_CREATE}'"), GRAPHQL),
    ("graphql podRentInterruptable", bash("curl https://api.runpod.io/graphql -d '{\"query\":\"mutation { podRentInterruptable(input:{}) { id } }\"}'"), GRAPHQL),
    ("graphql createCluster", bash("curl https://api.runpod.io/graphql --json '{\"query\":\"mutation { createCluster(input:{}) { id } }\"}'"), GRAPHQL),
    ("graphql deployCpuPod", bash("curl https://api.runpod.io/graphql -d '{\"query\":\"mutation { deployCpuPod(input:{}) { id } }\"}'"), GRAPHQL),
    ("graphql saveEndpoint", bash("curl https://api.runpod.io/graphql -d '{\"query\":\"mutation { saveEndpoint(input:{}) { id } }\"}'"), GRAPHQL),
    ("graphql operation spelled with a JSON escape", bash("curl https://api.runpod.io/graphql -d '{\"query\":\"mutation { \\u0070odFindAndDeployOnDemand(input:{}) { id } }\"}'"), GRAPHQL),
    ("graphql body from @file", bash("curl -d @body.json https://api.runpod.io/graphql"), GRAPHQL),
    ("graphql body from stdin", bash("curl --data-binary @- https://api.runpod.io/graphql"), GRAPHQL),
    ("graphql body shell-expanded", bash('curl -d "$BODY" https://api.runpod.io/graphql'), GRAPHQL),
    ("graphql mutation in the URL", bash("curl 'https://api.runpod.io/graphql?query=mutation%20%7B%20podFindAndDeployOnDemand%20%7D'"), GRAPHQL),
    ("httpie graphql item", bash("http POST api.runpod.io/graphql query='mutation { podFindAndDeployOnDemand }'"), GRAPHQL),
    ("httpie graphql body from file", bash("http POST api.runpod.io/graphql @body.json"), GRAPHQL),
    ("graphql read query", bash(f"curl -H 'content-type: application/json' https://api.runpod.io/graphql -d '{_GQL_READ}'"), ALLOW),
    ("graphql --data-raw read query", bash(f"curl --data-raw '{_GQL_READ}' https://api.runpod.io/graphql"), ALLOW),
    ("graphql --data-raw keeps a leading @ literal", bash("curl --data-raw '@not-a-file' https://api.runpod.io/graphql"), ALLOW),
    # ── Runpod MCP devices (omp routes MCP calls as `write xd://mcp__<server>_<tool>`) ────
    ("mcp create_pod", mcp("mcp__runpod_create_pod", '{"body":{"gpuTypeIds":["x"]}}'), MCP),
    ("mcp create_endpoint", mcp("mcp__runpod_create_endpoint"), MCP),
    ("mcp create_cluster", mcp("mcp__runpod_create_cluster"), MCP),
    ("mcp deploy_hub_repo", mcp("mcp__runpod_deploy_hub_repo"), MCP),
    ("mcp upper-case device", mcp("MCP__RUNPOD_CREATE_POD"), MCP),
    ("mcp percent-encoded device", mcp("mcp__runpod_create%5Fpod"), MCP),
    ("mcp hyphenated device", mcp("mcp__runpod_create-pod"), MCP),
    ("mcp device with a sub-path", mcp("mcp__runpod_create_pod/x"), MCP),
    ("mcp create with non-string content", ("write", {"path": "xd://mcp__runpod_create_pod", "content": 42}), MCP),
    ("mcp direct tool name", ("mcp__runpod_create_pod", {"body": {}}), MCP),
    ("mcp update_endpoint workersMax > 0", mcp("mcp__runpod_update_endpoint", '{"endpointId":"e","body":{"workersMax":2}}'), SCALE),
    ("mcp update_endpoint workersMin > 0 top level", mcp("mcp__runpod_update_endpoint", '{"workersMin":1}'), SCALE),
    ("mcp update_endpoint numeric string", mcp("mcp__runpod_update_endpoint", '{"body":{"workersMin":"3"}}'), SCALE),
    ("mcp update_endpoint unparseable", mcp("mcp__runpod_update_endpoint", "not json"), SCALE),
    ("mcp update_endpoint missing content", ("write", {"path": "xd://mcp__runpod_update_endpoint"}), SCALE),
    ("mcp update_endpoint rename", mcp("mcp__runpod_update_endpoint", '{"endpointId":"e","body":{"name":"x"}}'), ALLOW),
    ("mcp update_endpoint scale to zero", mcp("mcp__runpod_update_endpoint", '{"body":{"workersMin":0,"workersMax":0}}'), ALLOW),
    ("mcp list_pods", mcp("mcp__runpod_list_pods"), ALLOW),
    ("mcp get_pod", mcp("mcp__runpod_get_pod", '{"podId":"p"}'), ALLOW),
    ("mcp delete_pod", mcp("mcp__runpod_delete_pod", '{"podId":"p"}'), ALLOW),
    ("mcp delete_endpoint", mcp("mcp__runpod_delete_endpoint", '{"endpointId":"e"}'), ALLOW),
    ("read the create_pod device docs", ("read", {"path": "xd://mcp__runpod_create_pod"}), ALLOW),
    # ── write/edit/apply_patch to runpod-run's protected paths ────────────────────────────
    ("write budget via ~", write("~/.config/runpod-run/budget.json"), BUDGET),
    ("write budget absolute", write("{HOME}/.config/runpod-run/budget"), BUDGET),
    ("write budget via $HOME", write("$HOME/.config/runpod-run/budget-ledger.jsonl"), BUDGET),
    ("write budget relative to cwd", write("../.config/runpod-run/budget.json"), BUDGET),
    ("write budget through ..", write("~/.config/runpod-run/../runpod-run/budget.json"), BUDGET),
    ("write budget through a file symlink", write("~/sneaky"), BUDGET),
    ("write new budget file through a dir symlink", write("~/linkdir/budget-new.json"), BUDGET),
    ("write runpod-run config (not budget)", write("~/.config/runpod-run/config.toml"), ALLOW),
    ("write installed script", write("~/.local/bin/runpod-run"), SCRIPT),
    ("write installed skill file", write("~/.agents/skills/runpod-offload/SKILL.md"), SCRIPT),
    ("write lookalike skill dir", write("~/.agents/skills/runpod-offloader/SKILL.md"), ALLOW),
    ("write in the worktree", write("src/app.py"), ALLOW),
    ("write with a non-string path", ("write", {"path": 42, "content": "x"}), ALLOW),
    ("write with no input", ("write", None), ALLOW),
    ("edit installed script hashline", edit(hashline("~/.agents/skills/runpod-offload/runpod-run")), SCRIPT),
    ("edit agent-tools source copy", edit(hashline("~/dev/agent-tools/skills/universal/runpod-offload/runpod-run")), ALLOW),
    ("edit second section protected", edit(hashline("src/a.py") + hashline("~/.local/bin/runpod-run")), SCRIPT),
    ("edit MV onto installed script", edit("[src/a.py#ABCD]\nMV ~/.local/bin/runpod-run\n"), SCRIPT),
    ("edit body row naming the script", edit("[src/a.py#ABCD]\nPUT 1.=1:\n+[~/.local/bin/runpod-run#ABCD]\n"), ALLOW),
    ("edit malformed input on a protected path", ("edit", {"path": "~/.local/bin/runpod-run", "input": 5}), SCRIPT),
    ("edit malformed input elsewhere", ("edit", {"input": 5}), ALLOW),
    ("apply_patch update installed script", ("apply_patch", {"input": "*** Begin Patch\n*** Update File: ~/.local/bin/runpod-run\n@@\n-a\n+b\n*** End Patch\n"}), SCRIPT),
    ("apply_patch move onto budget", ("apply_patch", {"patch": "*** Begin Patch\n*** Update File: notes.txt\n*** Move to: ~/.config/runpod-run/budget.json\n*** End Patch\n"}), BUDGET),
    ("apply_patch add file in worktree", ("apply_patch", {"input": "*** Begin Patch\n*** Add File: src/new.py\n+x\n*** End Patch\n"}), ALLOW),
    # ── bash write shapes on protected paths ──────────────────────────────────────────────
    ("redirect into budget", bash("echo 0 > ~/.config/runpod-run/budget.json"), BUDGET),
    ("clobber redirect into budget", bash("echo 0 >| ~/.config/runpod-run/budget.json"), BUDGET),
    ("&> redirect into installed script", bash("echo x &> ~/.local/bin/runpod-run"), SCRIPT),
    ("append into budget", bash("echo 0 >> ~/.config/runpod-run/budget.json"), BUDGET),
    ("stderr redirect into budget", bash("ls 2>~/.config/runpod-run/budget.json"), BUDGET),
    ("tee -a into budget", bash("echo 0 | tee -a ~/.config/runpod-run/budget.json"), BUDGET),
    ("sudo tee installed script", bash("sudo tee ~/.local/bin/runpod-run < /tmp/x"), SCRIPT),
    ("cp over installed script", bash("cp /tmp/x ~/.local/bin/runpod-run"), SCRIPT),
    ("cp into the budget dir", bash("cp /tmp/budget.json ~/.config/runpod-run/"), BUDGET),
    ("cp -t into the budget dir", bash("cp -t ~/.config/runpod-run budget.json"), BUDGET),
    ("mv installed script away", bash("mv ~/.local/bin/runpod-run /tmp/old"), SCRIPT),
    ("mv a file named runpod-run into ~/.local/bin", bash("mv /tmp/runpod-run ~/.local/bin/"), SCRIPT),
    ("install over installed script", bash("install -m 755 /tmp/x ~/.local/bin/runpod-run"), SCRIPT),
    ("ln -sf over installed script", bash("ln -sf /tmp/evil ~/.local/bin/runpod-run"), SCRIPT),
    ("rm budget", bash("rm ~/.config/runpod-run/budget.json"), BUDGET),
    ("rm -rf installed skill dir", bash("rm -rf ~/.agents/skills/runpod-offload"), SCRIPT),
    ("rm -rf an ancestor of the budget", bash("rm -rf ~/.config"), BUDGET),
    ("rm glob inside the budget dir", bash("rm ~/.config/runpod-run/*"), BUDGET),
    ("rm glob matching the installed script", bash("rm -f ~/.local/bin/runpod-*"), SCRIPT),
    ("chmod installed script", bash("chmod +x ~/.local/bin/runpod-run"), SCRIPT),
    ("truncate budget", bash("truncate -s 0 ~/.config/runpod-run/budget.json"), BUDGET),
    ("dd of= budget", bash("dd if=/dev/zero of=$HOME/.config/runpod-run/budget.json count=1"), BUDGET),
    ("sed -i installed script", bash("sed -i 's/a/b/' ~/.local/bin/runpod-run"), SCRIPT),
    ("sed -i.bak installed script", bash("sed -i.bak -e 's/a/b/' ~/.local/bin/runpod-run"), SCRIPT),
    ("perl -pi installed script", bash("perl -pi -e 's/a/b/' ~/.local/bin/runpod-run"), SCRIPT),
    ("cd then relative redirect", bash("cd ~/.config/runpod-run && echo 0 > budget.json"), BUDGET),
    ("failed cd keeps the old cwd", bash("cd /nonexistent; echo 0 > ../.config/runpod-run/budget.json"), BUDGET),
    ("bash -c redirect into budget", bash("bash -c 'echo 0 > ~/.config/runpod-run/budget.json'"), BUDGET),
    ("redirect through a file symlink", bash("echo 0 > ~/sneaky"), BUDGET),
    ("cat budget", bash("cat ~/.config/runpod-run/budget.json"), ALLOW),
    ("less installed script", bash("less ~/.local/bin/runpod-run"), ALLOW),
    ("grep installed script", bash("grep -n budget ~/.local/bin/runpod-run"), ALLOW),
    ("sed -n read of installed script", bash("sed -n 1,20p ~/.local/bin/runpod-run"), ALLOW),
    ("cp budget out (a read)", bash("cp ~/.config/runpod-run/budget.json /tmp/budget-copy.json"), ALLOW),
    # a link gives the protected file a second, unprotected name the next stage could write
    ("ln -s naming the installed script", bash("ln -s ~/.local/bin/runpod-run /tmp/rr"), SCRIPT),
    ("ln -s then write through the link", bash("ln -s ~/.config/runpod-run/budget.json /tmp/b && echo 0 > /tmp/b"), BUDGET),
    ("hard link to the budget", bash("ln ~/.config/runpod-run/budget.json /tmp/h"), BUDGET),
    ("one-operand ln -s into cwd", bash("ln -s ~/.config/runpod-run/budget.json"), BUDGET),
    ("cp -s symlink of the budget", bash("cp -s ~/.config/runpod-run/budget.json /tmp/b"), BUDGET),
    ("ln -s to an unrelated file", bash("ln -s /tmp/tool ~/.local/bin/tool"), ALLOW),
    ("read budget then redirect elsewhere", bash("cat ~/.config/runpod-run/budget.json > /tmp/b"), ALLOW),
    ("mv an unrelated file into ~/.local/bin", bash("mv /tmp/tool ~/.local/bin/"), ALLOW),
    ("rm unrelated cache", bash("rm -rf ~/.cache/foo"), ALLOW),
    ("rm unrelated glob in home", bash("rm -f ~/*.log"), ALLOW),
    ("rm a file named [", bash("rm /tmp/["), ALLOW),
    ("rm a file named a][", bash("rm '/tmp/a]['"), ALLOW),
    ("sed -i agent-tools source copy", bash("sed -i 's/a/b/' ~/dev/agent-tools/skills/universal/runpod-offload/runpod-run"), ALLOW),
    # ── eval cells ───────────────────────────────────────────────────────────────────────
    ("eval GraphQL podFindAndDeployOnDemand", ("eval", {"language": "js", "code": _EVAL_FETCH % "podFindAndDeployOnDemand"}), "deny:eval-runpod-graphql-deploy"),
    ("eval GraphQL podRentInterruptable", ("eval", {"language": "js", "code": _EVAL_FETCH % "podRentInterruptable"}), "deny:eval-runpod-graphql-rent"),
    ("eval GraphQL spelled with a JS escape", ("eval", {"language": "js", "code": _EVAL_FETCH % "\\u0070odFindAndDeployOnDemand"}), "deny:eval-runpod-graphql-deploy"),
    ("eval GraphQL read query", ("eval", {"language": "js", "code": _EVAL_FETCH % "myself"}), ALLOW),
    ("eval longer identifier is not the needle", ("eval", {"language": "py", "code": "x = podFindAndDeployOnDemandLegacy"}), ALLOW),
]

_RUNNER = r'''
import { readFileSync } from "node:fs";
import factory, { evaluateToolCall } from "./guard.ts";
const [casesPath, cwd] = process.argv.slice(2);
const cases: [string, string, any][] = JSON.parse(readFileSync(casesPath, "utf8"));
const handlers: Record<string, any> = {};
factory({ setLabel: () => {}, on: (ev: string, h: any) => { handlers[ev] = h; }, getAllTools: () => [] });
const label = (r: any) => {
  if (!r) return "allow";
  const m = /^rig guard (deny|ask) ([^ :]+)/.exec(r.reason ?? "");
  return m ? m[1] + ":" + m[2] : "block:" + r.reason;
};
const out: Record<string, { evaluated: string; handled: string; confirmed: string }> = {};
for (const [name, toolName, input] of cases) {
  const findings = evaluateToolCall(toolName, input, undefined, cwd);
  const hit = findings.find((f) => f.kind === "deny") ?? findings[0];
  const handled = await handlers["tool_call"]({ toolName, input }, { hasUI: false, cwd });
  const confirmed = await handlers["tool_call"]({ toolName, input }, { hasUI: true, cwd, ui: { confirm: async () => true } });
  out[name] = { evaluated: hit ? hit.kind + ":" + hit.rule.id : "allow", handled: label(handled), confirmed: label(confirmed) };
}
process.stdout.write(JSON.stringify(out) + "\n");
'''


def _bun() -> str | None:
    found = _bun_bin()
    if found:
        return found
    if os.environ.get("CI"):
        # CI installs bun on purpose: a silent skip there would leave the enforcement layer
        # untested on every PR.
        pytest.fail("bun is required in CI to execute the generated guard")
    return None


def _fake_home(home: Path) -> None:
    """The protected layout of this machine: the budget ledger, the installed skill copy, and
    ``~/.local/bin/runpod-run`` as a symlink to it — plus two symlinks a write could hide behind
    and the agent-tools source copy that stays editable."""
    budget_dir = home / ".config" / "runpod-run"
    budget_dir.mkdir(parents=True)
    (budget_dir / "budget.json").write_text("{}\n", encoding="utf-8")
    skill = home / ".agents" / "skills" / "runpod-offload"
    skill.mkdir(parents=True)
    (skill / "runpod-run").write_text("#!/bin/sh\n", encoding="utf-8")
    (skill / "SKILL.md").write_text("# runpod-offload\n", encoding="utf-8")
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "runpod-run").symlink_to(skill / "runpod-run")
    (home / "sneaky").symlink_to(budget_dir / "budget.json")
    (home / "linkdir").symlink_to(budget_dir)
    source = home / "dev" / "agent-tools" / "skills" / "universal" / "runpod-offload"
    source.mkdir(parents=True)
    (source / "runpod-run").write_text("#!/bin/sh\n", encoding="utf-8")
    (home / "work").mkdir()


def _with_home(value, home: str):
    if isinstance(value, str):
        return value.replace("{HOME}", home)
    if isinstance(value, dict):
        return {k: _with_home(v, home) for k, v in value.items()}
    if isinstance(value, list):
        return [_with_home(v, home) for v in value]
    return value


def _guard_env(home: Path) -> dict[str, str]:
    return {**os.environ, "HOME": str(home)}


@pytest.fixture(scope="module")
def verdicts(tmp_path_factory) -> dict[str, dict[str, str]]:
    bun = _bun()
    if not bun:
        pytest.skip("bun runtime not available")
    work = tmp_path_factory.mktemp("guard-runpod")
    home = work / "home"
    _fake_home(home)
    (work / "guard.ts").write_text(render_guard_ts(), encoding="utf-8")
    (work / "run.mts").write_text(_RUNNER, encoding="utf-8")
    table = [[n, tool, _with_home(inp, str(home))] for n, (tool, inp), _ in CASES]
    (work / "cases.json").write_text(json.dumps(table), encoding="utf-8")
    proc = subprocess.run(
        [bun, "run.mts", str(work / "cases.json"), str(home / "work")],
        cwd=work, capture_output=True, text=True, timeout=120, env=_guard_env(home),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_case_names_are_unique():
    names = [n for n, _, _ in CASES]
    assert len(names) == len(set(names))


@pytest.mark.parametrize(("name", "call", "expected"), CASES, ids=[c[0] for c in CASES])
def test_runpod_rule_verdict(verdicts, name, call, expected):
    got = verdicts[name]
    # evaluateToolCall (the check CLI's engine) and the real handler must agree, and a deny
    # holds even when an operator would confirm every prompt
    assert got == {"evaluated": expected, "handled": expected, "confirmed": expected}, f"{call!r}"


def test_every_runpod_rule_is_a_deny_whose_hint_starts_with_use_runpod_run():
    deny = {r.id: r for r in OMP_GUARD_DENY_RULES}
    assert not set(RUNPOD_RULE_IDS) & {r.id for r in OMP_GUARD_ASK_RULES}
    for rule_id in RUNPOD_RULE_IDS:
        assert rule_id in deny, rule_id
        assert deny[rule_id].hint.startswith("use runpod-run"), rule_id
    # and the list above is the whole family: a new runpod-run rule must be added here (and to
    # the verdict table) too
    assert {r.id for r in OMP_GUARD_DENY_RULES if r.hint.startswith("use runpod-run")} == set(RUNPOD_RULE_IDS)


def test_every_rule_scope_is_its_matchers_scope():
    for rule in (*OMP_GUARD_DENY_RULES, *OMP_GUARD_ASK_RULES):
        assert rule.scope == MATCHER_SCOPES.get(rule.matcher, ("bash",)), rule.id
    assert MATCHER_SCOPES["mcp_tool"] == ("write",)
    assert MATCHER_SCOPES["write_path"] == ("bash", "write", "edit", "apply_patch")
    assert "http_write_url" not in MATCHER_SCOPES  # an argv matcher: the default ("bash",)


def test_template_version_is_4():
    assert GUARD_TEMPLATE_VERSION == 4


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"matcher": "mcp_tool", "tokens": ("mcp__runpod_create_pod",)}, "scope"),
        ({"matcher": "mcp_tool", "tokens": (), "scope": ("write",)}, "mcp_tool needs"),
        ({"matcher": "mcp_tool", "tokens": ("mcp__runpod_create-pod",), "scope": ("write",)}, "mcp_tool needs"),
        ({"matcher": "mcp_tool", "tokens": ("mcp__x_y",), "value_prefixes": ("a",), "scope": ("write",)}, "mcp_tool needs"),
        ({"matcher": "write_path", "tokens": ("relative/path",), "scope": ("bash", "write", "edit", "apply_patch")}, "write_path needs"),
        ({"matcher": "write_path", "tokens": ("~/x",), "scope": ("write",)}, "scope"),
        ({"matcher": "write_path", "tokens": ("~/x",), "flags": ("-f",), "scope": ("bash", "write", "edit", "apply_patch")}, "write_path needs"),
        ({"matcher": "http_write_url", "tokens": (), "value_prefixes": ("^/x",)}, "http_write_url needs"),
        ({"matcher": "http_write_url", "tokens": ("runpod.io",), "value_prefixes": ()}, "http_write_url needs"),
        ({"matcher": "http_write_url", "tokens": ("https://runpod.io",), "value_prefixes": ("^/x",)}, "http_write_url needs"),
        ({"matcher": "http_write_url", "tokens": ("runpod.io",), "value_prefixes": ("([",)}, "regex"),
        ({"matcher": "code_pattern", "tokens": ("x",), "flags": ("bogus",), "scope": ("eval",)}, "code_pattern needs"),
        ({"matcher": "argv_prefix", "tokens": ("x",), "scope": ("write",)}, "scope"),
    ],
)
def test_guard_rule_rejects_malformed_runpod_matcher_shapes(kwargs, message):
    with pytest.raises(ValueError, match=message):
        GuardRule("bad", "hint", **kwargs)


def test_guard_rule_accepts_the_new_matcher_shapes():
    GuardRule("ok-mcp", "h", "mcp_tool", ("mcp__runpod_update_endpoint",), ("workersMin",), scope=("write",))
    GuardRule("ok-path", "h", "write_path", ("~/.x/y*", "/etc/z"), scope=("bash", "write", "edit", "apply_patch"))
    GuardRule("ok-http", "h", "http_write_url", ("runpod.io",), ("needle",), (r"^/graphql(/|$|\?)",))
    GuardRule("ok-word", "h", "code_pattern", ("needle",), ("word",), scope=("eval",))


def _check(bun: str, guard: Path, home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [bun, str(guard), *args], cwd=home / "work", capture_output=True, text=True, timeout=120,
        env=_guard_env(home),
    )


def test_check_cli_evaluates_a_non_bash_tool_call(tmp_path):
    """``bun rig-permissions-guard.ts --tool <name> --input '<json>'`` is the live proof for the
    write/edit/MCP surfaces: same verdict line and exit code as the bash form."""
    bun = _bun()
    if not bun:
        pytest.skip("bun runtime not available")
    home = tmp_path / "home"
    _fake_home(home)
    guard = tmp_path / "rig-permissions-guard.ts"
    guard.write_text(render_guard_ts(), encoding="utf-8")

    deny = _check(bun, guard, home, "--tool", "write", "--input",
                  json.dumps({"path": "xd://mcp__runpod_create_pod", "content": "{}"}))
    assert deny.returncode == 2, deny.stdout + deny.stderr
    assert deny.stdout.startswith("deny runpod-mcp-create: use runpod-run")

    allow = _check(bun, guard, home, "--tool", "write", "--input",
                   json.dumps({"path": "xd://mcp__runpod_list_pods", "content": "{}"}))
    assert allow.returncode == 0, allow.stdout + allow.stderr
    assert allow.stdout.strip() == "allow"

    edit_deny = _check(bun, guard, home, "--tool", "edit", "--input",
                       json.dumps({"input": hashline("~/.local/bin/runpod-run")}))
    assert edit_deny.returncode == 2, edit_deny.stdout + edit_deny.stderr
    assert edit_deny.stdout.startswith("deny runpod-run-script-write: use runpod-run")

    via_tool = _check(bun, guard, home, "--tool", "bash", "--input",
                      json.dumps({"command": "runpodctl pod create --gpu-id x"}))
    assert via_tool.returncode == 2, via_tool.stdout + via_tool.stderr
    assert via_tool.stdout.startswith("deny runpodctl-pod-create: use runpod-run")

    positional = _check(bun, guard, home, "runpodctl pod create --gpu-id x")
    assert positional.returncode == 2, positional.stdout + positional.stderr

    bad = _check(bun, guard, home, "--tool", "write", "--input", "not json")
    assert bad.returncode == 64, bad.stdout + bad.stderr
    assert "--input" in bad.stderr
