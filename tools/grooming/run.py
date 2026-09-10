#!/usr/bin/env python3
"""Run the read-only compiler with origin-scoped HTTP credentials.

Artifact redirects use signed storage URLs. Authorization must never be forwarded
from api.github.com to another origin. Build, substantive finalization and HTML
rendering are separate stages; none of them writes to the issue tracker.
"""
from __future__ import annotations
import pathlib
import runpy
import urllib.parse
import urllib.request

class OriginScopedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(redirected.full_url)
        if new.scheme != 'https':
            raise ValueError('Refusing non-HTTPS artifact redirect')
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            for mapping in (redirected.headers, redirected.unredirected_hdrs):
                for key in tuple(mapping):
                    if key.lower() in ('authorization', 'cookie', 'proxy-authorization'):
                        redirected.remove_header(key)
        return redirected

def self_test():
    handler = OriginScopedRedirect()
    req = urllib.request.Request('https://api.github.com/example', headers={'Authorization':'Bearer test-only','Accept':'application/json'})
    other = handler.redirect_request(req,None,302,'Found',{},'https://example.blob.core.windows.net/signed')
    assert not any(k.lower()=='authorization' for k in other.headers)
    same = handler.redirect_request(req,None,302,'Found',{},'https://api.github.com/another')
    assert same.get_header('Authorization') == 'Bearer test-only'
    try:
        handler.redirect_request(req,None,302,'Found',{},'http://example.invalid/')
    except ValueError:
        pass
    else:
        raise AssertionError('HTTP downgrade accepted')

if __name__ == '__main__':
    self_test()
    urllib.request.install_opener(urllib.request.build_opener(OriginScopedRedirect()))
    runpy.run_path('tools/grooming/build.py',run_name='__main__')
    if pathlib.Path('tools/grooming/finalize.py').exists():
        runpy.run_path('tools/grooming/finalize.py',run_name='__main__')
