# Review addendum: tg-cli/docs/specs/md-as-pdf.md

Source revision: `b640b65e7b30321ec8c14259896212f1fe78626f`.

This source document is retained with its original draft/approved/skipped status. This addendum does not approve previously skipped work. Root `rig-cli/SPEC.md` describes Evolve, not the entire rig ecosystem.

Current recovery contract: [Auxiliary CLI behavior and end-to-end user workflows](../auxiliary.md). Governing epic: https://github.com/alex-mextner/rig-cli/issues/521.

## Example and revalidation

A CLI is invoked from outside its repository with a valid input, an invalid input and an unavailable optional backend. Successful output, nonzero failure, cancellation and cleanup are separately asserted. A mock backend passing does not establish that a live account, local model or audio device was exercised.

## Source quotations

> Нужно ещё проверить, что это работает на машине, и приложить к отчёту проверяемые доказательства.

Source **Q202**; compiled-memory-attribution; MEMORY.md standing lesson: Merge is not this machine; on disk is not PATH; PDF page(s) 74. Thematic recovery requirement, not retroactive authorship of this entire specification.

<details><summary>Original wording (before editorial correction)</summary>

> надо еще проверить что это на машине работает и в отчете приложить пруфы проверяемые

</details>

> Все доказательства должны быть проверяемыми.

Source **Q028**; compiled-fragment-attribution; rig-zerobug/rig-fable session 2026-09-04..07; agent=Full175Report, ResultReport; PDF page(s) 39. Thematic recovery requirement, not retroactive authorship of this entire specification.

<details><summary>Original wording (before editorial correction)</summary>

> все пруфы должны быть проверяемы

</details>

## Linked acceptance backlog

