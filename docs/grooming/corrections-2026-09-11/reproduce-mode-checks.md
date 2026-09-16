# Reproduce the real-mode and evidence-reader observations

These characterize current behavior; they do not certify implementation of the general M-model/R-role feature.

## Source and execution

Use review-cli revision `5430f9361468bf9bd1a66a4d4be8198024f989ba` and the three scripts in `tools/verification/`. The producer verifies a SHA-256 manifest of all 74 Python product files under `reviewlib` before labelling its result with that revision. Run each output collection in a new directory.

```sh
python tools/verification/run_review_mode_matrix.py \
  --source /path/to/review-cli --out ./mode-observations
python tools/verification/check_review_mode_evidence.py \
  --source /path/to/review-cli --matrix ./mode-observations --out ./evidence-checks
```

The first command executes 56 cases through real CLI parsing, model selection, mode handlers, prompt builders, scheduling, round loops, moderators and statistics writing. Provider availability and the model-response boundary are fixtures; unavailable models never receive successful fake responses.

The second command runs the real `bin/review task 444 --check --min-iter 1 --min-roles 3 --json` against the generated files, with the same repository path and with normal identity verification enabled. Neither its reader nor its success predicate is replaced. Both runs completed with zero experiment errors. Expected negative product outcomes remain negative rows, not harness errors or passing feature tests.

## Precise result

In the one-model default-selection case, diff executes and records three named review roles; the real evidence reader accepts them. Just-ask performs one generic answer, quorum performs one expert persona plus moderation, and brainstorm performs three expert personas per round. The latter three do not write a named `roles` field in their default-path statistics, and the evidence reader credits zero named review roles and rejects the requested three-role coverage.

Thus **zero credited named roles is an evidence/consistency observation, not zero executed personas**. Explicit single-model selection produces named role coverage, whereas explicit two-model paths differ again. Task identity supplied by flag or environment does not change the observed role behavior. Omitting both forms stops the four text CLI commands before execution.

A deliberately failed correctness role is rejected by the three-role evidence check in all four modes, including brainstorm's otherwise successful partial-result synthesis. No identity-check bypass was used.

## Limits and artifacts

`mode-observations.csv` contains every case's real command outcome, expert/moderator/synthesis counts, observed prompt roles/personas and evidence-reader outcome. Counts include failed attempts; they are not all successful expert reviews. The output bundle retains individual prompts, logs and structured results from synthetic inputs.

The fixture declares zero, one, two or four available model IDs. A default roster can select a subset, so four available IDs must not be reported as four actual default-model invocations. Pool 1/3/5 observations do not establish support for arbitrary user-selected R. Configured boards, all role sets, visual/QA combinations and live-provider quality remain separate acceptance cases.

Environment: isolated HOME/XDG directories, Python 3.13.5, no live accounts. These probe scripts do not depend on pytest; the separate repository-suite run used pytest 9.0.2, outside the project's declared >=8,<9 range, and is reported separately.
