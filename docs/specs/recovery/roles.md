# Independent roles, models and execution routes

## User request

> «Любое M доступных моделей должно уметь заполнять любое R требуемых ролей».

Clarification in this conversation, 11 September 2026. The earlier request (export record 227) required every role to be supported in every review mode even when only one model was available.

## Contract

The required work is a set of R roles. The available execution resources include M eligible models and their compatible provider, harness and account routes. These are independent dimensions; the number of distinct models must not silently determine the number of roles performed.

For every required role, schedule an actual invocation with that role's prompt and retain its actual outcome. Reusing a model is valid; relabelling or copying one answer is not another invocation. A moderator, synthesis pass or retry does not increase the number of completed expert roles.

This applies in normal operation as well as outages. Cover M<R, M=R and M>R. Zero eligible models or an unsupported role/capability must produce an explicit unsatisfied result, not fabricated coverage.

## Separate controls

Model selection identifies eligible models. Role selection describes the required perspectives. Provider and harness selection identify compatible execution routes. Account selection uses account-specific availability. Effort follows the route that actually executes. Concurrency limits the number of simultaneous invocations, not the required role set.

For example, two available models and five required roles can execute five role-specific invocations with a concurrency of one or two. The exact assignment policy can prefer diversity, priority or capacity, but must remain visible and must not drop a role because M is smaller than R. More models than roles do not require unnecessary additional work unless explicitly requested by the chosen review policy.

## Execution modes

Use one role-planning contract in the library and every model-executing mode. Preserve each mode's purpose: diff inspection, direct answers, expert discussion, brainstorm rounds, visual inspection and testing. Preserve read-only or side-effect-isolation requirements when a mode executes tools.

Role coverage must not depend on whether models came from a default roster, an explicit list, a configured board or a preset. In multi-round modes, identify the round and the role's actual result. Do not count a repeated role in another round as a new distinct role.

`task`, `stat` and other management commands consume evidence rather than run additional expert roles. For the four text execution commands observed in current main, a task code is ordinarily required through `--task` or `REVIEW_TASK_CODE`; it is not an exceptional condition restricting this feature.

## Current evidence, not desired behavior

At review-cli `5430f9361468bf9bd1a66a4d4be8198024f989ba`, an isolated experiment ran real CLI parsing, selection, mode handlers, prompts, scheduling and statistics writing. Only provider availability and model responses were fixtures.

With one available named model and default selection:

| Mode | Actual expert work | Named review-role evidence |
|---|---|---|
| diff | Three role-specific calls | Three roles recorded |
| just-ask | One generic answer call | Absent |
| quorum | One expert persona, plus a moderator | Absent |
| brainstorm | Three different personas per round, for three rounds | Absent |

The real evidence consumer, `review task 444 --check --min-iter 1 --min-roles 3 --json`, accepts the first record and rejects the other three with zero **credited** named review roles. This is not a claim that quorum or brainstorm executed no personas.

Two explicitly selected models take different assignment paths again. A failed-role control confirms that failed work is not accepted as complete three-role coverage by the evidence reader. The complete observations and their limits are in the verification notes.

## Acceptance matrix

| Dimension | Required cases |
|---|---|
| Available models versus roles | M<R, M=R, M>R; include M=0, M=1 and M=2 |
| Selection | Defaults, explicit models, configured board, presets |
| Required roles | Different valid role sets and counts; malformed or unsupported roles |
| Scheduling | Sequential and concurrent execution without silently reducing R |
| Outcomes | All roles succeed; one role fails; route replacement; no eligible route |
| Modes | Every execution mode and supported visual composition |
| Evidence | Actual prompts/results, persisted records and the consuming CLI/library |

Tests must exercise actual planners and handlers. A handler stub inspecting an optional metadata field is a routing test, not evidence of expert execution. External provider/model fixtures are appropriate for hermetic integration tests; live accounts and installed-machine checks remain separate.

## Delivery

Canonical feature: [review-cli#444](https://github.com/ultrabricks/review-cli/issues/444). Integration epic: [review-cli#499](https://github.com/ultrabricks/review-cli/issues/499). Historical default-selection and pool examples remain linked through #433, #441 and #442.

The earlier generated specification's restrictions to one model, a fixed three-role floor, or no role completion under `--pool 1` are superseded by the clarified M/R contract. The complete previous specification is retained under `docs/grooming/corrections-2026-09-11/preserved/roles-before-correction.md`; none of its completion claims substitutes for this feature's acceptance.
