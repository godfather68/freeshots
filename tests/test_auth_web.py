"""FastAPI integration: router endpoints, cookies, CSRF, open-redirect protection, guards,
entitlements provider, page context."""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher, Type
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from shots.auth import passwords as pw
from shots.auth import settings as S
from shots.auth.service import AuthService
from shots.auth.web import (auth_context, csrf_token, current_user, entitlements_provider, form_data, install_auth,
                            require_admin, require_entitlement, require_user, safe_next, verify_csrf)

GOOD = "violet-harbour-kettle-93"
GOOD2 = "maple-quartz-lantern-58"


@pytest.fixture(autouse=True)
def fast_hasher():
    old = pw.hasher()
    pw.set_hasher(PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, type=Type.ID))
    yield
    pw.set_hasher(old)


def make_app(svc: AuthService) -> FastAPI:
    app = FastAPI()
    install_auth(app, svc)

    @app.get("/login")
    def login_page(request: Request):
        ctx = auth_context(request, csrf=True)
        return {k: ctx[k] for k in ("csrf_token", "auth_error", "auth_error_message", "next")}

    @app.get("/page")
    def page(request: Request):
        ctx = auth_context(request)
        return {"csrf_token": ctx["csrf_token"], "user": ctx["user"].email if ctx["user"] else None,
                "plan": ctx["plan"], "ok": ctx["auth_ok_message"]}

    @app.get("/private")
    def private(user=Depends(require_user)):
        return {"email": user.email}

    @app.get("/export")
    def export(ents=Depends(require_entitlement("export"))):
        return {"export": ents.export}

    @app.get("/admin")
    def admin(user=Depends(require_admin)):
        return {"admin": user.email}

    @app.get("/ents")
    def ents(request: Request):
        e = entitlements_provider(request)
        return {"all": e.can_view_all_leagues, "days": e.max_days_ahead, "user": bool(current_user(request))}

    @app.post("/action", dependencies=[Depends(verify_csrf)])
    async def action(request: Request):
        return {"value": (await form_data(request))["value"]}

    return app


@pytest.fixture
def svc(migrated_db):
    s = AuthService(migrated_db, secret=b"k" * 32)
    yield s
    s.engine.dispose()


@pytest.fixture
def client(svc):
    with TestClient(make_app(svc), follow_redirects=False) as c:
        yield c


def csrf(client) -> str:
    return client.get("/login").json()["csrf_token"]


def register(client, email="a@example.com", password=GOOD, **extra):
    return client.post("/auth/register", data={"email": email, "password": password, "csrf_token": csrf(client),
                                               **extra})


def login(client, email="a@example.com", password=GOOD, **extra):
    return client.post("/auth/login", data={"email": email, "password": password, "csrf_token": csrf(client),
                                            **extra})


def cookie_header(resp, name: str = S.SESSION_COOKIE) -> str:
    (h,) = [v for v in resp.headers.get_list("set-cookie") if v.startswith(name + "=")]
    return h


def location(resp) -> str:
    assert resp.status_code == 303
    return resp.headers["location"]


# ----------------------------------------------------------------------------- cookies

def test_prelogin_csrf_cookie_flags(client):
    r = client.get("/login")
    token = r.json()["csrf_token"]
    assert token and len(token) >= 40
    assert not [v for v in r.headers.get_list("set-cookie") if v.startswith(S.SESSION_COOKIE + "=")]
    h = cookie_header(r, S.PRELOGIN_COOKIE).lower()
    assert "httponly" in h and "samesite=lax" in h and "path=/" in h and "secure" not in h
    assert f"max-age={int(S.PRELOGIN_CSRF_TTL.total_seconds())}" in h
    nonce = client.cookies.get(S.PRELOGIN_COOKIE)
    r2 = client.get("/login")
    assert not r2.headers.get_list("set-cookie") and client.cookies.get(S.PRELOGIN_COOKIE) == nonce  # reused