- [tg-cli#27](https://github.com/alex-mextner/tg-cli/issues/27) — /new command — spawn a new agent session (interactive inline-button options)
- [tg-cli#29](https://github.com/alex-mextner/tg-cli/issues/29) — File-excerpt attachment — quote referenced lines for path:line refs (currently broken)
- [tg-cli#31](https://github.com/alex-mextner/tg-cli/issues/31) — CodeQL JS/TS: fix-or-justify 8 pre-existing TS findings (surfaced by #25 rig rollout)
- [tg-cli#80](https://github.com/alex-mextner/tg-cli/issues/80) — tg VERSION literal (1.15.0) has DRIFTED from package.json (1.0.0) — derive it dynamically
- [tg-cli#95](https://github.com/alex-mextner/tg-cli/issues/95) — tg --file report.html renders RAW HTML tags in PDF instead of formatted output
- [tg-cli#102](https://github.com/alex-mextner/tg-cli/issues/102) — md-pdf: convertMdToPdf lacks the network-blackhole + drops embed-resources hardening that convertHtmlToPdf got in #96
- [tg-cli#103](https://github.com/alex-mextner/tg-cli/issues/103) — pdf-render: Chrome still loads absolute-path / file:// <img> subresources into the printed PDF (both md-pdf and html-report paths)
- [tg-cli#134](https://github.com/alex-mextner/tg-cli/issues/134) — tg replies: multi-id sends (split/album) show duplicate lines in list/find
- [tg-cli#136](https://github.com/alex-mextner/tg-cli/issues/136) — tg replies history.jsonl: outbound writer has no RMW lock (unlike routes.json)
- [tg-cli#138](https://github.com/alex-mextner/tg-cli/issues/138) — --title should refuse a tg#<id> reference
- [tg-cli#140](https://github.com/alex-mextner/tg-cli/issues/140) — pre-send-photo hook: forward outgoing caption as --intent to review visual
- [tg-cli#142](https://github.com/alex-mextner/tg-cli/issues/142) — pre-send-photo: _safe_intent() silently drops an unencodable caption without warn()
- [tg-cli#160](https://github.com/alex-mextner/tg-cli/issues/160) — Fix tg voice transcript confirmation and reactions
- [tg-cli#163](https://github.com/alex-mextner/tg-cli/issues/163) — pre-send-photo: selection-highlight veto downgraded to WARN, not BLOCK
- [tg-cli#171](https://github.com/alex-mextner/tg-cli/issues/171) — Auto-continue transient API overload stops
- [tg-cli#188](https://github.com/alex-mextner/tg-cli/issues/188) — tg: hard-block stray CJK stuck mid-word into Latin/Cyrillic text
- [tg-cli#191](https://github.com/alex-mextner/tg-cli/issues/191) — tg replies: agent-scoped history (attribution + default/--agent/--all)
- [tg-cli#205](https://github.com/alex-mextner/tg-cli/issues/205) — tg: block mixed-script homoglyph/mojibake garbage tokens (почčesна)
- [tg-cli#210](https://github.com/alex-mextner/tg-cli/issues/210) — tg-cli: skip fenced code blocks when auto-converting markdown pipe tables
- [tg-cli#215](https://github.com/alex-mextner/tg-cli/issues/215) — tg: don't hard-block Cyrillic-affixed acronyms like antiDDoS-style mixed-script tech terms
- [tg-cli#216](https://github.com/alex-mextner/tg-cli/issues/216) — tg: make escalation-format section validation actually section-aware
- [tg-cli#217](https://github.com/alex-mextner/tg-cli/issues/217) — tg: require a real file:line, not just a path, in the escalation-format where-to-look field
- [tg-cli#221](https://github.com/alex-mextner/tg-cli/issues/221) — tg: harden pipe-table rendering for ragged rows and tighten the where-to-look file reference check
- [tg-cli#223](https://github.com/alex-mextner/tg-cli/issues/223) — tg: escalation-format validator structure checks have false-positive and false-negative edge cases
- [tg-cli#225](https://github.com/alex-mextner/tg-cli/issues/225) — tg: report the mixed-script guard's offending-token position against the authored input, not the rendered table
- [tg-cli#226](https://github.com/alex-mextner/tg-cli/issues/226) — tg: mixed-script guard hard-blocks common Russian dev slang like zapushil, zadeployil (Cyrillic prefix plus English verb stem plus Cyrillic suffix)
- [tg-cli#227](https://github.com/alex-mextner/tg-cli/issues/227) — tg: mixed-script guard is bypassable via invisible format characters splitting a run
- [tg-cli#228](https://github.com/alex-mextner/tg-cli/issues/228) — transmit() preflight refusals (rich-HTML / flood-cap) leak pdfTempDirs via process.exit
- [tg-cli#237](https://github.com/alex-mextner/tg-cli/issues/237) — Add offline dry-run validation for tg escalation-format gate
- [tg-cli#246](https://github.com/alex-mextner/tg-cli/issues/246) — tg --detect-model misbrands omp sessions as claude (CLAUDECODE env checked before ancestry)
- [tg-cli#248](https://github.com/alex-mextner/tg-cli/issues/248) — Review this uncommitted git diff for bugs, regressions, security issues,
- [tg-cli#249](https://github.com/alex-mextner/tg-cli/issues/249) — Review OMP model-detection ordering diff
- [tg-cli#251](https://github.com/alex-mextner/tg-cli/issues/251) — Review omp model-detection ordering diff for cross-file consistency
- [tg-cli#252](https://github.com/alex-mextner/tg-cli/issues/252) — Review OMP detection ordering for cross-file consistency
- [tg-cli#253](https://github.com/alex-mextner/tg-cli/issues/253) — Review OMP lineage-first model detection diff
- [tg-cli#254](https://github.com/alex-mextner/tg-cli/issues/254) — Review OMP model-detection order diff
- [tg-cli#255](https://github.com/alex-mextner/tg-cli/issues/255) — Read-only review of the current uncommitted tg-cli diff for bugs, regres
- [tg-cli#256](https://github.com/alex-mextner/tg-cli/issues/256) — Review OMP model detection ordering
- [tg-cli#257](https://github.com/alex-mextner/tg-cli/issues/257) — Review OMP model-detection ordering diff
- [tg-cli#258](https://github.com/alex-mextner/tg-cli/issues/258) — Review OMP model-detection ordering diff
- [tg-cli#259](https://github.com/alex-mextner/tg-cli/issues/259) — Escalation gate hasWallOfText measures RAW line length incl. HTML markup — any single-line HTML body >350 chars is deny-by-default blocked
- [tg-cli#263](https://github.com/alex-mextner/tg-cli/issues/263) — harness-event mislabels bare StopFailure notifications with stale tmux window name instead of live harness kind
- [tg-cli#270](https://github.com/alex-mextner/tg-cli/issues/270) — escalation-format: withVisualLineBreaks is markup-context-blind (breaks inside <pre>/<code>) and its block-tag set is incomplete
- [tg-cli#284](https://github.com/alex-mextner/tg-cli/issues/284) — Review last-user-target rename for cross-file consistency
- [tg-cli#314](https://github.com/alex-mextner/tg-cli/issues/314) — Review Codex rig hook dedup diff
- [tg-cli#319](https://github.com/alex-mextner/tg-cli/issues/319) — Review omp non-tmux diff
- [tg-cli#336](https://github.com/alex-mextner/tg-cli/issues/336) — Audit ACP-backed omp routing for unsafe delivery paths
- [tg-cli#353](https://github.com/alex-mextner/tg-cli/issues/353) — ACP session/prompt stays wedged forever after one failed prompt (post-#346)
- [tg-cli#357](https://github.com/alex-mextner/tg-cli/issues/357) — Delivery isolation: preserve per-target ordering without waiting for another agent's response

## Preserved specification

# tg-cli — md-as-pdf feature spec

Module: `features/md-pdf/convert.ts`. ON by default. Toggle: `--no-feature md-as-pdf` or
`features: { md-as-pdf: false }` in `~/.config/tg-cli/config.yaml`.

## North star

`.md` / `.markdown` files attached from disk are converted to PDF before upload. Telegram's
native Markdown preview is unreliable for rich documents (tables, fenced code, formatting);
a PDF renders correctly on every client and is immediately readable without a viewer app. The
conversion is silent on success and non-blocking on failure. Local images referenced by the
markdown are deliberately NOT embedded into the PDF — see [Security](#security).

## Pipeline

1. Detect: attachment is a disk-sourced `.md` or `.markdown` file (auto-detected path or
   explicit `--file`).
2. **Pandoc pass** — convert to self-contained HTML5:
   ```
   pandoc --from=gfm --to=html5 --standalone <input.md> -o <tmp.html>
   ```
3. **Chrome print pass** — render HTML to PDF:
   ```
   <chrome> --headless=new --no-pdf-header-footer \
            --host-resolver-rules="MAP * ~NOTFOUND" \
            --virtual-time-budget=2000 \
            --print-to-pdf=<tmp.pdf> \
            "file://<percent-encoded-absolute-path-to-tmp.html>"
   ```
   The print runs through the shared `printToPdf` helper, the single source of the
   Chrome print flags for both this path and the code-as-pdf / html-report paths
   (`features/code-pdf/convert.ts`), so they cannot drift on the print sandbox. See
   [Security](#security).
4. Upload the resulting PDF in place of the original `.md`. The filename shown in Telegram
   is `<original-basename>.pdf`.
5. On any failure (see below) — upload the original `.md` unchanged and emit a one-line
   warning to stderr. The send is never blocked.

### file:// URL encoding requirement

The path passed to Chrome must be a valid `file://` URL. Non-ASCII characters in the
absolute path (e.g. Cyrillic or CJK directory names) must be percent-encoded
(`encodeURIComponent` per path segment, `/` separators preserved). Passing a raw path with
non-ASCII bytes causes Chrome to silently produce a blank PDF.

### Chrome binary discovery

Checked in order:
1. `TG_CHROME_PATH` environment variable (absolute path).
2. `google-chrome`, `google-chrome-stable`, `chromium`, `chromium-browser` on `PATH`.
3. macOS default locations: `/Applications/Google Chrome.app/…/MacOS/Google Chrome`,
   `/Applications/Chromium.app/…/MacOS/Chromium`.

If no binary is found, the feature degrades to uploading the original `.md`.

## Scope: disk-only; memory-sourced markdown excluded

The conversion applies **only** to disk files (R1 / explicit `--file`). It does NOT apply
to:
- **R4 fragment files** (large inline code blocks extracted from the message text into
  in-memory blobs). These fragments are not standalone documents; converting them to PDF
  would wrap a raw code excerpt in a document shell and hide any marker comments injected
  for line-spec navigation.
- **Line-spec marker copies** (in-memory copies of source files with injected marker-comment
  bands). Same reason: the marker comments are meaningful only as plain text; they would be
  invisible or misleading in PDF form.
- Any `{filename, content}` in-memory `SendItem`, regardless of extension.

The distinction is: if the original source is a real file the user deliberately referenced
or attached, convert it. If the content was assembled in memory by tg-cli itself, do not.

## Failure policy

Any of the following causes a silent fallback (original `.md` uploaded, stderr warning):
- `pandoc` not found or exits non-zero.
- Chrome binary not found or exits non-zero.
- Output PDF is absent or zero bytes after Chrome exits.
- Any unhandled exception during the pipeline.

The warning line format: `[md-as-pdf] <reason>: falling back to .md upload`.

Temp files (`<tmp>.html`, `<tmp>.pdf`) are created in the OS temp directory and deleted in
a `finally` block regardless of success or failure. On unclean exit (process kill), the OS
cleans them up on the next reboot in the normal course.

## Security

The PDF is rendered from user-supplied markdown and then uploaded to Telegram, so the
render must not be a network-egress or local-file-disclosure channel (issue tg-cli#102 —
parity with the `.html` report path, tg-cli#95/#96):

- **Network blackhole.** The Chrome print runs with
  `--host-resolver-rules="MAP * ~NOTFOUND"`, which fails every DNS lookup. A markdown
  document with a remote resource (`![x](http://host/p.png)`, a remote font/iframe) cannot
  resolve a host at print time, so it cannot beacon out (tracking pixel / SSRF / egress).
  Scope: this blocks remote HOST NAMES; an IP-literal host may skip the resolver and a
  `file://` subresource needs no DNS — but there is no return channel either way.
- **No pandoc-stage local-file inlining.** pandoc is invoked WITHOUT `--embed-resources` /
  `--resource-path`. `--embed-resources` base64-inlines any local file the markdown
  references (`![x](/etc/passwd)`, `![x](../secret)`) into the HTML it produces — with zero
  attacker effort, for EVERY referenced path, relative or absolute — and fetches remote
  `src=`s at the pandoc stage. Dropping it closes both. The cost: a relative LOCAL image in
  the markdown shows broken in the PDF (remote images are blackholed regardless). A report
  is text-formatting, not an image document.

This closes the two surfaces above but is **not full parity** with the `.html` report path.
Two residuals remain, both tracked:

- **tg-cli#103 (shared with the `.html` path):** removing `--embed-resources` stops the
  pandoc-stage inlining, but Chrome itself — printing the `file://` document — still loads
  an ABSOLUTE-path or `file:`-scheme subresource (`<img src="file:///abs/secret.png">`) and
  prints it into the PDF; the DNS blackhole does not apply to `file://`. A relative ref is
  safe (it resolves into the temp dir, where the file isn't).
- **tg-cli#104 (md path only):** unlike the `.html` path — which runs `sanitizeReportHtml`
  + `stripEventHandlerAttrs` to strip `<script>`/`<iframe>`/`<object>`/`<embed>`/`<svg>` and
  `on*=` handlers before the print — `convertMdToPdf` does NOT sanitize pandoc's output.
  pandoc keeps gfm `raw_html` on, so a hostile `.md` with a raw `<iframe src="file://…">`
  (arbitrary local TEXT disclosure, broader than the `<img>` residual) or `<script>` reaches
  Chrome. Bringing the md path to the `.html` path's sanitization is tracked as tg-cli#104.

Both the md-pdf path and the code-pdf/html-report paths share one print helper
(`printToPdf` in `features/md-pdf/convert.ts`), so the print sandbox flags are defined in a
single place and the paths cannot silently diverge.

## Dependencies

- **pandoc** — must be on `PATH`. Install: `brew install pandoc` (macOS) /
  `apt install pandoc` (Debian/Ubuntu). No version constraint beyond GFM support
  (available since pandoc 2.x).
- **Headless Chrome** — Chromium or Google Chrome, version 112+ (when `--headless=new` was
  stabilised). Discovered automatically; override with `TG_CHROME_PATH`.
- **Emoji + Cyrillic rendering** — relies on the system font stack. No special font
  installation required on macOS or a standard Linux desktop. Headless server environments
  without font packages may need `fonts-noto` or similar.

## Non-goals

- No LaTeX / XeLaTeX pipeline. Pandoc's `--pdf-engine=xelatex` is not used — too heavy a
  dependency for a CLI tool.
- No multi-page layout tuning (margins, page size, CSS print media). Default Chrome print
  output is acceptable.
- No conversion of in-memory fragment blobs or marker copies (see Scope section).
- No re-encoding or compression of the output PDF.
- No conversion of formats other than `.md` / `.markdown`.
