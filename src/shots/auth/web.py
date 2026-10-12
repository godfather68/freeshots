"""FastAPI integration of the auth services (no pages: the web app owns GET /login, /register,
/account, /pricing and renders them with `auth_context`).

Wiring (in the web app factory):

    from shots.auth.web import install_auth, entitlements_provider
    app = create_app(db, entitlements=entitlements_provider)   # every page/query sees the caller's plan
    install_auth(app, db)                                        # service + router + cookie middleware

The order does not matter (the provider reads the service from app.state at request time).
State-changing forms post `csrf_token` (see `csrf_token(request)`); routes of the app that change
state depend on `verify_csrf` and read fields with `await form_data(request)`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import Engine
from starlette.datastructures import MutableHeaders
from starlette.responses import Response

from shots.auth import messages
from shots.auth import settings as S
from shots.auth.service import AuthError, AuthService, SessionState, UserInfo
from shots.db.access import KEYS, Entitlements

log = logging.getLogger(__name__)

_STATE_SERVICE = "shots_auth"
_SESSION = "_shots_auth_session"  # request.state cache: SessionState | None
_ENTS = "_shots_auth_ents"
_FORM = "_shots_auth_form"
_PENDING = "_shots_auth_pending_cookie"  # Set-Cookie value the middleware adds (pre-login CSRF nonce)
BOOL_KEYS = tuple(k for k in KEYS if isinstance(getattr(Entitlements(), k), bool))


# ----------------------------------------------------------------------------- service and request basics

def get_service(request: Request) -> AuthService:
    svc = getattr(request.app.state, _STATE_SERVICE, None)
    if svc is None:
        raise RuntimeError("auth is not installed on this app: call shots.auth.web.install_auth(app, db)")
    return svc


def client_ip(request: Request) -> str | None:
    """Peer address (uvicorn's --proxy-headers rewrites it from X-Forwarded-For for trusted proxies only)."""
    return request.client.host if request.client else None


def user_agent(request: Request) -> str | None:
    return (request.headers.get("user-agent") or "")[:400] or None


def is_https(request: Request) -> bool:
    return request.url.scheme == "https" or os.environ.get("SHOTS_COOKIE_SECURE", "").lower() in ("1", "true", "yes")


def safe_next(value: str | None, default: str = S.DEFAULT_NEXT) -> str:
    """A local path to redirect to, or `default`. Rejects absolute / scheme-relative URLs ("//x",
    "/\\x", "https://x"), control characters and backslashes: no open redirect."""
    if not value or not isinstance(value, str) or len(value) > S.NEXT_MAX_LENGTH:
        return default
    if "\\" in value or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return default
    if not value.startswith("/") or value.startswith("//"):
        return default
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return default
    return value


# ----------------------------------------------------------------------------- cookies

def _cookie_kwargs(request: Request | None) -> dict:
    return dict(path="/", httponly=True, samesite="lax", secure=bool(request is not None and is_https(request)))


def set_session_cookie(response: Response, token: str, request: Request, max_age: int | None = None) -> None:
    """HttpOnly, SameSite=Lax, Path=/, Secure when the request came over HTTPS (or SHOTS_COOKIE_SECURE=1)."""
    age = int(S.SESSION_ABSOLUTE_TIMEOUT.total_seconds()) if max_age is None else max_age
    response.set_cookie(S.SESSION_COOKIE, token, max_age=age, **_cookie_kwargs(request))


def clear_session_cookie(response: Response, request: Request | None = None) -> None:
    response.delete_cookie(S.SESSION_COOKIE, **_cookie_kwargs(request))


def clear_prelogin_cookie(response: Response, request: Request) -> None:
    if request.cookies.get(S.PRELOGIN_COOKIE) is not None:
        response.delete_cookie(S.PRELOGIN_COOKIE, **_cookie_kwargs(request))


class AuthCookieMiddleware:
    """Pure ASGI middleware: adds the Set-Cookie of a cookie created while rendering a page (the
    pre-login CSRF nonce of the login / register forms), unless the response sets that cookie itself."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        state = scope.setdefault("state", {})

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                pending = state.get(_PENDING)
                if pending:
                    headers = MutableHeaders(scope=message)
                    mine = pending.split("=", 1)[0] + "="
                    if not any(v.startswith(mine) for v in headers.getlist("set-cookie")):
                        headers.append("set-cookie", pending)
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ----------------------------------------------------------------------------- current session / user

def session_token(request: Request) -> str | None:
    return request.cookies.get(S.SESSION_COOKIE)


def current_session(request: Request) -> SessionState | None:
    """The caller's live session (validated once per request, then cached)."""
    if not hasattr(request.state, _SESSION):
        setattr(request.state, _SESSION, get_service(request).validate(session_token(request)))
    return getattr(request.state, _SESSION)


def current_user(request: Request) -> UserInfo | None:
    """Dependency: the signed-in user, or None for anonymous visitors."""
    sess = current_session(request)
    return sess.user if sess else None


def _login_redirect(request: Request) -> HTTPException:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    qs = {"error": "login_required"}
    if request.method == "GET":
        qs["next"] = safe_next(target)
    return HTTPException(status_code=303, headers={"Location": f"{S.LOGIN_PATH}?{urlencode(qs)}"})


def require_user(request: Request) -> UserInfo:
    """Dependency: the signed-in user; anonymous callers get a 303 to /login?next=<this page>."""
    user = current_user(request)
    if user is None:
        raise _login_redirect(request)
    return user


def require_admin(request: Request) -> UserInfo:
    user = require_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


def entitlements_provider(request: Request) -> Entitlements:
    """`create_app(..., entitlements=entitlements_provider)`: the caller's entitlements (cached per request)."""
    if not hasattr(request.state, _ENTS):
        user = current_user(request)
        svc = get_service(request)
        setattr(request.state, _ENTS, svc.entitlements_for_user(user.id) if user else svc.anonymous_entitlements())
    return getattr(request.state, _ENTS)


def require_entitlement(key: str):
    """Dependency factory for boolean entitlements (`Depends(require_entitlement("export"))`).
    Anonymous callers without it get a 303 to /login, signed-in ones a 403. Numeric / list
    entitlements (max_days_ahead, allowed_leagues) are enforced in the repository queries."""
    if key not in BOOL_KEYS:
        raise ValueError(f"not a boolean entitlement: {key!r} (known: {', '.join(BOOL_KEYS)})")

    def dependency(request: Request) -> Entitlements:
        ents = entitlements_provider(request)
        if not getattr(ents, key):
            if current_user(request) is None:
                raise _login_redirect(request)
            raise HTTPException(status_code=403, detail="Your plan does not include this feature")
        return ents

    dependency.__name__ = f"require_{key}"
    return dependency


# ----------------------------------------------------------------------------- CSRF and forms

def csrf_token(request: Request) -> str:
    """The CSRF token to embed in forms (`<input type="hidden" name="csrf_token" value="...">`).
    Callers with a session: derived from it (no DB write). Visitors without one get a stateless
    pre-login token (valid for the login / register forms only), bound to a random nonce in the
    HttpOnly `shots_prelogin` cookie (set by AuthCookieMiddleware when missing). Nothing is stored."""
    sess = current_session(request)
    if sess is not None:
        return sess.csrf
    svc = get_service(request)
    nonce = getattr(request.state, _PENDING + "_nonce", None) or request.cookies.get(S.PRELOGIN_COOKIE)
    if not svc.valid_prelogin_nonce(nonce):
        nonce = svc.new_prelogin_nonce()
        probe = Response()
        probe.set_cookie(S.PRELOGIN_COOKIE, nonce, max_age=int(S.PRELOGIN_CSRF_TTL.total_seconds()),
                         **_cookie_kwargs(request))
        setattr(request.state, _PENDING, probe.headers["set-cookie"])
        setattr(request.state, _PENDING + "_nonce", nonce)
    return svc.prelogin_csrf(nonce)


async def form_data(request: Request) -> dict[str, str] | None:
    """Parse an application/x-www-form-urlencoded body (at most FORM_MAX_BYTES; first value of each
    field wins). None if the body is missing, too large, malformed or of another type. Cached."""
    if hasattr(request.state, _FORM):
        return getattr(request.state, _FORM)
    result = None
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    declared = request.headers.get("content-length")
    if ctype == "application/x-www-form-urlencoded" and not (declared and declared.isdigit()
                                                             and int(declared) > S.FORM_MAX_BYTES):
        body, too_big = b"", False
        async for chunk in request.stream():
            body += chunk
            if len(body) > S.FORM_MAX_BYTES:
                too_big = True
                break
        if not too_big:
            try:
                pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, strict_parsing=False,
                                  max_num_fields=50)
                result = {}
                for k, v in pairs:
                    result.setdefault(k, v)
            except (UnicodeDecodeError, ValueError):
                result = None
    setattr(request.state, _FORM, result)
    return result


