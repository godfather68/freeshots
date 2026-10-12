"""Web UI wired to the auth backend: login/register/account/pricing pages, round trips, locked-state links."""

import re

import pytest
from fastapi.testclient import TestClient

from shots.auth.web import entitlements_provider, install_auth
from shots.db import migrate
from shots.web.app import create_app
from tests.test_web import TODAY, make_board, shown_ids, write_board
from tests.test_web_team import with_team

PW = "correct horse battery staple 42"


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "shots.db"
    migrate.upgrade(path)
    write_board(path, with_team(make_board()))
    app = create_app(path, today=lambda: TODAY, entitlements=entitlements_provider)
    svc = install_auth(app, path)
    return app, svc


@pytest.fixture
def client(env):
    return TestClient(env[0])


def token(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def register(c, email="fan@example.com", pw=PW, next_=""):
    t = token(c.get("/register").text)
    return c.post("/auth/register", data={"email": email, "password": pw, "password_confirm": pw,
                                          "csrf_token": t, "next": next_}, follow_redirects=False)


def test_pages_render_anonymous(client):
    for url in ("/login", "/register", "/pricing", "/", "/about", "/track"):
        assert client.get(url).status_code == 200, url
    html = client.get("/").text
    assert 'href="/login?next=/"' in html and 'href="/register"' in html and "Sign out" not in html


def test_anonymous_gets_free_plan_and_lock_links_to_register(client):
    html = client.get("/").text
    assert shown_ids(html) == ["a", "c"]  # Free: top 5 leagues, 3 days
    assert 'href="/register?next=/"' in html and "Available on" in html
    assert 'href="/register?next=/match/a"' in client.get("/match/a").text


def test_register_round_trip_and_logout(client):
    r = register(client, next_="/match/a")
    assert r.status_code == 303 and r.headers["location"] == "/match/a"
    html = client.get("/").text
    assert "fan@example.com" in html and 'action="/auth/logout"' in html
    assert 'href="/pricing"' in html  # signed in: locks point to pricing
    acct = client.get("/account")
    assert acct.status_code == 200 and "Free" in acct.text and 'action="/auth/password"' in acct.text
    out = client.post("/auth/logout", data={"csrf_token": token(html)}, follow_redirects=False)
    assert out.status_code == 303
    assert client.get("/account", follow_redirects=False).status_code == 303


def test_login_round_trip_and_errors(client, env):
    env[1].create_user("pro@example.com", PW, plan="pro")
    t = token(client.get("/login").text)
    bad = client.post("/auth/login", data={"email": "pro@example.com", "password": "nope-nope-nope", "csrf_token": t},
                      follow_redirects=False)
    assert bad.headers["location"].startswith("/login?error=invalid_credentials")
    page = client.get(bad.headers["location"])
    assert 'role="alert"' in page.text and "Incorrect email or password." in page.text
    assert 'aria-describedby="form-error"' in page.text and 'autocomplete="current-password"' in page.text
    ok = client.post("/auth/login", data={"email": "pro@example.com", "password": PW, "csrf_token": token(page.text)},
                     follow_redirects=False)
    assert ok.status_code == 303
    assert shown_ids(client.get("/?when=30d").text) == ["a", "b", "c", "d"]  # Pro sees everything
    assert 'name="side"' in client.get("/").text  # and team filters


def test_error_message_is_never_the_raw_query(client):
    html = client.get("/login?error=%3Cscript%3Ealert(1)%3C/script%3E&ok=<b>x</b>").text
    assert "<script>alert(1)" not in html and "Something went wrong" in html and "<b>x</b>" not in html


def test_register_page_shows_rules_and_autocomplete(client):
    html = client.get("/register?error=weak_password&reason=too_short").text
    assert "at least 12 characters" in html and 'autocomplete="new-password"' in html and 'autocomplete="email"' in html
    assert 'id="pw-rules"' in html and "aria-describedby=\"pw-rules form-error\"" in html


def test_next_is_validated(client):
    html = client.get("/login?next=//evil.example").text
    assert "evil.example" not in html


def test_account_requires_login(client):
    r = client.get("/account", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?error=login_required")


def test_pricing_lists_plans_and_highlights_current(client, env):
    html = client.get("/pricing").text
    for name in ("Free", "Pro", "Premium"):
        assert name in html
    assert "Coming soon" in html and html.count("Your plan") == 1  # anonymous visitors are on Free
    register(client)
    user = env[1].find_user("fan@example.com")
    env[1].assign_plan(user.id, "pro")
    page = client.get("/pricing").text
    assert page.count("Your plan") == 1 and re.search(r'id="p-pro">Pro <span class="badge ok">Your plan', page)


def test_logged_in_page_forms_have_csrf(client):
    register(client)
    assert 'name="csrf_token"' in client.get("/account").text


def test_app_without_auth_hides_account_links(tmp_path, migrated_db):
    write_board(migrated_db, make_board())
    c = TestClient(create_app(migrated_db, today=lambda: TODAY))
    assert "Sign in" not in c.get("/").text
    assert c.get("/login").status_code == 404 and c.get("/account").status_code == 404
