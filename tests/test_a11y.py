"""Browser accessibility audits (axe-core + keyboard smoke tests) on a live server with a seeded temp DB.

Needs `pip install playwright` and `python -m playwright install chromium`; skipped otherwise.
Run only these with `python -m pytest -m a11y`. axe-core is vendored (tests/a11y/vendor/, MPL-2.0).
"""

import json
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.a11y
sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from shots.db import migrate  # noqa: E402
from shots.db.access import Entitlements  # noqa: E402
from shots.web import filters as flt  # noqa: E402
from shots.web.app import create_app  # noqa: E402
from tests.test_web import TODAY, back_frame, make_board, seed_backfill, write_board  # noqa: E402
from tests.test_web_team import with_team  # noqa: E402

AXE = Path(__file__).parent / "a11y" / "vendor" / "axe.min.js"
NOW = datetime(2026, 10, 8, 11, 30)
RESTRICTED = Entitlements(can_view_all_leagues=False, allowed_leagues=frozenset(flt.TOP5), max_days_ahead=3,
                          team_predictions=False, export=False, track_detail=False)
VIEWPORTS = {"mobile": {"width": 375, "height": 800}, "desktop": {"width": 1280, "height": 900}}
SCHEMES = ("light", "dark")
PAGES = {
    "board": "/", "board-ranked": "/?sort=prob&p=60&n=28", "board-empty": "/?p=90",
    "board-team": "/?side=either&tn=14&tp=50", "match": "/match/a?n=28", "match-cold": "/match/b",
    "track": "/track", "about": "/about", "pricing": "/pricing", "not-found": "/match/zzz",
}


def _serve(app):
    """Run the app with uvicorn in a thread on a free port; returns (base_url, stop)."""
    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "server did not start"

    def stop():
        server.should_exit = True
        t.join(timeout=5)

    return f"http://127.0.0.1:{port}", stop


def _seed(tmp_path_factory):
    path = tmp_path_factory.mktemp("a11y") / "shots.db"
    migrate.upgrade(path)
    write_board(path, with_team(make_board()))
    seed_backfill(path, back_frame())
    return path


@pytest.fixture(scope="module")
def full_url(tmp_path_factory):
    app = create_app(_seed(tmp_path_factory), today=lambda: TODAY)
    app.state.clock = lambda: NOW
    url, stop = _serve(app)
    yield url
    stop()


@pytest.fixture(scope="module")
def locked_url(tmp_path_factory):
    app = create_app(_seed(tmp_path_factory), today=lambda: TODAY, entitlements=lambda request: RESTRICTED)
    app.state.clock = lambda: NOW
    url, stop = _serve(app)
    yield url
    stop()


PW = "correct horse battery staple 42"
AUTH_PAGES = {"login": "/login", "login-error": "/login?error=invalid_credentials&next=/match/a", "register": "/register",
              "register-error": "/register?error=weak_password&reason=too_short", "pricing": "/pricing",
              "board-anonymous": "/", "match-anonymous": "/match/a"}


@pytest.fixture(scope="module")
def auth_url(tmp_path_factory):
    from shots.auth.web import entitlements_provider, install_auth

    path = _seed(tmp_path_factory)
    app = create_app(path, today=lambda: TODAY, entitlements=entitlements_provider)
    svc = install_auth(app, path)
    svc.create_user("pro@example.com", PW, plan="pro")
    app.state.clock = lambda: NOW
    url, stop = _serve(app)
    yield url
    stop()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browsers not installed
            pytest.skip(f"Playwright Chromium not available: {exc}")
        yield b
        b.close()


def new_page(browser, scheme="light", view="desktop"):
    # bypass_csp: run_axe injects axe-core as an inline script, which the app's CSP (script-src 'self')
    # rightly blocks. The app under CSP is checked in tests/test_auth_hardening.py::test_csp_in_a_browser.
    ctx = browser.new_context(color_scheme=scheme, viewport=VIEWPORTS[view], reduced_motion="reduce",
                              bypass_csp=True)
    return ctx, ctx.new_page()


def run_axe(page):
    page.add_script_tag(path=str(AXE))
    return page.evaluate("""async () => {
      const r = await axe.run(document, {runOnly: {type: 'tag',
        values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice']}});
      return r.violations.map(v => ({id: v.id, impact: v.impact, help: v.help,
        nodes: v.nodes.slice(0, 4).map(n => n.target.join(' ') + ' :: ' + (n.failureSummary || '').slice(0, 300))}));
    }""")


def serious(violations):
    return [v for v in violations if v["impact"] in ("serious", "critical")]


def assert_clean(page, label):
    bad = serious(run_axe(page))
    assert not bad, f"{label}: " + json.dumps(bad, indent=1)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), f"{label}: horizontal scroll"


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("view", VIEWPORTS)
@pytest.mark.parametrize("name", PAGES)
def test_axe_every_page(browser, full_url, name, view, scheme):
    ctx, page = new_page(browser, scheme, view)
    try:
        page.goto(full_url + PAGES[name])
        assert_clean(page, f"{name} {view} {scheme}")
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("view", VIEWPORTS)
@pytest.mark.parametrize("path", ["/", "/match/a", "/track", "/pricing", "/?league=Italy+Serie+B"])
def test_axe_locked_states(browser, locked_url, path, view, scheme):
    ctx, page = new_page(browser, scheme, view)
    try:
        page.goto(locked_url + path)
        if path != "/pricing":
            assert page.locator(".lockmark").count() >= 1, "restricted plan must show a locked state"
        assert_clean(page, f"locked {path} {view} {scheme}")
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", SCHEMES)
def test_axe_with_popover_and_drawer_open(browser, full_url, scheme):
    ctx, page = new_page(browser, scheme, "mobile")
    try:
        page.goto(full_url + "/")
        page.locator("#board-live details.term > summary").first.click()
        assert page.locator("details.term[open]").count() == 1
        assert_clean(page, f"popover open {scheme}")
        page.keyboard.press("Escape")
        page.locator(".ftoggle").click()
        assert page.locator("#filters.open").count() == 1
        assert_clean(page, f"drawer open {scheme}")
    finally:
        ctx.close()


