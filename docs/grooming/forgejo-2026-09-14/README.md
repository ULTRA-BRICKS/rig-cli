# Forgejo restoration checkpoint

The 13 September migration preserved metadata but did not import native issues. The migration archive README explicitly records server-side native migration failure. Archived state and historical grooming counts are not current completion evidence.

## Identity and preservation

`source-map.csv` deterministically enumerates every archived issue in the eleven in-scope repositories, ordered by repository then original number. The source database ID disambiguates issue identity; the author is source attribution only. Empty current URLs mean **not restored**, never unchanged IDs. No current issue has been closed or reopened. draw-cli is excluded from active scope; historical artifacts remain untouched.

`archive-checksums.json` records the SHA-256 of each complete issue archive and comparison with its migration manifest. Originals and comment exports remain under `~/xp/ultrabricks-migration/metadata-archives/<repo>/github/`; no raw private transcripts, credentials or global configuration are copied into this branch.

The optional-task issue at https://git.hyperide.ai/ultrabricks/review-cli/issues/1 belongs to the parallel feature owner and is not a restoration of historical #444. It is not modified here.

## Tracker verification

The issue archives contain **1,631 issues: 909 historically open and 722 historically closed**. All eleven manifest checks pass. Current explicit repository counters report nine empty trackers, one new review-cli issue and one new tg-cli issue. The new issues belong to their feature owners; neither is a migrated source issue. Search and counters were taken sequentially while those owners worked, not as an atomic snapshot.

| Repository | Archived issues | Historically open |
|---|---:|---:|
| agent-tools | 436 | 249 |
| dev-cli | 21 | 6 |
| harness-cli | 21 | 10 |
| pm-cli | 20 | 9 |
| qa-cli | 8 | 1 |
| research-cli | 12 | 3 |
| review-cli | 349 | 216 |
| rig-cli | 360 | 189 |
| stt-cli | 33 | 24 |
| task-cli | 136 | 75 |
| tg-cli | 235 | 127 |

Use explicit host and origin selection, for example:

```sh
fj -H git.hyperide.ai -C ~/xp/review-cli issue -R origin search --state all
fj -H git.hyperide.ai -C ~/xp/review-cli repo view ultrabricks/review-cli
```

Implicit selection in rig-cli produced HTTP 410 in a checkout retaining both `github` and `origin` remotes. Explicit Forgejo selection succeeds. Do not use the historical remote. An unauthenticated API probe returned HTTP 403 and was stopped; no credential extraction or alternate-auth retry was attempted.

Installed fj 0.6.0 exposes no search pagination flags or native dependency/parent commands. Search output must be checked against repository counters; this does not establish pagination correctness for a populated tracker. Bulk restoration is pending a reviewed small pilot and independent read-back, not authorized by an empty result alone.

The locally archived Forgejo API schema does advertise `/repos/{owner}/{repo}/issues/{index}/dependencies` (GET/POST/DELETE) and page/limit parameters on issue listing. This establishes a documented API surface, not a tested authenticated mutation capability. Do not describe native dependencies as absent from Forgejo itself.

## Pilot boundary

`pilot-review-roles.md` is a proposed replacement for historical review-cli#444. It preserves the general M/R requirement and supersedes mandatory task association. It is a draft until review passes, creation returns an identity, and a separate view confirms the exact content. Read the current issue/discussion immediately before edits; preserve concurrent changes. Persist every command result and read-back. Stop on denial or uncertain delivery; never retry creation blindly.

Historical #441/#442/#499 and harness-cli#18 remain unmapped. Harness #18 is a conversation-reader feature; its exact original flags were not recovered. No keyword-derived epic links or bulk dependency edges are created. Closed historical issues require individual acceptance review before any state decision.

The individual exports match the archived issue bodies for review-cli #433/#441/#442/#444/#499 and harness-cli #1/#18. Historical review-cli#515 is a **pull request**, titled “test(review): isolate panel availability in effort propagation coverage,” found in `metadata/review-cli/pulls.json`; it must not be restored as an issue. The two archived comments on #444 reference historical red/green screenshot-text paths; they are preserved locally, not treated as current acceptance proof.

The first staged review failed to obtain required reviewer outcomes: expired Claude authentication and sandbox-denied OMP/OpenCode runtime writes. Its verified wrapper had no child processes and was stopped with SIGTERM (exit 143). No pilot issue was created and no passing staged review, new commit or PR is claimed. Preservation/language checks passed; `python3 -m pytest -q tests/grooming` passed 92 tests. The scope of these tests does not include current closed-task acceptance or publication.

## Publication

The previous publication README points to a GitHub-hosted immutable reader. It is historical publication evidence, not a new Forgejo deployment. The existing reader generator contains historical GitHub links and cannot be published as a current tracker reader unchanged. Current spec-dev infrastructure, protected reading, public navigation, complete documents, no-JS rendering and mobile acceptance remain to be verified. No unrelated service is changed here.

Read-only `review spec-web status` reports the existing daemon running on port 7920 with autostart enabled. `review spec-web list` reports seven registered historical specs, none of the current recovery topic documents. The local navigator returned HTTP 200; the public retrospective URL returned HTTP 403. Neither result proves current-spec deployment. The supported registration interface is `review spec-web add <path>`; registration changes the shared service registry and is left to the publication owner after source and link review.
