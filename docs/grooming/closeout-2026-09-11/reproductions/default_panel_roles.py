"""RETIRED: this was a dispatch-field probe, not a mode-execution test.

The original replaced each mode handler with a stub that returned zero and read
ctx.extra['task_roles']. It could not establish the number of executed personas,
actual exit status, or persisted role coverage. The earlier interpretation was
incorrect; quorum and brainstorm do execute personas on their default paths.

The complete old probe is preserved, not silently rewritten into a passing test:
    docs/grooming/corrections-2026-09-11/preserved/default-panel-handler-stub.py.txt

Use the real-handler and real-evidence-reader experiments instead:
    tools/verification/run_review_mode_matrix.py
    tools/verification/check_review_mode_evidence.py

Results and limits:
    docs/grooming/corrections-2026-09-11/review-role-observations.md
    docs/grooming/corrections-2026-09-11/mode-observations.csv
"""

if __name__ == '__main__':
    raise SystemExit('Retired dispatch-only probe; run the real-handler experiment documented above.')