def test_keyboard_drawer_and_glossary(browser, full_url):
    ctx, page = new_page(browser, "light", "mobile")
    try:
        page.goto(full_url + "/")
        toggle = page.locator(".ftoggle")
        toggle.focus()
        page.keyboard.press("Enter")
        assert page.locator("#filters.open").count() == 1
        assert page.evaluate("document.activeElement.classList.contains('fclose')"), "focus moves into the drawer"
        # tab through the whole drawer: focus must stay inside the form (page behind is inert) and never get stuck
        seen = set()
        for _ in range(80):
            page.keyboard.press("Tab")
            inside, key = page.evaluate("""() => {
              const a = document.activeElement;
              return [!!a.closest('#filters') || a === document.body,
                      a.tagName + '|' + (a.name || a.className || a.id) + '|' + (a.value || '')];
            }""")
            assert inside, f"focus escaped the open drawer: {key}"
            seen.add(key)
        assert len(seen) > 10, "tab must visit the drawer controls, not loop on one element"
        page.keyboard.press("Escape")
        assert page.locator("#filters.open").count() == 0
        assert page.evaluate("document.activeElement.classList.contains('ftoggle')"), "focus returns to the toggle"
        assert page.evaluate("document.querySelectorAll('[inert]').length") == 0

        # glossary popover: open with Enter, close with Escape, focus returns to its button
        summ = page.locator("#board-live details.term > summary").first
        summ.focus()
        page.keyboard.press("Enter")
        assert page.locator("details.term[open]").count() == 1
        assert page.locator("details.term[open] .pop").is_visible()
        page.keyboard.press("Escape")
        assert page.locator("details.term[open]").count() == 0
        assert page.evaluate("document.activeElement.tagName") == "SUMMARY"
        # opening one closes the other: no stack of popovers
        page.locator("#board-live details.term > summary").nth(0).click()
        page.locator("#board-live details.term > summary").nth(1).click()
        assert page.locator("details.term[open]").count() == 1
    finally:
        ctx.close()


def test_slider_auto_apply_keeps_focus_and_announces(browser, full_url):
    ctx, page = new_page(browser, "light", "desktop")
    try:
        page.goto(full_url + "/")
        page.locator("#n").focus()
        page.keyboard.press("ArrowRight")
        page.wait_for_function("document.getElementById('result-status').textContent.length > 0")
        assert "25+" in page.locator("#result-status").text_content()
        assert page.evaluate("document.activeElement.id") == "n", "auto-apply must not steal focus"
        assert "n=25" in page.url
        assert page.locator("#board-live .summary > div > b").nth(3).text_content() == "25+"
    finally:
        ctx.close()


def test_guide_dismiss_is_remembered_and_reopenable(browser, full_url):
    ctx, page = new_page(browser, "light", "desktop")
    try:
        page.goto(full_url + "/")
        assert page.locator("#guide").is_visible()
        page.locator(".gclose").click()
        assert not page.locator("#guide").is_visible()
        page.reload()
        assert not page.locator("#guide").is_visible(), "dismissal is remembered in localStorage"
        page.locator("[data-guide-open]").first.click()
        assert page.locator("#guide").is_visible()
        page.reload()
        assert page.locator("#guide").is_visible(), "re-opening forgets the dismissal"
    finally:
        ctx.close()


def test_guide_is_plain_section_without_javascript(browser, full_url):
    ctx = browser.new_context(java_script_enabled=False)
    page = ctx.new_page()
    try:
        page.goto(full_url + "/")
        assert page.locator("#guide").is_visible()
        assert page.locator("#guide .steps li").count() == 3
        page.locator("#board-live details.term > summary").first.click()  # native disclosure still works
        assert page.locator("details.term[open]").count() == 1
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("view", VIEWPORTS)
@pytest.mark.parametrize("name", AUTH_PAGES)
def test_axe_auth_pages(browser, auth_url, name, view, scheme):
    ctx, page = new_page(browser, scheme, view)
    try:
        page.goto(auth_url + AUTH_PAGES[name])
        assert_clean(page, f"{name} {view} {scheme}")
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("view", VIEWPORTS)
def test_axe_signed_in_account_flow(browser, auth_url, view, scheme):
    ctx, page = new_page(browser, scheme, view)
    try:
        page.goto(auth_url + "/login")
        page.get_by_label("Email address").fill("pro@example.com")
        page.get_by_label("Password").fill(PW)
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url(auth_url + "/")
        assert page.locator("form.signout").count() == 1
        assert_clean(page, f"signed-in board {view} {scheme}")
        page.goto(auth_url + "/account")
        assert "pro@example.com" in page.content()
        assert_clean(page, f"account {view} {scheme}")
        page.goto(auth_url + "/account?error=weak_password&reason=common")
        assert_clean(page, f"account error {view} {scheme}")
    finally:
        ctx.close()
