"""Regression tests of the 2026-10-09 security review: /track cache bound, atomic rate limiting,
Argon2 concurrency cap, Host allow-list, stateless pre-login CSRF (no session row per anonymous view),
worker auth purge, explicit proxy / access-log settings, security headers."""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from argon2 import PasswordHasher, Type
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from shots.auth import passwords as pw
from shots.auth import settings as S
from shots.auth.service import AuthError, AuthService
from shots.auth.web import entitlements_provider, install_auth
from shots.db.engine import make_engine, session_scope
from shots.db.models import LoginAttempt, UserSession

GOOD = "violet-harbour-kettle-93"
TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "shots" / "web" / "templates"


@pytest.fixture(autouse=True)
def fast_hasher():
    old = pw.hasher()
    pw.set_hasher(PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, type=Type.ID))
    yield
    pw.set_hasher(old)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 9, 12, 0, 0)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def svc(migrated_db, clock):
    s = AuthService(migrated_db, secret=b"k" * 32, clock=clock)
    yield s
    s.engine.dispose()


_SEEDED: set = set()


def real_app(db, svc):
    """The real web app + auth on `db`, with a board seeded (pages show "No board yet" without one)."""
    from shots.web.app import create_app
    from tests.test_web import TODAY, make_board, write_board

    if str(db) not in _SEEDED:
        write_board(db, make_board())
        _SEEDED.add(str(db))
    app = create_app(db, today=lambda: TODAY, entitlements=entitlements_provider)
    install_auth(app, svc)
    return app


