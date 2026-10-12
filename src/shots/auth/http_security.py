"""HTTP hardening for the web app: security headers on every response and a Host allow-list.

    from shots.auth.http_security import install_security_headers, install_trusted_hosts
    install_security_headers(app)        # in create_app: CSP, nosniff, framing, referrer, caching rules
    install_trusted_hosts(app)           # in the served app: 400 for any Host not in SHOTS_ALLOWED_HOSTS

Header policy (docs/platform.md, "Authentication" -> "HTTP hardening"):
- `Content-Security-Policy`: everything from our own origin only; no inline scripts (`script-src 'self'`).
  `style-src` keeps 'unsafe-inline' because templates set a few dynamic `style="width:..%"` bars.
- `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`,
  a minimal `Permissions-Policy`.
- `Vary: Cookie` on every non-static response (pages differ by session); `Cache-Control: no-store,
  private` on auth pages / endpoints and on any response to a request carrying a session cookie or
  setting a cookie (signed-in pages must never be cached or shared).
- `Strict-Transport-Security` only when the request itself came over HTTPS.
"""

from __future__ import annotations

import os
from typing import Iterable

from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware

from shots.auth import settings as S

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; "
       "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
PERMISSIONS_POLICY = ("camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
                      "interest-cohort=(), browsing-topics=()")
HSTS = "max-age=31536000"
STATIC_PREFIX = "/static/"
PRIVATE_PATHS = frozenset({S.LOGIN_PATH, S.REGISTER_PATH, S.ACCOUNT_PATH})
PRIVATE_PREFIXES = ("/auth/",)
DEFAULT_ALLOWED_HOSTS = ("127.0.0.1", "localhost")


def _cookie_names(raw: str) -> set[str]:
    return {part.split("=", 1)[0].strip() for part in raw.split(";") if "=" in part}


class SecurityHeadersMiddleware:
    """Pure ASGI middleware (works for streaming / file responses too); never overrides a CSP or
    framing header a route set deliberately."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path") or "/"
        static = path.startswith(STATIC_PREFIX)
        https = scope.get("scheme") == "https"
        cookies: set[str] = set()
        for k, v in scope.get("headers") or ():
            if k == b"cookie":
                cookies |= _cookie_names(v.decode("latin-1"))
        private = (path in PRIVATE_PATHS or path.startswith(PRIVATE_PREFIXES) or S.SESSION_COOKIE in cookies)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h.setdefault("Content-Security-Policy", CSP)
                h.setdefault("X-Content-Type-Options", "nosniff")
                h.setdefault("X-Frame-Options", "DENY")
                h.setdefault("Referrer-Policy", "same-origin")
                h.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
                if https:
                    h.setdefault("Strict-Transport-Security", HSTS)
                if not static:
                    vary = [v.strip() for v in h.get("vary", "").split(",") if v.strip()]
                    if not any(v.lower() == "cookie" for v in vary):
                        h["Vary"] = ", ".join(vary + ["Cookie"])
                    if private or h.getlist("set-cookie"):
                        h["Cache-Control"] = "no-store, private"
            await send(message)

        await self.app(scope, receive, send_wrapper)


def install_security_headers(app) -> None:
    app.add_middleware(SecurityHeadersMiddleware)


def allowed_hosts(env: str | None = None) -> list[str]:
    """SHOTS_ALLOWED_HOSTS (comma-separated host names, e.g. "127.0.0.1,localhost,shots.example.com";
    "*.example.com" wildcards as in Starlette), default 127.0.0.1 and localhost."""
    raw = os.environ.get("SHOTS_ALLOWED_HOSTS") if env is None else env
    hosts = [h.strip().lower() for h in (raw or "").split(",") if h.strip()]
    return hosts or list(DEFAULT_ALLOWED_HOSTS)


def install_trusted_hosts(app, hosts: Iterable[str] | None = None) -> list[str]:
    """Reject requests whose Host header is not allowed (400): blocks DNS-rebinding attacks on the
    local server. Add it last so it runs first."""
    allowed = list(hosts) if hosts is not None else allowed_hosts()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed)
    return allowed
