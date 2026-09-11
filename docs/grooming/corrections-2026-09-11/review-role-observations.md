# Review role coverage: corrected observations

Source under test: `ultrabricks/review-cli@5430f9361468bf9bd1a66a4d4be8198024f989ba` (the current main read on 11 September 2026).

## Correction to the previous report

The old `default_panel_roles.py` replaced the entire mode handler with a function that returned zero. Its assertion read `ctx.extra['task_roles']`, not the prompts or results of actual model invocations. It could demonstrate a missing dispatch field, but **could not prove that all three modes executed zero roles or completed successfully**.

Do not use that old observation as end-to-end acceptance evidence. The general requirement remains independent assignment of M available models to R required roles; a particular dispatch field is an implementation detail.

## New experiment

56 isolated observations exercise the real argument parser, configuration loading, model selection, mode handlers, prompt builders, thread scheduler, round loop, moderator, and statistics writer. The only substitutes are provider availability and the product's existing `REVIEW_FAKE_BACKEND` model-response leaf. A wrapper records every outgoing prompt before returning the fixture response. Each process has a new HOME, XDG directories, log directory and statistics file.

Cases cover four text execution modes; default and explicit model selection; zero, one, two and four entries in the available-provider fixture; task code supplied by flag, environment or omitted; explicit pool values 1, 3 and 5; and a permanently failing role. These are observations of the current implementation, **not 56 passing tests of the complete desired M/R contract**. The declared provider inventory and the subset selected by a mode are recorded separately; availability of four configured model IDs does not mean the default roster contains all four.

## One available named model, default selection

Only `oc:xai/grok-4.6` is available in the fixture. No `-m` is supplied. All four real text handlers complete with the usable fixture responses.

| Mode | Actual expert invocations | Actual prompt roles/personas | Persisted named review roles |
|---|---:|---|---|
| diff | 3 | security, correctness, performance | The same three roles |
| just-ask | 1 | Generic answer prompt; no named review lens | `roles` field absent |
| quorum | 1, plus 1 moderator | One expert persona: Pragmatic staff engineer | `roles` field absent |
| brainstorm | 3 per round; 9 across 3 rounds, plus 3 moderators and 1 synthesis | Three distinct rotating personas in each round; six persona names across the run | `roles` field absent |

Consequently, **"zero roles in just-ask, quorum and brainstorm" was false as a statement about execution**. The observed inconsistency concerns role assignment and evidence across paths: quorum does create a persona, brainstorm already reuses a model across personas, but neither default path records the corresponding named review-role coverage.

With two explicitly selected available models and no configured board, diff and just-ask issue two generic expert calls; quorum issues two persona calls; brainstorm still issues three persona calls per round. Changing `--pool` to 1, 3 or 5 does not turn those explicit two-model paths into a common independently specified R-role plan.

## Task identity

For diff, just-ask, quorum and brainstorm, omitting both `--task` and `REVIEW_TASK_CODE` returns **2 before any model call**. Supplying the same code through the flag or environment produces the same observed default-selection role behavior. Task identity is a normal requirement of these text commands, not a special scope restriction on the feature.

Standalone visual review and management commands have different argument contracts. No visual or QA execution result is claimed by this text-mode matrix.

## Negative controls

The model leaf rejects every model outside the availability fixture. With zero available models, the commands fail rather than receiving successful fake responses from supposedly unavailable providers.

When the correctness role is made permanently unsuccessful, diff records only security/performance as usable coverage; just-ask and quorum return nonzero; brainstorm retains the two successful roles and can complete its partial-result synthesis. A `roles` list, a successful synthesis, and complete required-role coverage are different facts. Consumers must verify the relevant result rather than treating a metadata list as sufficient proof.

## Test relevance

Handler-stub tests remain useful for checking argument routing; they must be labelled as routing tests. They are not substitutes for the leaf-only experiments above. Positive and negative assertions should inspect real generated prompts, real invocation outcomes, and the persisted evidence read by the product's consumers, without copying the scheduler into the test.

Canonical feature: [review-cli#444](https://github.com/ultrabricks/review-cli/issues/444). The complete M/R acceptance matrix, visual/QA integration and provider-account checks are separate from the observations documented here.
