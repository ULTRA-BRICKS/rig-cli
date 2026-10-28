"""config-web request guards and the shared interface constants (rig-cli#310).

Split out of :mod:`riglib.config_web` so the localhost-only CSRF / DNS-rebinding policy lives in one
small, independently reviewable module. Everything here is pure (no I/O, no ``http.server``), which
is also why the tests can drive the guards with a plain dict of headers. :mod:`riglib.config_web`
re-exports every public name, so ``config_web.is_cross_site_write`` etc. keep working.
"""

from __future__ import annotations

from typing import Any

# The interface (host) is shared by the HTTP server and the tests, so it is defined once.
# Localhost-only by invariant — see the :mod:`riglib.config_web` docstring.
HOST = "127.0.0.1"
DEFAULT_PORT = 8787

# Cap any JSON POST body (rig-cli#310: shared by /edit, /api/apply — a single {key,value} edit or
# an apply's {scope,fingerprint,skip_keys} is tiny, well under 1 KiB even with dozens of skip
# keys). Binding to localhost does NOT stop a malicious page the user visits from POSTing to us
# (the browser will), so an unbounded `rfile.read(Content-Length)` is a trivial memory-DoS vector
# — reject anything implausibly large rather than allocate it. 64 KiB is generous for the
# legitimate payload. Kept as MAX_EDIT_BODY_BYTES (not renamed) — it's still a public test-facing
# name (tests/test_config_web.py) and renaming buys nothing besides churn.
MAX_EDIT_BODY_BYTES = 64 * 1024


def is_cross_site_write(headers: Any, *, bound_port: int | None = None) -> bool:
    """True when a POST looks like a CROSS-SITE browser request that must be refused.

    config-web binds ``127.0.0.1``, but that is NOT a security boundary in a browser: ANY web
    page the user visits can ``fetch('http://127.0.0.1:8787/edit', …)`` and (with a ``text/plain``
    body) dodge the CORS preflight, letting an attacker rewrite the user's ``rig.yaml`` /
    ``~/.config/rig/config.yaml`` — a classic DNS-rebinding / CSRF-against-localhost attack. We
    refuse a write unless it is same-origin:

    - ``Sec-Fetch-Site`` (sent by every modern browser, NOT forgeable by page JS) must be
      ``same-origin`` / ``none`` when present — a ``cross-site`` / ``same-site`` value is rejected.
    - A present ``Origin`` must EXACTLY match our own origin. "Same-origin" is scheme + host +
      PORT, so when ``bound_port`` is known an ``Origin`` on a different loopback port
      (``http://127.0.0.1:9999`` — another local service the attacker controls) is rejected, not
      just a foreign host. A non-``http`` scheme is rejected too.

    A non-browser client (curl, the test socket) sends neither header and is allowed — the threat
    model is a hostile *web page*, not local CLI tooling. Fail-closed only on a header that
    actively indicates a cross-site browser request.
    """
    sec = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if sec and sec not in ("same-origin", "none"):
        return True
    origin = (headers.get("Origin") or "").strip()
    if origin:
        from urllib.parse import urlparse

        parsed = urlparse(origin)
        if parsed.scheme != "http":
            return True
        if parsed.hostname not in (HOST, "localhost"):
            return True
        # exact-port match: same-origin is scheme+host+PORT. When we DON'T know our own bound port
        # (bound_port is None — a caller forgot to pass it, or a non-standard server), FAIL CLOSED
        # and reject any present Origin rather than silently waving the port check through. A
        # default http Origin omits the port → urlparse yields None → treat as 80, which never
        # matches our loopback dev port, so it is rejected.
        if bound_port is None:
            return True
        origin_port = parsed.port if parsed.port is not None else 80
        if origin_port != bound_port:
            return True
    return False


def is_allowed_host(headers: Any) -> bool:
    """True when the request's ``Host`` header names our loopback (a DNS-rebinding guard).

    Binding ``127.0.0.1`` stops remote TCP, but a page on ``http://evil.test`` whose DNS the
    attacker controls can be made to resolve to ``127.0.0.1`` and reach us; the browser then sends
    ``Host: evil.test``. The CSRF/Origin guard only protects WRITES — a GET of the config HTML (or a
    POST with a foreign Host) would still be served, letting the attacker page read the config
    cross-origin. So EVERY request must carry a loopback ``Host``. A missing Host (HTTP/1.0, raw
    client) is allowed — only a present, foreign hostname is rejected. The port, if present, is
    ignored here (the CSRF port check covers writes); we gate on the HOSTNAME only.
    """
    host = (headers.get("Host") or "").strip()
    if not host:
        return True  # no Host (HTTP/1.0 / a bare client) — not a rebinding browser request
    # The server binds AF_INET (IPv4) only, so the Host is always `name` or `name:port` — strip a
    # trailing `:port` and compare the hostname. (No IPv6 bracket handling: we never bind `::1`, so
    # a bracketed `[::1]` Host would be a non-loopback request to this IPv4 server and is rejected.)
    hostname = host.rsplit(":", 1)[0] if ":" in host else host
    return hostname in (HOST, "localhost")