def _origin_ok(request: Request) -> bool:
    """If the browser sent an Origin, it must be this host (defence in depth on top of the token)."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    if origin == "null":
        return False
    return urlsplit(origin).netloc.lower() == (request.headers.get("host") or "").lower()


def check_csrf(request: Request, form: dict | None, allow_prelogin: bool = False) -> bool:
    """Origin check + the caller's session CSRF token. allow_prelogin=True (login / register only)
    also accepts the stateless pre-login token bound to the `shots_prelogin` cookie."""
    submitted = (form or {}).get(S.CSRF_FIELD) or request.headers.get(S.CSRF_HEADER)
    if not _origin_ok(request):
        return False
    svc = get_service(request)
    if svc.check_csrf(session_token(request), submitted):
        return True
    return allow_prelogin and svc.check_prelogin_csrf(request.cookies.get(S.PRELOGIN_COOKIE), submitted)


async def verify_csrf(request: Request) -> None:
    """Dependency for the app's own state-changing routes: 403 unless the form field `csrf_token`
    (or header X-CSRF-Token) matches the caller's live session. Read fields with form_data()."""
    from starlette.concurrency import run_in_threadpool

    form = await form_data(request)
    if not await run_in_threadpool(check_csrf, request, form):
        raise HTTPException(status_code=403, detail="CSRF check failed")