def test_secure_flag_over_https(svc):
    with TestClient(make_app(svc), base_url="https://testserver", follow_redirects=False) as c:
        assert "secure" in cookie_header(c.get("/login"), S.PRELOGIN_COOKIE).lower()
        r = c.post("/auth/register", data={"email": "s@example.com", "password": GOOD, "csrf_token": csrf(c)})
        assert "secure" in cookie_header(r).lower()


def test_secure_flag_forced_by_env(svc, client, monkeypatch):
    monkeypatch.setenv("SHOTS_COOKIE_SECURE", "1")
    assert "secure" in cookie_header(client.get("/login"), S.PRELOGIN_COOKIE).lower()


def test_pages_without_forms_do_not_create_sessions(client, svc):
    r = client.get("/page")
    assert r.json()["csrf_token"] is None and not r.headers.get_list("set-cookie")


# ----------------------------------------------------------------------------- register / login / logout

def test_register_logs_in_and_rotates(client):
    anon = csrf(client)
    anon_cookie = client.cookies.get(S.SESSION_COOKIE)
    r = client.post("/auth/register", data={"email": "New@Example.com", "password": GOOD, "password_confirm": GOOD,
                                            "csrf_token": anon, "next": "/private?x=1"})
    assert location(r) == "/private?x=1"
    h = cookie_header(r).lower()
    assert "httponly" in h and f"max-age={int(S.SESSION_ABSOLUTE_TIMEOUT.total_seconds())}" in h
    assert client.cookies.get(S.SESSION_COOKIE) != anon_cookie
    assert client.get("/private").json() == {"email": "new@example.com"}
    page = client.get("/page").json()
    assert page["plan"] == {"key": "free", "name": "Free"} and page["csrf_token"] not in (None, anon)


@pytest.mark.parametrize("data,error", [
    ({"email": "bad", "password": GOOD}, "error=invalid_email"),
    ({"email": "a@example.com", "password": "short"}, "error=weak_password&reason=too_short"),
    ({"email": "a@example.com", "password": "password1234"}, "error=weak_password&reason=common"),
    ({"email": "a@example.com", "password": GOOD, "password_confirm": GOOD2}, "error=password_mismatch"),
])
def test_register_errors(client, data, error):
    r = client.post("/auth/register", data={**data, "csrf_token": csrf(client)})
    assert location(r) == f"/register?{error}"
    assert client.get("/private").status_code == 303


def test_register_email_taken(client):
    register(client)
    client.cookies.clear()
    assert location(register(client, email="A@example.com", password=GOOD2)) == "/register?error=email_taken"


def test_login_success_and_generic_failures(client):
    register(client)
    client.cookies.clear()
    wrong = login(client, password="wrong-password-xx", next="/private")
    unknown = login(client, email="ghost@example.com", next="/private")
    assert location(wrong) == location(unknown) == "/login?error=invalid_credentials&next=%2Fprivate"
    assert client.get("/private").status_code == 303
    before = client.cookies.get(S.SESSION_COOKIE)
    r = login(client, next="/private")
    assert location(r) == "/private" and client.cookies.get(S.SESSION_COOKIE) != before
    assert client.get("/private").status_code == 200


def test_login_rate_limited(client):
    register(client)
    client.cookies.clear()
    for _ in range(S.LOGIN_FREE_FAILURES):
        login(client, password="wrong-password-xx")
    assert location(login(client)) == "/login?error=rate_limited"


def test_logout(client, svc):
    register(client)
    token = client.cookies.get(S.SESSION_COOKIE)
    tok = client.get("/page").json()["csrf_token"]
    r = client.post("/auth/logout", data={"csrf_token": tok})
    assert location(r) == "/"
    h = cookie_header(r).lower()
    assert 'shots_session=""' in h and "max-age=0" in h
    assert svc.validate(token) is None


def test_logout_requires_csrf(client, svc):
    register(client)
    token = client.cookies.get(S.SESSION_COOKIE)
    assert location(client.post("/auth/logout", data={})) == "/account?error=csrf"
    assert svc.validate(token) is not None


