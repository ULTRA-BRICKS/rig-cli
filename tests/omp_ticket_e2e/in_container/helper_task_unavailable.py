#!/usr/bin/env python3
"""Test-only agents-hooks/v1 pre-bash hook for scenario C of the ticket-gate agent E2E.

Makes `task` look missing to DISPATCHED agents while the coordinator keeps a working `task`:
the bridge restores `args.agent_id` from the harness (never from model-controlled input) only
inside a subagent. Stands in for "task is not installed / the backend is down for this helper".
"""

import json
import re
import sys

TASK_CALL = re.compile(r"(?:^|[;&|(]|&&|\|\|)\s*(?:\S+/)?task(?:\s|$)")


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        print(json.dumps({"hook_api": "agents-hooks/v1", "decision": "allow"}))
        return 0
    args = event.get("args") or {}
    command = str(args.get("command") or "")
    if str(args.get("agent_id") or "").strip() and TASK_CALL.search(command):
        print(json.dumps({"hook_api": "agents-hooks/v1", "decision": "block",
                          "message": "bash: task: command not found"}))
        return 10
    print(json.dumps({"hook_api": "agents-hooks/v1", "decision": "allow"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