# ----------------------------------------------------------------------------- page context

def auth_context(request: Request, csrf: bool = False) -> dict:
    """Template context for any page. csrf=True on pages with a login/register form (a stateless
    pre-login token for visitors); a signed-in user always gets a token (logout button)."""
    user = current_user(request)
    svc = get_service(request)
    q = request.query_params
    error, reason = q.get("error"), q.get("reason")
    token = csrf_token(request) if (csrf or user is not None) else None
    plan = svc.plan_for_user(user.id if user else None)
    return {
        "user": user,
        "plan": {"key": plan[0], "name": plan[1]} if plan else None,
        "ent": entitlements_provider(request),
        "csrf_token": token,
        "csrf_field": S.CSRF_FIELD,
        "auth_error": error if error in messages.ERRORS else (None if not error else "invalid_request"),
        "auth_error_message": messages.error_message(error, reason),
        "auth_ok_message": messages.ok_message(q.get("ok")),
        "next": safe_next(q.get("next"), default=""),
        "password_min_length": S.PASSWORD_MIN_LENGTH,
        "password_max_length": S.PASSWORD_MAX_LENGTH,
    }


# ----------------------------------------------------------------------------- router

def _redirect(url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _back(path: str, error: str, reason: str | None = None, next_: str | None = None) -> RedirectResponse:
    qs = {"error": error}
    if reason:
        qs["reason"] = reason
    if next_ and next_ != S.DEFAULT_NEXT:
        qs["next"] = next_
    return _redirect(f"{path}?{urlencode(qs)}")


async def _form_dep(request: Request) -> dict | None:
    return await form_data(request)


def auth_router(prefix: str = "/auth") -> APIRouter:
    """POST /auth/register, /auth/login, /auth/logout, /auth/logout-all, /auth/password.
    Form fields: csrf_token always; next (optional local path); see docs/platform.md."""
    router = APIRouter(prefix=prefix)

    @router.post("/register")
    def register(request: Request, form: dict | None = Depends(_form_dep)):
        next_ = safe_next((form or {}).get("next"))
        if form is None:
            return _back(S.REGISTER_PATH, "invalid_request", next_=next_)
        if not check_csrf(request, form, allow_prelogin=True):
            return _back(S.REGISTER_PATH, "csrf", next_=next_)
        svc = get_service(request)
        try:
            user = svc.register(form.get("email", ""), form.get("password", ""), client_ip(request),
                                password_confirm=form.get("password_confirm"))
            new = svc.create_session(user.id, client_ip(request), user_agent(request),
                                     replace_token=session_token(request))
        except AuthError as exc:
            return _back(S.REGISTER_PATH, exc.code, exc.reason, next_)
        resp = _redirect(next_)
        set_session_cookie(resp, new.token, request, new.max_age)
        clear_prelogin_cookie(resp, request)
        return resp

    @router.post("/login")
    def login(request: Request, form: dict | None = Depends(_form_dep)):
        next_ = safe_next((form or {}).get("next"))
        if form is None:
            return _back(S.LOGIN_PATH, "invalid_request", next_=next_)
        if not check_csrf(request, form, allow_prelogin=True):
            return _back(S.LOGIN_PATH, "csrf", next_=next_)
        try:
            _, new = get_service(request).login(form.get("email", ""), form.get("password", ""),
                                                client_ip(request), user_agent(request),
                                                old_token=session_token(request))
        except AuthError as exc:
            return _back(S.LOGIN_PATH, exc.code, None, next_)
        resp = _redirect(next_)
        set_session_cookie(resp, new.token, request, new.max_age)
        clear_prelogin_cookie(resp, request)
        return resp

    @router.post("/logout")
    def logout(request: Request, form: dict | None = Depends(_form_dep)):
        next_ = safe_next((form or {}).get("next"))
        if not check_csrf(request, form):
            return _back(S.ACCOUNT_PATH if current_user(request) else S.DEFAULT_NEXT, "csrf")
        get_service(request).revoke(session_token(request))
        resp = _redirect(next_)
        clear_session_cookie(resp, request)
        return resp

    @router.post("/logout-all")
    def logout_all(request: Request, form: dict | None = Depends(_form_dep)):
        user = current_user(request)
        if user is None:
            return _redirect(S.LOGIN_PATH)
        if not check_csrf(request, form):
            return _back(S.ACCOUNT_PATH, "csrf")
        get_service(request).revoke_all(user.id)
        resp = _redirect(f"{S.DEFAULT_NEXT}?{urlencode({'ok': 'signed_out_everywhere'})}")
        clear_session_cookie(resp, request)
        return resp

    @router.post("/password")
    def change_password(request: Request, form: dict | None = Depends(_form_dep)):
        user = current_user(request)
        if user is None:
            return _redirect(f"{S.LOGIN_PATH}?{urlencode({'error': 'login_required', 'next': S.ACCOUNT_PATH})}")
        if form is None:
            return _back(S.ACCOUNT_PATH, "invalid_request")
        if not check_csrf(request, form):
            return _back(S.ACCOUNT_PATH, "csrf")
        try:
            new = get_service(request).change_password(
                user.id, form.get("current_password", ""), form.get("new_password", ""),
                form.get("new_password_confirm"), client_ip(request), user_agent(request))
        except AuthError as exc:
            return _back(S.ACCOUNT_PATH, exc.code, exc.reason)
        resp = _redirect(f"{S.ACCOUNT_PATH}?{urlencode({'ok': 'password_changed'})}")
        set_session_cookie(resp, new.token, request, new.max_age)
        return resp

    return router


def install_auth(app: FastAPI, db: str | Path | Engine | AuthService | None = None,
                 anonymous_plan: str | None = None) -> AuthService:
    """Attach the auth service, the /auth router and the cookie middleware to `app`."""
    svc = db if isinstance(db, AuthService) else AuthService(db, anonymous_plan=anonymous_plan)
    setattr(app.state, _STATE_SERVICE, svc)
    app.add_middleware(AuthCookieMiddleware)
    app.include_router(auth_router())
    return svc