def test_logout_all(client, svc):
    register(client)
    other = svc.create_session(svc.find_user("a@example.com").id).token
    tok = client.get("/page").json()["csrf_token"]
    r = client.post("/auth/logout-all", data={"csrf_token": tok})
    assert location(r) == "/?ok=signed_out_everywhere"
    assert svc.validate(other) is None and client.get("/private").status_code == 303


def test_change_password_endpoint(client, svc):
    register(client)
    other = svc.create_session(svc.find_user("a@example.com").id).token
    tok = client.get("/page").json()["csrf_token"]
    bad = client.post("/auth/password", data={"csrf_token": tok, "current_password": "nope-nope-nope",
                                              "new_password": GOOD2})
    assert location(bad) == "/account?error=invalid_credentials"
    weak = client.post("/auth/password", data={"csrf_token": tok, "current_password": GOOD, "new_password": "short"})
    assert location(weak) == "/account?error=weak_password&reason=too_short"
    ok = client.post("/auth/password", data={"csrf_token": tok, "current_password": GOOD, "new_password": GOOD2,
                                             "new_password_confirm": GOOD2})
    assert location(ok) == "/account?ok=password_changed"
    assert svc.validate(other) is None
    assert client.get("/private").status_code == 200  # this browser got a fresh session
    assert client.get("/page").json()["csrf_token"] != tok


def test_change_password_requires_login(client):
    r = client.post("/auth/password", data={"csrf_token": csrf(client)})
    assert location(r) == "/login?error=login_required&next=%2Faccount"


# ----------------------------------------------------------------------------- CSRF

def test_csrf_rejections(client):
    tok = csrf(client)
    base = {"email": "a@example.com", "password": GOOD}
    assert location(client.post("/auth/register", data=base)) == "/register?error=csrf"
    assert location(client.post("/auth/register", data={**base, "csrf_token": tok + "x"})) == "/register?error=csrf"
    assert location(client.post("/auth/register", data={**base, "csrf_token": tok},
                                headers={"Origin": "https://evil.example"})) == "/register?error=csrf"
    assert location(client.post("/auth/register", data={**base, "csrf_token": tok},
                                headers={"Origin": "null"})) == "/register?error=csrf"
    client.cookies.clear()  # token without its pre-login cookie
    assert location(client.post("/auth/login", data={**base, "csrf_token": tok})) == "/login?error=csrf"
    assert location(register(client)) == "/"  # and with everything right it works


def test_csrf_token_of_another_session_rejected(svc):
    with TestClient(make_app(svc), follow_redirects=False) as a, TestClient(make_app(svc), follow_redirects=False) as b:
        tok_a = csrf(a)
        csrf(b)
        r = b.post("/auth/register", data={"email": "a@example.com", "password": GOOD, "csrf_token": tok_a})
        assert location(r) == "/register?error=csrf"


def test_same_origin_header_accepted(client):
    tok = csrf(client)
    r = client.post("/auth/register", data={"email": "a@example.com", "password": GOOD, "csrf_token": tok},
                    headers={"Origin": "http://testserver"})
    assert location(r) == "/"


def test_verify_csrf_dependency(client):
    register(client)
    tok = client.get("/page").json()["csrf_token"]
    assert client.post("/action", data={"value": "1"}).status_code == 403
    assert client.post("/action", data={"value": "1", "csrf_token": "bad"}).status_code == 403
    assert client.post("/action", data={"value": "1", "csrf_token": tok}).json() == {"value": "1"}
    assert client.post("/action", data={"value": "2"}, headers={"X-CSRF-Token": tok}).json() == {"value": "2"}


def test_invalid_form_bodies(client):
    tok = csrf(client)
    r = client.post("/auth/login", json={"email": "a@example.com", "password": GOOD, "csrf_token": tok})
    assert location(r) == "/login?error=invalid_request"
    big = {"email": "a@example.com", "password": GOOD, "csrf_token": tok, "pad": "x" * (S.FORM_MAX_BYTES + 1)}
    assert location(client.post("/auth/login", data=big)) == "/login?error=invalid_request"


