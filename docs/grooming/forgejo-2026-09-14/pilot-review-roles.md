## Feature

Execute any R required roles using any M eligible models in every model-executing review mode. Each role requires a separate invocation, its role-specific prompt and its actual outcome. A model can fill several roles; copying an answer, retrying, moderating or synthesizing does not add completed expert roles.

## User request

> «Любое M доступных моделей должно уметь заполнять любое R требуемых ролей».

Source: historical review-cli#444, clarification recorded 11 September 2026. Original issue author: alex-mextner. This restoration attributes the archived source; it does not recreate authorship or timestamps.

## Expected behavior

Use the same role contract for defaults, explicit model lists, boards and presets across diff, direct answers, quorum, brainstorm, visual and testing modes. Preserve each mode's purpose. Model eligibility, required roles, execution routes, concurrency and task association are independent controls.

Task association is optional. Every run retains a stable identity and its evidence; a task may be attached after execution without rerunning models or changing required roles. The archived mandatory-task paragraph is superseded by the 14 September 2026 request.

## Acceptance

- [ ] Cover M<R, M=R and M>R, including zero, one and two eligible models and varied valid role sets.
- [ ] Every completed role has a real invocation, the correct prompt and persisted outcome in every execution mode.
- [ ] Failed or unsupported roles remain explicitly incomplete; zero eligible routes cannot produce successful coverage.
- [ ] Defaults, explicit selection, boards, presets and sequential/concurrent execution preserve the same required role set.
- [ ] CLI/library tests exercise real handlers and evidence consumers, with successful and failed-role controls; fixtures replace external providers only.
- [ ] Omitting a task leaves role planning unchanged; later association preserves run identity and outcomes.
- [ ] Help, configuration and reports distinguish model diversity from completed roles.

## Links

- Current optional-task feature: https://git.hyperide.ai/ultrabricks/review-cli/issues/1
- Versioned specification: `docs/specs/recovery/roles.md` in the rig-cli documentation branch.
- Source identity: ultrabricks/review-cli#444, preserved in the 13 September migration archive. Historical epic #499 and cases #433/#441/#442 have no verified current IDs yet; no native dependency is claimed.

Historical state: open. Current acceptance has not been verified. Earlier real-handler observations are preserved in `docs/grooming/corrections-2026-09-11/review-role-observations.md`; they concern their recorded revision, not current product completion.
