"""Regression tests for live Forgejo auth and conservative PR lookup."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from riglib import forgejo_prs as f
from test_forgejo_prs import T, _pull


def test_oauth_uses_bearer_scheme(tmp_path, monkeypatch):
    key = tmp_path / 'keys.json'
    key.write_text(json.dumps({'hosts': {'git.hyperide.ai': {'type': 'OAuth', 'token': 'synthetic'}}}))
    monkeypatch.setattr(f, '_fj_key_files', lambda: [key])
    monkeypatch.setattr(f.shutil, 'which', lambda name: None)
    monkeypatch.delenv('FORGEJO_TOKEN', raising=False)
    assert f._auth_headers(T.origin)['Authorization'] == 'Bearer synthetic'


def test_https_remote_preserves_non_default_port():
    target = f.forgejo_repo_from_remote('https://git.hyperide.ai:8443/ultrabricks/rig-cli.git')
    assert target.origin == 'https://git.hyperide.ai:8443'


@pytest.mark.parametrize('change', [{'state': 'unknown'}, {'head': 'malformed'}])
def test_malformed_pull_never_looks_closed(change):
    row = _pull(1)
    row.update(change)
    with pytest.raises(f.ForgeLookupError):
        f.normalize_pull(row)


def test_server_page_cap_does_not_truncate_and_auth_resolves_once(monkeypatch):
    auth_calls = []
    def headers(origin):
        auth_calls.append(origin)
        return {}
    monkeypatch.setattr(f, '_auth_headers', headers)
    pages = {1: [_pull(1)], 2: [_pull(2)]}
    def get(url, headers, timeout):
        page = int(url.split('page=')[1].split('&')[0])
        return 200, json.dumps(pages.get(page, [])), url
    result = f.fetch_pulls(T, state='all', limit=50, http_get=get)
    assert [row['number'] for row in result] == [1, 2]
    assert auth_calls == [T.origin]


def test_http_redirect_is_refused_before_forwarding_credentials():
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/source':
                self.send_response(302)
                self.send_header('Location', f'http://localhost:{self.server.server_port}/sink')
            else:
                received.append(self.headers.get('Authorization'))
                self.send_response(200)
            self.end_headers()
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}/source'
        status, _, _ = f._default_http_get(url, {'Authorization': 'token synthetic'}, 2)
        assert received == [], 'Authorization must not reach a redirected origin'
        assert status == 302
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_api_path_segments_cannot_inject_query(monkeypatch):
    monkeypatch.setattr(f, '_auth_headers', lambda origin: {})
    target = f.ForgejoRepo(T.origin, 'owner?bad=1', 'repo#other')
    urls = []
    def get(url, headers, timeout):
        urls.append(url)
        return 200, '[]', url
    f.fetch_pulls(target, state='all', limit=1, http_get=get)
    assert '/owner%3Fbad%3D1/repo%23other/pulls?' in urls[0]