# ----------------------------------------------------------------------------- open redirect

@pytest.mark.parametrize("target", ["//evil.example", "https://evil.example/x", "/\\evil.example", "\\\\evil.example",
                                    "http:evil.example", "javascript:alert(1)", "/\t/evil.example", "evil.example",
                                    "/" + "a" * S.NEXT_MAX_LENGTH, " //evil.example"])
def test_open_redirect_rejected(client, target):
    assert safe_next(target) == "/"
    register(client)
    client.cookies.clear()
    assert location(login(client, next=target)) == "/"


@pytest.mark.parametrize("target", ["/", "/match/abc?n=24", "/account#x", "/%2F%2Fevil.example"])
def test_local_next_accepted(target):
    assert safe_next(target) == target


def test_error_redirect_drops_unsafe_next(client):
    r = login(client, email="ghost@example.com", next="https://evil.example")
    assert location(r) == "/login?error=invalid_credentials"


# ----------------------------------------------------------------------------- guards / entitlements

def test_require_user_redirects_to_login(client):
    r = client.get("/private?tab=2")
    assert location(r) == "/login?error=login_required&next=%2Fprivate%3Ftab%3D2"


def test_require_entitlement(client, svc):
    assert location(client.get("/export")).startswith("/login?error=login_required")
    register(client)
    assert client.get("/export").status_code == 403  # Free has no export
    svc.assign_plan(svc.find_user("a@example.com").id, "premium")
    assert client.get("/export").json() == {"export": True}


def test_require_entitlement_only_boolean_keys():
    with pytest.raises(ValueError):
        require_entitlement("max_days_ahead")
    with pytest.raises(ValueError):
        require_entitlement("premium")


def test_require_admin(client, svc):
    register(client)
    assert client.get("/admin").status_code == 403
    svc.set_admin(svc.find_user("a@example.com").id, True)
    assert client.get("/admin").json() == {"admin": "a@example.com"}


def test_entitlements_provider(client, svc):
    assert client.get("/ents").json() == {"all": False, "days": 3, "user": False}  # anonymous = Free
    register(client)
    svc.assign_plan(svc.find_user("a@example.com").id, "pro")
    assert client.get("/ents").json() == {"all": True, "days": 30, "user": True}
    svc.set_active(svc.find_user("a@example.com").id, False)
    assert client.get("/ents").json() == {"all": False, "days": 3, "user": False}


def test_forged_cookie_is_anonymous(client):
    client.cookies.set(S.SESSION_COOKIE, "forged")
    assert client.get("/ents").json()["user"] is False


# ----------------------------------------------------------------------------- page context

def test_auth_context_messages_never_reflect_input(client):
    j = client.get("/login", params={"error": "<script>alert(1)</script>", "next": "//evil"}).json()
    assert j["auth_error"] == "invalid_request" and "<script>" not in j["auth_error_message"] and j["next"] == ""
    j = client.get("/login", params={"error": "weak_password", "reason": "too_short", "next": "/match/x"}).json()
    assert j["auth_error"] == "weak_password" and "12 characters" in j["auth_error_message"]
    assert j["next"] == "/match/x"
    assert client.get("/page", params={"ok": "password_changed"}).json()["ok"].startswith("Your password")


def test_csrf_token_requires_install(migrated_db):
    app = FastAPI()

    @app.get("/x")
    def x(request: Request):
        return csrf_token(request)

    with TestClient(app, raise_server_exceptions=True) as c, pytest.raises(RuntimeError, match="install_auth"):
        c.get("/x")


def test_works_with_the_real_web_app(migrated_db, svc):
    """create_app(entitlements=entitlements_provider) + install_auth: pages render, auth works."""
    from shots.web.app import create_app

    app = create_app(migrated_db, entitlements=entitlements_provider)
    install_auth(app, svc)
    with TestClient(app, follow_redirects=False) as c:
        assert c.get("/").status_code == 200
        r = c.post("/auth/login", data={"email": "x@example.com", "password": GOOD})
        assert location(r) == "/login?error=csrf"
