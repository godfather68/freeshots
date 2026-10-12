"""Web app on the database: the entitlements hook is enforced server-side for every route."""

from fastapi.testclient import TestClient

from shots.db.access import Entitlements
from shots.web.app import create_app
from shots.web import filters as flt
from tests.test_web import TODAY, make_board, shown_ids, write_board
from tests.test_web_team import with_team

TOP5_ONLY = Entitlements(can_view_all_leagues=False, allowed_leagues=frozenset(flt.TOP5), max_days_ahead=3,
                         team_predictions=False, export=False, track_detail=False)


def test_entitlements_hook_limits_every_route(migrated_db):
    write_board(migrated_db, with_team(make_board()))
    c = TestClient(create_app(migrated_db, today=lambda: TODAY, entitlements=lambda request: TOP5_ONLY))
    html = c.get("/").text
    assert shown_ids(html) == ["a", "c"]  # b, d: second tiers; nothing beyond 3 days
    assert "Italy Serie B" not in html and 'name="side"' not in html  # no team filter without the entitlement
    assert shown_ids(c.get("/?league=Italy+Serie+B").text) == ["a", "c"]  # not visible: ignored, not widened
    assert shown_ids(c.get("/?grp=second").text) == []
    assert c.get("/match/b").status_code == 404 and c.get("/match/d").status_code == 404
    m = c.get("/match/a?tn=14")
    assert m.status_code == 200 and "teamcurve" not in m.text
    full = TestClient(create_app(migrated_db, today=lambda: TODAY))
    assert shown_ids(full.get("/").text) == ["a", "b", "c", "d"] and "teamcurve" in full.get("/match/a").text


def _snapshot(path, skip=frozenset({"sessions", "login_attempts", "users", "subscriptions"})) -> dict:
    """Full content of every table except the auth tables (raw sqlite3, read-only)."""
    import sqlite3

    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                         "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: c.execute(f'SELECT * FROM "{n}" ORDER BY rowid').fetchall() for n in names if n not in skip}
    finally:
        c.close()


def test_web_requests_never_write_football_or_prediction_data(migrated_db):
    """Invariant 5: the web app writes only the auth tables (sessions, login_attempts, users,
    subscriptions); every other table is byte-for-byte unchanged by pages and auth flows."""
    import re

    from shots.auth.web import entitlements_provider, install_auth
    from tests.test_web import back_frame, seed_backfill

    write_board(migrated_db, with_team(make_board()))
    seed_backfill(migrated_db, back_frame())
    before = _snapshot(migrated_db)
    assert before["predictions"] and before["matches"] and before["prediction_runs"]
    app = create_app(migrated_db, today=lambda: TODAY, entitlements=entitlements_provider)
    install_auth(app, migrated_db)
    pw = "correct horse battery staple 42"
    with TestClient(app, follow_redirects=False) as c:
        for url in ("/", "/?sort=prob&n=28", "/match/a", "/match/zzz", "/track", "/track?src=live",
                    "/about", "/pricing", "/login", "/register", "/plans"):
            assert c.get(url).status_code in (200, 303, 308, 404), url
        tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/register").text).group(1)
        r = c.post("/auth/register", data={"email": "fan@example.com", "password": pw, "csrf_token": tok})
        assert r.status_code == 303 and r.headers["location"] == "/"
        for url in ("/", "/match/a", "/track", "/account"):
            assert c.get(url).status_code == 200, url
        tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/account").text).group(1)
        c.post("/auth/logout", data={"csrf_token": tok})
        tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text).group(1)
        c.post("/auth/login", data={"email": "fan@example.com", "password": "wrong-password-xx", "csrf_token": tok})
    after = _snapshot(migrated_db)
    assert after == before
