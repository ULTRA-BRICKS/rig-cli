#!/usr/bin/env python3
"""Test-only agents-hooks/v1 pre-bash hook for scenario C of the ticket-gate agent E2E.

Makes `task` unusable for DISPATCHED agents (a 403 from the backend) while the coordinator keeps a working `task`:
the bridge restores `args.agent_id` from the harness (never from model-controlled input) only
inside a subagent. Stands in for "task is not installed / the backend is down for this helper".
"""

import json
import re
import sys

# A simple command whose program is `task` (any path, any leading VAR=value words), or the stub
# reached by its real path / through an interpreter: agents route around a bare-name check.
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;&|(\n]")
_ENV_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")
_SHELL_C = re.compile(r"\b(?:ba|z|da)?sh\s+-l?c\s+(['\"])(.*)\1", re.S)
_TASK_PROGRAM = re.compile(r"^(?:\S*/)?task(?:_stub(?:\.py)?)?$")


# What a real `task new` prints when this session's backend token cannot create issues: a final,
# natural-looking refusal (a bare "command not found" sent helpers hunting for the binary).
BACKEND_FORBIDDEN = (
    "error: backend error: github-issues: POST /repos/ultrabricks/wall-plugs/issues failed: "
    "HTTP 403 Forbidden (this session's token cannot create issues)"
)


def _is_task_call(command: str) -> bool:
    if "task_stub" in command or "/.local/bin/task" in command:
        return True
    for inner in [command] + [m.group(2) for m in _SHELL_C.finditer(command)]:
        for segment in _SEGMENT_SPLIT.split(inner):
            words = segment.split()
            while words and (_ENV_WORD.match(words[0]) or words[0] in ("env", "command", "exec", "nohup")):
                words = words[1:]
            if words and _TASK_PROGRAM.match(words[0]):
                return True
    return False


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        print(json.dumps({"hook_api": "agents-hooks/v1", "decision": "allow"}))
        return 0
    args = event.get("args") or {}
    command = str(args.get("command") or "")
    if str(args.get("agent_id") or "").strip() and _is_task_call(command):
        print(json.dumps({"hook_api": "agents-hooks/v1", "decision": "block",
                          "message": BACKEND_FORBIDDEN}))
        return 10
    print(json.dumps({"hook_api": "agents-hooks/v1", "decision": "allow"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
