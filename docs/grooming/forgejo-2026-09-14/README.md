# Forgejo restoration checkpoint

The 13 September migration preserved metadata but did not import native issues. The migration archive README explicitly records server-side native migration failure. Archived state and historical grooming counts are not current completion evidence.

## Identity and preservation

`source-map.csv` deterministically enumerates every archived issue in the eleven in-scope repositories, ordered by repository then original number. The source database ID disambiguates issue identity; the author is source attribution only. Empty current URLs mean **not restored**, never unchanged IDs. No current issue has been closed or reopened. draw-cli is excluded from active scope; historical artifacts remain untouched.

`archive-checksums.json` records the SHA-256 of each complete issue archive and comparison with its migration manifest. Originals and comment exports remain under `~/xp/ultrabricks-migration/metadata-archives/<repo>/github/`; no raw private transcripts, credentials or global configuration are copied into this branch.

The optional-task issue at https://git.hyperide.ai/ultrabricks/review-cli/issues/1 belongs to the parallel feature owner and is not a restoration of historical #444. It is not modified here.

## Tracker verification

Use explicit host and origin selection, for example:

```sh
fj -H git.hyperide.ai -C ~/xp/review-cli issue -R origin search --state all
fj -H git.hyperide.ai -C ~/xp/review-cli repo view ultrabricks/review-cli
```

Implicit selection in rig-cli produced HTTP 410 in a checkout retaining both `github` and `origin` remotes. Explicit Forgejo selection succeeds. Do not use the historical remote. An unauthenticated API probe returned HTTP 403 and was stopped; no credential extraction or alternate-auth retry was attempted.

Installed fj 0.6.0 exposes no search pagination flags or native dependency/parent commands. Search output must be checked against repository counters; this does not establish pagination correctness for a populated tracker. Bulk restoration is pending a reviewed small pilot and independent read-back, not authorized by an empty result alone.

## Pilot boundary

`pilot-review-roles.md` is a proposed replacement for historical review-cli#444. It preserves the general M/R requirement and supersedes mandatory task association. It is a draft until review passes, creation returns an identity, and a separate view confirms the exact content. Read the current issue/discussion immediately before edits; preserve concurrent changes. Persist every command result and read-back. Stop on denial or uncertain delivery; never retry creation blindly.

Historical #441/#442/#499 and harness-cli#18 remain unmapped. Harness #18 is a conversation-reader feature; its exact original flags were not recovered. No keyword-derived epic links or bulk dependency edges are created. Closed historical issues require individual acceptance review before any state decision.

## Publication

The previous publication README points to a GitHub-hosted immutable reader. It is historical publication evidence, not a new Forgejo deployment. The existing reader generator contains historical GitHub links and cannot be published as a current tracker reader unchanged. Current spec-dev infrastructure, protected reading, public navigation, complete documents, no-JS rendering and mobile acceptance remain to be verified. No unrelated service is changed here.