def count(engine, model, *where) -> int:
    with session_scope(engine) as s:
        return int(s.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def form_token(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


# ----------------------------------------------------------------------------- 1. /track cache

def test_track_cache_is_keyed_on_resolved_values_and_bounded(migrated_db, monkeypatch):
    """300 junk src / n values used to create 300 cache entries (and 300 expensive computations)."""
    from shots.db.repo import Repository
    from shots.web import app as webapp

    calls = []
    real = Repository.track_summary

    def counting(self, source, n, min_prob, ent=None):
        calls.append((source, n))
        return real(self, source, n, min_prob, ent) if ent is not None else real(self, source, n, min_prob)

    monkeypatch.setattr(Repository, "track_summary", counting)
    app = webapp.create_app(migrated_db)
    with TestClient(app) as c:
        for i in range(300):
            assert c.get("/track", params={"src": f"junk{i}", "n": 1000 + i}).status_code == 200
            assert c.get("/track", params={"src": "live", "n": -i}).status_code == 200
    assert len(app.state.track_cache) <= 4
    assert len(calls) <= 4


def test_track_cache_lru_bound_with_many_valid_ns(migrated_db, monkeypatch):
    from shots.db.repo import Repository
    from shots.web import app as webapp

    avail = list(range(10, 60))

    def fake(self, source, n, min_prob, ent=None):
        n = n if n in avail else 24
        return dict(src=source, counts={}, avail=avail, n=n, has_data=False, metrics=None, total=0, n_comp=0)

    monkeypatch.setattr(Repository, "track_summary", fake)
    app = webapp.create_app(migrated_db)
    with TestClient(app) as c:
        for src in ("backfill", "live"):
            for n in avail:
                assert c.get("/track", params={"src": src, "n": n}).status_code == 200
    assert len(app.state.track_cache) == webapp.TRACK_CACHE_MAX


# ----------------------------------------------------------------------------- 2. atomic rate limits

def _slow_counting_verify(monkeypatch, delay=0.05):
    calls = []
    lock = threading.Lock()
    real = pw.verify_password

    def verify(encoded, password):
        with lock:
            calls.append(1)
        time.sleep(delay)  # widen the check -> verify -> record window, as a real Argon2 verify does
        return real(encoded, password)

    monkeypatch.setattr(pw, "verify_password", verify)
    return calls


def _hammer(n, fn):
    barrier = threading.Barrier(n)
    results = []

    def run():
        barrier.wait()
        try:
            fn()
            results.append("ok")
        except AuthError as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results


def test_parallel_wrong_passwords_get_exactly_the_free_failures(svc, monkeypatch):
    """Before the fix 30 parallel guesses got ~19 password verifications instead of 5."""
    svc.create_user("a@example.com", GOOD)
    calls = _slow_counting_verify(monkeypatch)
    results = _hammer(30, lambda: svc.authenticate("a@example.com", "wrong-password-xx", "9.9.9.9"))
    assert len(calls) == S.LOGIN_FREE_FAILURES
    assert results.count("invalid_credentials") == S.LOGIN_FREE_FAILURES
    assert results.count("rate_limited") == 30 - S.LOGIN_FREE_FAILURES
    assert count(svc.engine, LoginAttempt, LoginAttempt.kind == "login") == S.LOGIN_FREE_FAILURES


def test_parallel_registrations_respect_the_client_limit(svc, monkeypatch):
    created = []
    real = svc._create_user

    def slow_create(*a, **kw):
        time.sleep(0.05)
        created.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(svc, "_create_user", slow_create)
    i = iter(range(1000))
    lock = threading.Lock()

    def one():
        with lock:
            k = next(i)
        svc.register(f"user{k}@example.com", GOOD, "9.9.9.9")

    results = _hammer(20, one)
    assert len(created) == S.REGISTER_CLIENT_MAX == results.count("ok")
    assert results.count("rate_limited") == 20 - S.REGISTER_CLIENT_MAX


def test_parallel_password_change_guesses_are_bounded(svc, monkeypatch):
    uid = svc.create_user("a@example.com", GOOD).id
    calls = _slow_counting_verify(monkeypatch)
    _hammer(20, lambda: svc.change_password(uid, "wrong-password-xx", "maple-quartz-lantern-58"))
    assert len(calls) == S.LOGIN_FREE_FAILURES


def test_successful_login_is_recorded_as_success(svc):
    svc.create_user("a@example.com", GOOD)
    svc.authenticate("a@example.com", GOOD, "1.1.1.1")
    assert count(svc.engine, LoginAttempt, LoginAttempt.success.is_(True)) == 1
    assert count(svc.engine, LoginAttempt, LoginAttempt.success.is_(False)) == 0


def test_argon2_concurrency_is_capped():
    state = {"now": 0, "max": 0}
    lock = threading.Lock()

    class SlowHasher:
        def _work(self):
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            time.sleep(0.03)
            with lock:
                state["now"] -= 1

        def hash(self, secret):
            self._work()
            return "$argon2id$fake"

        def verify(self, encoded, secret):
            self._work()
            return True

    old = pw.hasher()
    pw.set_hasher(SlowHasher())
    try:
        threads = [threading.Thread(target=pw.verify_password, args=("$argon2id$x", "p")) for _ in range(12)]
        threads += [threading.Thread(target=pw.hash_password, args=("p",)) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    finally:
        pw.set_hasher(old)
    assert 1 < state["max"] <= S.ARGON2_MAX_CONCURRENT


# ----------------------------------------------------------------------------- 3. Host allow-list

def test_foreign_host_is_rejected(migrated_db, monkeypatch):
    from shots.web.app import _default_app

    monkeypatch.setenv("SHOTS_DB", str(migrated_db))
    monkeypatch.delenv("SHOTS_ALLOWED_HOSTS", raising=False)
    app = _default_app()
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        assert c.get("/about").status_code == 200
        assert c.get("/about", headers={"host": "localhost:8000"}).status_code == 200
        assert c.get("/about", headers={"host": "evil.example"}).status_code == 400
        assert c.get("/about", headers={"host": "rebind.attacker.test:8000"}).status_code == 400


def test_allowed_hosts_from_env(migrated_db, monkeypatch):
    from shots.auth.http_security import allowed_hosts
    from shots.web.app import _default_app

    assert allowed_hosts("") == ["127.0.0.1", "localhost"]
    assert allowed_hosts(" shots.example.com, 127.0.0.1 ") == ["shots.example.com", "127.0.0.1"]
    monkeypatch.setenv("SHOTS_DB", str(migrated_db))
    monkeypatch.setenv("SHOTS_ALLOWED_HOSTS", "shots.example.com")
    with TestClient(_default_app(), base_url="https://shots.example.com") as c:
        assert c.get("/about").status_code == 200
        assert c.get("/about", headers={"host": "127.0.0.1"}).status_code == 400


# ----------------------------------------------------------------------------- 4. pre-login CSRF and purge

def test_anonymous_form_views_create_no_session_rows(migrated_db, svc):
    with TestClient(real_app(migrated_db, svc), follow_redirects=False) as c:
        for i in range(50):
            c.cookies.clear()
            page = "/login" if i % 2 else "/register"
            r = c.get(page)
            assert r.status_code == 200 and form_token(r.text)
    assert count(svc.engine, UserSession) == 0


def test_login_and_register_with_prelogin_token(migrated_db, svc):
    with TestClient(real_app(migrated_db, svc), follow_redirects=False) as c:
        tok = form_token(c.get("/register").text)
        r = c.post("/auth/register", data={"email": "a@example.com", "password": GOOD, "csrf_token": tok})
        assert r.status_code == 303 and r.headers["location"] == "/"
        cleared = [v for v in r.headers.get_list("set-cookie") if v.startswith(S.PRELOGIN_COOKIE + "=")]
        assert cleared and "max-age=0" in cleared[0].lower()  # pre-login cookie dropped on sign-in
        assert count(svc.engine, UserSession) == 1
        c.cookies.clear()
        tok = form_token(c.get("/login").text)
        r = c.post("/auth/login", data={"email": "a@example.com", "password": GOOD, "csrf_token": tok})
        assert r.headers["location"] == "/" and c.get("/account").status_code == 200


@pytest.mark.parametrize("path,page", [("/auth/login", "/login"), ("/auth/register", "/register")])
def test_prelogin_token_rejections(migrated_db, svc, clock, path, page):
    data = {"email": "a@example.com", "password": GOOD}
    with TestClient(real_app(migrated_db, svc), follow_redirects=False) as c:
        tok = form_token(c.get(page).text)
        csrf_fail = f"{page}?error=csrf"
        assert c.post(path, data=data).headers["location"] == csrf_fail  # missing
        assert c.post(path, data={**data, "csrf_token": tok + "x"}).headers["location"] == csrf_fail
        assert c.post(path, data={**data, "csrf_token": "123.abc"}).headers["location"] == csrf_fail
        stamp, mac = tok.split(".")
        forged = f"{int(stamp) + 60}.{mac}"  # MAC covers the issue time
        assert c.post(path, data={**data, "csrf_token": forged}).headers["location"] == csrf_fail
        nonce = c.cookies.get(S.PRELOGIN_COOKIE)
        c.cookies.set(S.PRELOGIN_COOKIE, svc.new_prelogin_nonce())  # token of another browser
        assert c.post(path, data={**data, "csrf_token": tok}).headers["location"] == csrf_fail
        c.cookies.set(S.PRELOGIN_COOKIE, nonce)
        clock.advance(seconds=S.PRELOGIN_CSRF_TTL.total_seconds())  # expired
        assert c.post(path, data={**data, "csrf_token": tok}).headers["location"] == csrf_fail
        assert c.post(path, data={**data, "csrf_token": tok},
                      headers={"Origin": "https://evil.example"}).headers["location"] == csrf_fail
    assert count(svc.engine, LoginAttempt) == 0  # rejected before any password work


def test_prelogin_token_not_accepted_for_signed_in_actions(migrated_db, svc):
    with TestClient(real_app(migrated_db, svc), follow_redirects=False) as c:
        tok = form_token(c.get("/register").text)
        c.post("/auth/register", data={"email": "a@example.com", "password": GOOD, "csrf_token": tok})
        stale = svc.prelogin_csrf(svc.new_prelogin_nonce())
        r = c.post("/auth/logout", data={"csrf_token": stale})
        assert r.headers["location"] == "/account?error=csrf"


def test_prelogin_token_service(svc, clock):
    nonce = svc.new_prelogin_nonce()
    tok = svc.prelogin_csrf(nonce)
    assert svc.check_prelogin_csrf(nonce, tok)
    assert not svc.check_prelogin_csrf(svc.new_prelogin_nonce(), tok)
    assert not svc.check_prelogin_csrf(None, tok) and not svc.check_prelogin_csrf("short", tok)
    assert not svc.check_prelogin_csrf(nonce, None) and not svc.check_prelogin_csrf(nonce, "")
    clock.advance(seconds=S.PRELOGIN_CSRF_TTL.total_seconds() - 1)
    assert svc.check_prelogin_csrf(nonce, tok)
    clock.advance(seconds=1)
    assert not svc.check_prelogin_csrf(nonce, tok)
    other = AuthService(svc.engine, secret=b"x" * 32, clock=clock)
    assert not other.check_prelogin_csrf(nonce, svc.prelogin_csrf(nonce))  # server key matters


def test_worker_cycle_purges_auth_tables(migrated_db, monkeypatch, tmp_path):
    from shots.db.models import JobRun
    from shots.worker import cycle
    from shots.worker.lock import WorkerLock
    from tests.test_worker_cycle import FakeUpdate

    monkeypatch.setenv("SHOTS_DB", str(migrated_db))
    old = datetime(2020, 1, 1)
    engine = make_engine(migrated_db)
    try:
        with session_scope(engine) as s:
            s.add(UserSession(id_hash="a" * 64, user_id=None, csrf_hash="b" * 64, created_at=old,
                              last_seen_at=old, expires_at=old + timedelta(hours=2)))
            s.add(LoginAttempt(kind="login", account_hash="c" * 64, client_hash=None, success=False, at=old))
        assert cycle.run_once(lock=WorkerLock(tmp_path / "w.lock"), update=FakeUpdate()) == 0
        assert count(engine, UserSession) == 0 and count(engine, LoginAttempt) == 0
        with engine.connect() as c:
            w = c.execute(select(JobRun.counts).where(JobRun.job == "worker")).scalar_one()
        assert w["purge"] == {"sessions": 1, "attempts": 1}
    finally:
        engine.dispose()


# ----------------------------------------------------------------------------- 5 + 7. server options

def test_server_options_defaults_and_env(monkeypatch):
    from shots.web import __main__ as webmain

    for k in ("SHOTS_PROXY_HEADERS", "SHOTS_FORWARDED_ALLOW_IPS", "SHOTS_ACCESS_LOG"):
        monkeypatch.delenv(k, raising=False)
    o = webmain.server_options()
    assert o["host"] == "127.0.0.1" and o["proxy_headers"] is False and o["access_log"] is False
    assert o["forwarded_allow_ips"] == "127.0.0.1"
    monkeypatch.setenv("SHOTS_PROXY_HEADERS", "1")
    monkeypatch.setenv("SHOTS_FORWARDED_ALLOW_IPS", "10.0.0.5")
    monkeypatch.setenv("SHOTS_ACCESS_LOG", "1")
    o = webmain.server_options()
    assert o["proxy_headers"] is True and o["forwarded_allow_ips"] == "10.0.0.5" and o["access_log"] is True

    seen = {}
    monkeypatch.setattr(webmain.uvicorn, "run", lambda target, **kw: seen.update(kw, target=target))
    monkeypatch.delenv("SHOTS_PROXY_HEADERS")
    monkeypatch.delenv("SHOTS_ACCESS_LOG")
    webmain.main(["--port", "8123"])
    assert seen["port"] == 8123 and seen["proxy_headers"] is False and seen["access_log"] is False


def test_run_web_script_does_not_enable_access_log():
    text = (Path(__file__).resolve().parents[1] / "deploy" / "run_web.ps1").read_text(encoding="utf-8")
    assert "--access-log" not in text and "--proxy-headers" not in text


# ----------------------------------------------------------------------------- 6. security headers

def _assert_base_headers(r):
    h = r.headers
    csp = h["content-security-policy"]
    for part in ("default-src 'self'", "script-src 'self'", "frame-ancestors 'none'", "base-uri 'self'",
                 "form-action 'self'", "object-src 'none'"):
        assert part in csp
    assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "same-origin" and "camera=()" in h["permissions-policy"]


def test_security_headers(migrated_db, svc):
    with TestClient(real_app(migrated_db, svc), follow_redirects=False) as c:
        home = c.get("/")
        _assert_base_headers(home)
        assert "cookie" in home.headers["vary"].lower() and "no-store" not in home.headers.get("cache-control", "")
        assert "strict-transport-security" not in home.headers
        _assert_base_headers(c.get("/static/app.js"))
        _assert_base_headers(c.get("/match/nope", headers={"accept": "text/html"}))
        for page in ("/login", "/register"):
            r = c.get(page)
            assert r.headers["cache-control"] == "no-store, private" and "cookie" in r.headers["vary"].lower()
        tok = form_token(c.get("/register").text)
        r = c.post("/auth/register", data={"email": "a@example.com", "password": GOOD, "csrf_token": tok})
        assert r.headers["cache-control"] == "no-store, private"
        for page in ("/", "/account", "/track", "/about"):  # signed in: never cacheable
            r = c.get(page)
            assert r.status_code == 200 and r.headers["cache-control"] == "no-store, private", page
        assert "no-store" not in c.get("/static/app.css").headers.get("cache-control", "")
    with TestClient(real_app(migrated_db, svc), base_url="https://testserver") as c:
        assert c.get("/about").headers["strict-transport-security"].startswith("max-age=")


def test_templates_have_no_inline_scripts_or_handlers():
    """CSP script-src 'self' (no 'unsafe-inline') must not break the app: every script is external."""
    for path in TEMPLATES.glob("*.html"):
        html = path.read_text(encoding="utf-8")
        for tag in re.findall(r"<script\b[^>]*>", html, flags=re.I):
            assert "src=" in tag, f"inline script in {path.name}"
        assert not re.search(r"\son[a-z]+\s*=", html, flags=re.I), f"inline event handler in {path.name}"
        assert "javascript:" not in html.lower()


def test_csp_in_a_browser(migrated_db, svc):
    """With the CSP enforced (no bypass), the app's own pages raise no CSP violation, boot.js and
    app.js run, and the inline-styled probability bars still render."""
    sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    import socket

    import uvicorn

    app = real_app(migrated_db, svc)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        with sync_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:  # browsers not installed
                pytest.skip(f"Playwright Chromium not available: {exc}")
            page = browser.new_page()
            problems = []
            page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: problems.append(str(e)))
            base = f"http://127.0.0.1:{port}"
            for path in ("/", "/match/a", "/track", "/about", "/pricing", "/login", "/register"):
                r = page.goto(base + path)
                assert r.status == 200 and "script-src 'self'" in r.headers["content-security-policy"], path
                assert page.evaluate("document.documentElement.classList.contains('js')"), path  # boot.js ran
            page.goto(base + "/")
            assert page.evaluate("document.querySelector('output[for]').getAttribute('aria-live')") == "off"  # app.js
            width = page.evaluate("getComputedStyle(document.querySelector('.pbar i')).width")
            assert width not in ("0px", "auto"), width  # style="width:..%" bars still apply
            browser.close()
        assert not [m for m in problems if "Content Security Policy" in m or "Refused" in m], problems
    finally:
        server.should_exit = True
        t.join(timeout=5)
