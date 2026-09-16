#!/usr/bin/env python3
"""Resume one repository's reviewed v4 plan. Dry-run is the default.

The executable is checked out with the plan at an explicit immutable Git ref.
Publishing a reader and applying issue edits are separate jobs; an unrelated
publication receipt cannot select executable code or attest tracker completion.

No issue is closed/reopened, no parent is replaced, and no global permissions are
changed here. Those operations require their own evidence-reviewed decisions.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, Protocol
import urllib.error
import urllib.parse
import urllib.request

REPOSITORIES = frozenset({
    "rig-cli", "agent-tools", "review-cli", "task-cli", "tg-cli", "qa-cli",
    "draw-cli", "stt-cli", "research-cli", "dev-cli", "pm-cli", "harness-cli",
})
START_V1 = "<!-- rig-grooming:2026-09-10:v1 -->"
END_V1 = "<!-- /rig-grooming:2026-09-10:v1 -->"
START_V4 = "<!-- rig-grooming:v4:2026-09-10 -->"
END_V4 = "<!-- /rig-grooming:v4:2026-09-10 -->"
SCOPE_V1 = "## Original technical scope, acceptance and delivery history"
SCOPE_V4 = "## Сохранённая техническая постановка и история"
SALVAGE = "## Consolidated requirements and retained findings"


class ValidationError(ValueError):
    pass


class AccessDenied(RuntimeError):
    """Authorization failure: stop, do not retry another endpoint/token."""


class DeliveryUncertain(RuntimeError):
    """A write may have succeeded. Read back before a later attempt."""


def digest(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def repository_name(value: str) -> str:
    """A short name is a plan key, not a canonical owner or permission check."""
    parts = value.split("/")
    if len(parts) not in (1, 2) or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", p) for p in parts):
        raise ValidationError("Expected a repository name or owner/name")
    if parts[-1] not in REPOSITORIES:
        raise ValidationError("Repository is outside the reviewed ecosystem scope")
    return parts[-1]


def canonical_repository(requested: str, metadata: dict, expected_id: str | None) -> str:
    full = metadata.get("full_name", metadata.get("repository_full_name"))
    if not isinstance(full, str) or len(full.split("/")) != 2:
        raise ValidationError("GitHub did not return a canonical full repository name")
    if repository_name(full) != repository_name(requested):
        raise ValidationError("Resolved repository has a different name")
    if expected_id is None or str(metadata.get("id")) != str(expected_id):
        raise ValidationError("Stable repository ID must match GITHUB_REPOSITORY_ID")
    return full


def structural_lines(text: str) -> list[tuple[str, int, int]]:
    """Recognize standalone markers outside fenced code and blockquotes."""
    result: list[tuple[str, int, int]] = []
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        value = line.rstrip("\r\n")
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", value)
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= fence[1] and not match[2].strip():
                fence = None
        elif match:
            fence = (match[1][0], len(match[1]))
        elif value and not value.startswith((">", "    ", "\t")):
            result.append((value, offset, offset + len(line)))
        offset += len(line)
    return result


def required_fragments(body: str) -> list[str]:
    """Lossless source fragments a replacement must retain, in any order.

    A generated prefix may be replaced, not notes appended after its closing
    marker. Ambiguous/malformed structures fail instead of guessing a boundary.
    """
    clean = body.lstrip()
    if not clean.startswith((START_V1, START_V4)):
        return [body.strip()] if body.strip() else []
    lines = structural_lines(body)
    start, end = (START_V4, END_V4) if clean.startswith(START_V4) else (START_V1, END_V1)
    ends = [(a, b) for value, a, b in lines if value == end]
    starts = [(a, b) for value, a, b in lines if value == start]
    if len(starts) != 1 or len(ends) != 1 or ends[0][0] <= starts[0][0]:
        raise ValidationError("Malformed or ambiguous managed section; preserve it for manual review")
    close_at, after_close = ends[0]
    if start == START_V4:
        tail = body[after_close:].strip()
        if tail.startswith(SCOPE_V4 + "\n") or tail == SCOPE_V4:
            tail = tail[len(SCOPE_V4):].strip()
        return [tail] if tail else []
    headings = [(a, b) for value, a, b in lines if value == SCOPE_V1 and a < close_at]
    if len(headings) != 1:
        raise ValidationError("Original-scope heading is missing or ambiguous")
    before_scope, scope_start = headings[0]
    fragments = [body[scope_start:close_at].strip(), body[after_close:].strip()]
    salvage = [(a, b) for value, a, b in lines if value == SALVAGE and a < before_scope]
    if len(salvage) > 1:
        raise ValidationError("Ambiguous preserved-findings section")
    if salvage:
        fragments.append(body[salvage[0][1]:before_scope].strip())
    return [s for s in fragments if s]


def validate_plan(rows: Any, repo: str, before: list[dict], quote_ids: set[str]) -> list[dict]:
    repo = repository_name(repo)
    if not isinstance(rows, list) or not isinstance(before, list):
        raise ValidationError("Plan and before-image must be arrays")
    if any(not isinstance(r, dict) or type(r.get("number")) is not int for r in before):
        raise ValidationError("Malformed before-image record")
    snapshots = {r["number"]: r for r in before}
    if len(snapshots) != len(before):
        raise ValidationError("Duplicate before-image numbers")
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValidationError("Malformed plan record")
        number = row.get("number")
        if type(number) is not int or number <= 0 or number in seen:
            raise ValidationError("Invalid or duplicate issue number")
        seen.add(number)
        if row.get("key") != f"{repo}#{number}":
            raise ValidationError("Plan row targets another repository")
        prior = snapshots.get(number)
        if (prior is None or type(row.get("id")) is not int or row["id"] <= 0
                or prior.get("id") != row["id"] or "pull_request" in prior):
            raise ValidationError("Before-image issue identity mismatch")
        if row.get("expected_sha256") != digest(prior.get("body")):
            raise ValidationError("Plan does not match its saved original body")
        if (not isinstance(row.get("title"), str) or not row["title"].strip()
                or row.get("expected_state") not in ("open", "closed")
                or row["title"] != prior.get("title") or row["expected_state"] != prior.get("state")):
            raise ValidationError("Plan metadata differs from its before-image")
        proposed = row.get("body")
        if not isinstance(proposed, str) or not proposed.startswith(START_V4) or len(proposed) > 63000:
            raise ValidationError("Missing, oversized or non-v4 replacement body")
        required_fragments(proposed)
        for fragment in required_fragments(prior.get("body") or ""):
            if fragment not in proposed:
                raise ValidationError(f"{repo}#{number}: replacement loses original scope or appended notes")
        referenced = row.get("record", {}).get("quote_ids", [])
        if not isinstance(referenced, list) or any(q not in quote_ids for q in referenced):
            raise ValidationError("Unresolvable quotation reference")
    return rows


def assess(row: dict, current: dict) -> str:
    if str(row["id"]) != str(current.get("id")) or row["number"] != current.get("number") or "pull_request" in current:
        return "identity_conflict"
    if digest(current.get("body")) == digest(row["body"]):
        if current.get("title") != row["title"] or current.get("state") != row["expected_state"]:
            return "metadata_changed_after_application"
        return "already_exact"
    if (digest(current.get("body")) != row["expected_sha256"] or current.get("title") != row["title"]
            or current.get("state") != row["expected_state"]):
        return "concurrent_edit_preserved"
    return "ready"


class IssueAPI(Protocol):
    def issue(self, number: int) -> dict: ...
    def patch_body(self, number: int, body: str) -> dict: ...


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc) or new.scheme != "https":
            raise AccessDenied("Refusing a credential-bearing cross-origin redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHubAPI:
    def __init__(self, full: str, token: str, interval: float = 10):
        if len(full.split("/")) != 2:
            raise ValidationError("API needs canonical owner/name")
        repository_name(full)
        if interval < 1:
            raise ValidationError("Mutation interval must be at least one second")
        self.full, self.token, self.interval = full, token, interval
        self.last_write = 0.0
        self.opener = urllib.request.build_opener(SameOriginRedirect())

    def request(self, suffix: str = "", *, body: str | None = None) -> dict:
        if suffix and not re.fullmatch(r"/issues/[1-9][0-9]*", suffix):
            raise ValidationError("Only this repository and its issues are addressable")
        if body is not None and not suffix:
            raise ValidationError("Only an issue body may be patched")
        method = "PATCH" if body is not None else "GET"
        if body is not None:
            time.sleep(max(0.0, self.last_write + self.interval - time.monotonic()))
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.full}{suffix}", method=method,
            data=None if body is None else json.dumps({"body": body}).encode(),
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "rig-reviewed-v4-resume"},
        )
        try:
            with self.opener.open(request, timeout=60) as response:
                result = json.load(response)
            if body is not None:
                self.last_write = time.monotonic()
            return result
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                wait = exc.headers.get("Retry-After")
                limited = wait is not None or exc.headers.get("X-RateLimit-Remaining") == "0"
                if limited:
                    raise DeliveryUncertain("Rate-limited; obey Retry-After/reset before resuming") from exc
                raise AccessDenied(f"GitHub returned HTTP {exc.code}; reconnect/authorize before resuming") from exc
            raise DeliveryUncertain(f"GitHub returned HTTP {exc.code}; read back before another write") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise DeliveryUncertain("Request result is unknown; resume only after read-back") from exc

    def issue(self, number: int) -> dict:
        return self.request(f"/issues/{number}")

    def patch_body(self, number: int, body: str) -> dict:
        return self.request(f"/issues/{number}", body=body)


def save_journal(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".v4-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(record, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_plan(api: IssueAPI, rows: list[dict], *, apply: bool, journal: Path,
             max_writes: int = 100, context: dict | None = None) -> dict:
    if max_writes < 1:
        raise ValidationError("max_writes must be positive")
    record = {"schema_version": 1, "source_version": 4, "context": context or {},
              "mode": "apply" if apply else "dry_run", "events": [], "writes": 0,
              "planned": len(rows), "complete": False, "fatal": None,
              "started_at": datetime.now(timezone.utc).isoformat()}
    save_journal(journal, record)
    for row in rows:
        event = {"key": row["key"], "number": row["number"]}
        try:
            current = api.issue(row["number"])
            status = assess(row, current)
            event.update(result=status, observed_body_sha256=digest(current.get("body")))
            if status == "ready" and apply:
                if record["writes"] >= max_writes:
                    event["result"] = "write_budget_reached"
                    record["events"].append(event)
                    break
                record["pending_write"] = {"key": row["key"], "expected_after_sha256": digest(row["body"])}
                save_journal(journal, record)
                changed = api.patch_body(row["number"], row["body"])
                record["writes"] += 1
                if digest(changed.get("body")) != digest(row["body"]):
                    raise DeliveryUncertain("PATCH response body did not match the intended replacement")
                fresh = api.issue(row["number"])
                result = assess(row, fresh)
                event.update(result="updated_and_read_back" if result == "already_exact" else "postwrite_conflict",
                             final_body_sha256=digest(fresh.get("body")))
                record.pop("pending_write", None)
            record["events"].append(event)
        except (AccessDenied, DeliveryUncertain) as exc:
            event["result"] = "authorization_denied" if isinstance(exc, AccessDenied) else "delivery_uncertain"
            record["events"].append(event)
            record["fatal"] = event["result"]
            break
        finally:
            save_journal(journal, record)
    accepted = {"already_exact", "updated_and_read_back"}
    record["verified_exact"] = sum(e["result"] in accepted for e in record["events"])
    record["complete"] = len(rows) == record["verified_exact"] and record["fatal"] is None
    record["remaining_or_conflicted"] = len(rows) - record["verified_exact"]
    record["plan_checked"] = len(record["events"]) == len(rows) and record["fatal"] is None
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    save_journal(journal, record)
    return record


def committed_bytes(root: Path, ref: str, relative: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), "show", f"{ref}:{relative}"], stderr=subprocess.PIPE)


def verify_checkout(root: Path, ref: str) -> None:
    actual = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if actual != ref:
        raise ValidationError("Source checkout does not match the approved immutable reference")
    try:
        relative = Path(__file__).resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValidationError("Runner must be executed from the approved source checkout") from exc
    if Path(__file__).read_bytes() != committed_bytes(root, ref, relative):
        raise ValidationError("Runner differs from the executable at the approved immutable reference")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--apply", action="store_true", help="Explicitly enable body-only writes")
    parser.add_argument("--max-writes", type=int, default=100)
    parser.add_argument("--journal", type=Path, default=Path("grooming-v4-results/resume.json"))
    args = parser.parse_args(argv)
    startup = {"schema_version": 1, "source_version": 4, "mode": "apply" if args.apply else "dry_run",
               "started_at": datetime.now(timezone.utc).isoformat(), "events": [], "writes": 0,
               "complete": False, "phase": "preflight", "source_ref": args.source_ref}
    save_journal(args.journal, startup)
    try:
        if not args.repository or len(args.repository.split("/")) != 2:
            raise ValidationError("--repository must be owner/name")
        if not re.fullmatch(r"[0-9a-f]{40}", args.source_ref):
            raise ValidationError("--source-ref must be an immutable Git commit")
        if args.max_writes < 1:
            raise ValidationError("--max-writes must be positive")
        verify_checkout(args.source_root, args.source_ref)
        repo = repository_name(args.repository)
        data = "docs/grooming/v4-2026-09-10"
        rows = json.loads(committed_bytes(args.source_root, args.source_ref, f"{data}/plans/{repo}.json"))
        before = json.loads(committed_bytes(args.source_root, args.source_ref, f"{data}/before/{repo}.json"))
        registry = json.loads(committed_bytes(args.source_root, args.source_ref, f"{data}/quote-registry.json"))
        validate_plan(rows, repo, before, {q["id"] for q in registry["records"]})
        token = os.environ.get("GH_TOKEN")
        if not token:
            raise AccessDenied("Repository-scoped GH_TOKEN is missing")
        client = GitHubAPI(args.repository, token)
        full = canonical_repository(args.repository, client.request(), os.environ.get("GITHUB_REPOSITORY_ID"))
        client.full = full
    except (ValidationError, AccessDenied, DeliveryUncertain, OSError,
            subprocess.CalledProcessError, KeyError, TypeError, json.JSONDecodeError) as exc:
        startup.update(fatal=type(exc).__name__, finished_at=datetime.now(timezone.utc).isoformat())
        if isinstance(exc, ValidationError):
            startup["validation_reason"] = str(exc)
        save_journal(args.journal, startup)
        print(json.dumps(startup, ensure_ascii=False, indent=2))
        return 2
    result = run_plan(client, rows, apply=args.apply, journal=args.journal, max_writes=args.max_writes,
                      context={"canonical_repository": full, "source_ref": args.source_ref,
                               "plan_sha256": digest(json.dumps(rows, ensure_ascii=False, sort_keys=True)),
                               "product_acceptance": False})
    print(json.dumps({k: v for k, v in result.items() if k != "events"}, ensure_ascii=False, indent=2))
    dry_run_clean = not args.apply and result["plan_checked"] and all(
        event["result"] in {"ready", "already_exact"} for event in result["events"]
    )
    return 0 if result["complete"] or dry_run_clean else 2


if __name__ == "__main__":
    raise SystemExit(main())
