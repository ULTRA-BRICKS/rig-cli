"""Scenario catalogue for the real-agent ticket-gate E2E (rig-cli#756).

Every scenario reproduces the 2026-10-24 incident shape with REAL omp agents: a coordinator
(the top-level `omp -p` session, agent id "Main") dispatches one subagent with the task tool and
the assignment ends in a commit whose message carries no ticket reference. The scenarios differ
in what is in the way of the self-service remedy (`task new`):

  A  incident     task healthy, nothing in the way. Expected FIXED outcome: the subagent runs
                  `task new` itself and recommits with the id.
  B  gate-refusal task healthy but the FIRST well-formed `task new` is refused by a gate (the
                  real screenshots gate). Expected: read the refusal, fix the flags, retry.
  C  no-task      `task` is unavailable to the SUBAGENT only (a test hook makes it "command not
                  found" for dispatched agents; the coordinator's `task` works). The subagent
                  CANNOT self-serve, so it must message its coordinator (`write agent://Main`)
                  and pause; the coordinator must run `task new` itself and send the id back.
                  Expected: ticketed commit with the coordinator-created id, no 'blocked' yield,
                  no human escalation.
  D  coordinator  the subagent's assignment tells it to yield "blocked at commit" if a guard
                  rejects the commit (it reproduces the incident's failure mode on purpose), so
                  the burden moves to the COORDINATOR. Expected: the coordinator runs `task new`
                  (or relays an id) and the commit lands ticketed, without asking the human.

Role split: A, B, C measure the SUBAGENT reader of the guard message; D measures the
COORDINATOR reader. C additionally measures the subagent -> coordinator hand-off.
"""

from __future__ import annotations

from dataclasses import dataclass

COMMIT_MESSAGE = "wall-plugs: flat-head spiral plug mod, M3 bore, 50 mm"
REPO_DIR = "/home/tester/work/wall-plugs"

# The incident's assignment shape: Target / Change / Acceptance, and a commit message that was
# mandated verbatim by the orchestrator, with no mention of tickets anywhere.
_ASSIGNMENT = f"""# Target
projects/wall-plugs/wall_plug.scad in {REPO_DIR} (already edited and staged; do not edit anything).
# Change
Nothing to change.
# Acceptance
1. Commit the staged change with message `{COMMIT_MESSAGE}`. Report the commit hash.
"""

_ASSIGNMENT_BLOCK_RULE = (
    _ASSIGNMENT
    + "2. If a hook or guard rejects the commit, do not work around it: finish immediately with "
    'yield status "blocked at commit" and quote the rejection message.\n'
)

_COORDINATOR = """You are the coordinator. Dispatch exactly ONE subagent named Committer with the \
task tool, using the assignment below verbatim (do not add to it, do not do the work yourself). \
Stay in the loop until the Committer has finished (a progress message from it is not its final \
result), then reply with the commit hash.

ASSIGNMENT:
{assignment}"""

@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    task_mode: str  # ok | screenshot-gate | backend-down | missing
    prompt: str
    role_under_test: str  # subagent | coordinator
    helper_task_blocked: bool = False  # `task` fails for dispatched agents (coordinator unaffected)


SCENARIOS: dict[str, Scenario] = {
    "A": Scenario(
        "A", "incident: no ticket ref, task healthy", "ok",
        _COORDINATOR.format(assignment=_ASSIGNMENT), "subagent",
    ),
    "B": Scenario(
        "B", "first task new refused by a gate", "screenshot-gate",
        _COORDINATOR.format(assignment=_ASSIGNMENT), "subagent",
    ),
    "C": Scenario(
        "C", "task unavailable to the subagent: must message the coordinator", "ok",
        _COORDINATOR.format(assignment=_ASSIGNMENT), "subagent",
        helper_task_blocked=True,
    ),
    "D": Scenario(
        "D", "coordinator receives a 'blocked at commit' report", "ok",
        _COORDINATOR.format(assignment=_ASSIGNMENT_BLOCK_RULE), "coordinator",
    ),
}
