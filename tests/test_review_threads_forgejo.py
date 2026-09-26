"""ci/review-threads/review-threads.sh — the Forgejo provider against a local fake Forgejo API.

The gate must fail closed: an unresolved conversation anywhere on the PR blocks the merge. These
tests drive the real script (under bash, as the workflow runs it) against a stdlib HTTP server
that mimics the Forgejo REST v1 review endpoints.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "ci" / "review-threads" / "review-threads.sh"
REPO = "acme/widget"
PR = 7

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="the gate runs under bash and needs python3",
)


def _comment(cid: int, *, review_id, resolved: bool, path: str = "a.py", position: int = 5) -> dict:
    return {
        "id": cid,
        "pull_request_review_id": review_id,
        "original_commit_id": "c0ffee",
        "commit_id": "c0ffee",
        "path": path,
        "position": position,
        "original_position": position,
        "resolver": {"login": "alex"} if resolved else None,
    }


def _serve(reviews: list[dict], comments_by_review: dict[int, list[dict]]):
    prefix = f"/api/v1/repos/{REPO}/pulls/{PR}/reviews"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _send(self, payload, total=None):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if total is not None:
                self.send_header("X-Total-Count", str(total))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == prefix:
                query = dict(p.split("=", 1) for p in urlsplit(self.path).query.split("&") if "=" in p)
                page, limit = int(query.get("page", 1)), int(query.get("limit", 50))
                self._send(reviews[(page - 1) * limit: page * limit], total=len(reviews))
                return
            if path.startswith(prefix + "/") and path.endswith("/comments"):
                rid = int(path[len(prefix) + 1: -len("/comments")])
                self._send(comments_by_review.get(rid, []))
                return
            self.send_response(404)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _run_gate(server) -> subprocess.CompletedProcess:
    host, port = server.server_address[:2]
    env = {
        k: v for k, v in os.environ.items()
        if k not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_REPO", "GITHUB_API_URL", "REVIEW_THREADS_PROVIDER"}
    }
    env.update(
        GITHUB_SERVER_URL=f"http://{host}:{port}",
        GITHUB_API_URL=f"http://{host}:{port}/api/v1",
        GITHUB_REPOSITORY=REPO,
    )
    return subprocess.run(
        ["bash", str(SCRIPT), str(PR)], env=env, capture_output=True, text=True, timeout=60
    )


@pytest.fixture
def gate():
    servers = []

    def run(reviews, comments_by_review):
        server = _serve(reviews, comments_by_review)
        servers.append(server)
        return _run_gate(server)

    yield run
    for server in servers:
        server.shutdown()


def test_same_line_comments_across_reviews_are_one_conversation_resolved_by_its_first(gate):
    # Forgejo groups every comment on one path + line into ONE conversation, whatever review it
    # came with, and resolves it through its first comment -- so a later comment on an already
    # resolved line sits inside that resolved conversation, as the PR page shows it.
    result = gate(
        [{"id": 11}, {"id": 12}],
        {
            11: [_comment(101, review_id=None, resolved=True)],
            12: [_comment(102, review_id=None, resolved=False)],
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_unresolved_conversation_on_another_line_of_a_later_review_blocks(gate):
    result = gate(
        [{"id": 11}, {"id": 12}],
        {
            11: [_comment(101, review_id=11, resolved=True)],
            12: [_comment(102, review_id=12, resolved=False, position=9)],
        },
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 unresolved review thread" in result.stderr


def test_all_conversations_resolved_passes(gate):
    result = gate(
        [{"id": 11}, {"id": 12}],
        {
            11: [_comment(101, review_id=11, resolved=True)],
            12: [_comment(102, review_id=12, resolved=True, path="b.py")],
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_reply_in_a_resolved_conversation_does_not_reopen_it(gate):
    # Forgejo resolves a conversation through its first comment; replies carry no resolver.
    result = gate(
        [{"id": 11}],
        {11: [_comment(101, review_id=11, resolved=True), _comment(103, review_id=11, resolved=False)]},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_reviews_are_read_across_pages(gate):
    # 60 reviews span two pages of 50; the unresolved conversation lives on the second page.
    reviews = [{"id": 1000 + i} for i in range(60)]
    comments = {1000 + i: [_comment(5000 + i, review_id=1000 + i, resolved=True, path=f"f{i}.py")] for i in range(59)}
    comments[1059] = [_comment(5059, review_id=1059, resolved=False, path="last.py")]
    result = gate(reviews, comments)
    assert result.returncode == 1, result.stdout + result.stderr


def test_api_error_fails_closed(gate):
    result = gate([{"id": "not-an-int"}], {})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "failing closed" in result.stderr
