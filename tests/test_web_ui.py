"""Web UI shell: guide, glossary, freshness, presets, locked states, figures with tables, 404 page."""

import re
from html import unescape
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from shots.db import migrate
from shots.db.access import Entitlements
from shots.web import filters as flt
from shots.web.app import create_app
from shots.web.fresh import ago, freshness
from shots.web.glossary import TERMS
from tests.test_web import META, TODAY, make_board, write_board
from tests.test_web_team import with_team

NOW = datetime(2026, 10, 8, 11, 30)
PRO_LOCKED = Entitlements(can_view_all_leagues=False, allowed_leagues=frozenset(flt.TOP5), max_days_ahead=3,
                          team_predictions=False, export=False, track_detail=False)


def make(tmp_path, ents=None, team=True):
    path = tmp_path / "shots.db"
    migrate.upgrade(path)
    write_board(path, with_team(make_board()) if team else make_board())
    app = create_app(path, today=lambda: TODAY, entitlements=(lambda r: ents) if ents else None)
    app.state.clock = lambda: NOW
    return TestClient(app)


@pytest.fixture
def full(tmp_path):
    return make(tmp_path)


@pytest.fixture
def locked(tmp_path):
    return make(tmp_path, PRO_LOCKED)


PAGES = ["/", "/match/a", "/track", "/about", "/plans", "/?p=90"]


@pytest.mark.parametrize("url", PAGES)
def test_landmarks_and_headings(full, url):
    t = full.get(url).text
    assert 'class="skip" href="#main"' in t and '<main id="main"' in t
    assert "<header" in t and "<footer" in t and 'aria-label="Main"' in t
    assert t.count("<h1") == 1 and '<html lang="en">' in t
    assert "https://" not in re.sub(r"<code>.*?</code>", "", t)  # nothing fetched from outside: works offline


def test_nav_has_all_pages_and_placeholders(full):
    t = full.get("/").text
    for label in ("Matches", "Track record", "How it works", "Pricing", "Quick guide"):
        assert label in t
    assert 'aria-current="page"' in t


def test_guide_is_a_page_section_with_three_steps(full):
    t = full.get("/").text
    assert 'id="guide"' in t and t.count('<ol class="steps">') == 1
    assert len(re.findall(r"<h3>", re.search(r'<ol class="steps">.*?</ol>', t, re.S).group(0))) == 3
    assert "70% chance of 24+ shots" in t and "80% sure" in t
    assert 'href="/#guide"' in t  # re-openable from the header, no JS needed
    assert "guide-off" not in t  # the dismissal is applied client-side by boot.js only
    boot = full.get("/static/boot.js").text
    assert "localStorage" in boot and "try" in boot and "catch" in boot
    assert "try" in full.get("/static/app.js").text


def test_guide_threshold_follows_filter(full):
    assert "70% chance of 31+ shots" in full.get("/?n=31").text


def test_glossary_terms_everywhere(full):
    home = full.get("/").text
    assert home.count('<details class="term">') >= 8
    assert "What does &ldquo;No odds&rdquo; mean?" in home and "What does &ldquo;Experimental team numbers&rdquo; mean?" in home
    about = full.get("/about").text
    assert 'id="glossary"' in about
    for t, text in TERMS.values():
        assert t in unescape(about) and text in unescape(about)


def test_empty_state_says_what_to_do(full):
    t = full.get("/?p=90&odds=1").text
    assert "No matches fit these filters" in t and "what you can do next" in t
    assert re.search(r'Remove &ldquo;P ≥ 90%&rdquo;', t) and "Reset filters" in t


def test_presets_active_and_team_preset(full):
    t = full.get("/").text
    for label in ("Likely high-shot games", "Big leagues this weekend", "Team over 15 shots"):
        assert label in t
    href = re.search(r'href="(/\?[^"]*)"[^>]*>Big leagues this weekend', t).group(1)
    assert href == "/?grp=top5&amp;when=weekend"
    t2 = full.get("/?grp=top5&when=weekend").text
    assert re.search(r'aria-current="true">Big leagues this weekend', t2)
    # the preset keeps the user's threshold
    assert "/?n=28&amp;p=70&amp;sort=prob" in full.get("/?n=28").text
    # and each preset url really filters
    assert 'name="side"' in full.get("/?side=either&tn=15&tp=70").text


def test_old_board_has_no_team_preset(tmp_path):
    t = make(tmp_path, team=False).get("/").text
    assert "Team over 15 shots" not in t


def test_live_region_and_drawer_hooks(full):
    t = full.get("/").text
    assert 'id="result-status"' in t and 'role="status"' in t and 'aria-live="polite"' in t
    assert 'data-status="4 of 4 matches shown.' in t
    assert 'class="ftoggle"' in t and 'aria-controls="filters"' in t and 'class="fclose"' in t
    assert 'aria-labelledby="filters-title"' in t


def test_every_control_has_a_label(full):
    t = full.get("/").text
    for name in ("n", "p", "tn", "tp", "q", "sort"):
        assert f'for="{name}"' in t and f'id="{name}"' in t
    for m in re.finditer(r'<input type="number"[^>]*>', t):
        assert "aria-label=" in m.group(0)


# ---------------------------------------------------------------- freshness

def test_ago_phrases():
    assert ago(10) == "just now" and ago(20 * 60) == "20 min ago" and ago(2 * 3600) == "2 h ago"
    assert ago(3 * 86400) == "3 days ago"


def test_freshness_levels():
    meta = dict(META, built_at="2026-10-08T09:30:00")
    f = freshness(meta, {"latest_result": None, "last_job": {"status": "ok"}}, NOW)
    assert f["text"] == "Updated 2 h ago · results to 5 Oct" and f["level"] == "ok"
    stale = freshness(meta, None, datetime(2026, 10, 11, 9, 30))
    assert stale["level"] == "stale" and "3 days ago" in stale["text"] and "two days old" in stale["detail"]
    failed = freshness(meta, {"last_job": {"status": "failed"}}, NOW)
    assert failed["level"] == "failed" and "failed" in failed["detail"]
    assert freshness({}, None, NOW) is None


def test_freshness_shown_in_header(full):
    t = full.get("/").text
    assert "Updated 2 h ago" in t and 'class="statusbar ok"' in t


def test_stale_warning_in_header(tmp_path):
    c = make(tmp_path)
    c.app.state.clock = lambda: datetime(2026, 10, 12, 9, 30)
    t = c.get("/").text
    assert 'class="statusbar stale"' in t and "Warning:" in t


# ---------------------------------------------------------------- locked states

def test_locked_board_shows_friendly_locks_without_leaking(locked):
    t = locked.get("/").text
    assert "Available on" in t and 'href="/pricing"' in t
    assert "Italy Serie B" not in t and "Bari" not in t and "Hull" not in t
    assert "What your plan includes" in t and "Matches in the next 3 days" in t
    assert 'name="side"' not in t and "Team over 15 shots" in t  # offered as locked, not usable
    assert not re.search(r'href="/\?side=either', t)
    assert 'value="7d"' not in t and 'value="30d"' not in t and 'value="3d"' in t
    assert "Next 3 days" in t


def test_locked_requested_league_is_explained(locked):
    t = locked.get("/?league=Italy+Serie+B").text
    assert "not part of your current plan" in t and "Bari" not in t


def test_locked_team_card_on_match_page(locked):
    t = locked.get("/match/a").text
    assert "Shots by team" in t and "Available on" in t and "teamcurve" not in t


def test_hidden_match_is_a_friendly_404(locked):
    r = locked.get("/match/b")
    assert r.status_code == 404 and "could not find that page" in r.text and "Bari" not in r.text


def test_locked_track_detail(locked):
    assert "Per-league breakdown" in locked.get("/track").text


def test_full_access_has_no_locks(full):
    t = full.get("/").text
    assert "lockmark" not in t.replace(".lockmark", "") and "What your plan includes" not in t


# ---------------------------------------------------------------- figures and tables

def test_match_charts_have_captions_text_and_tables(full):
    t = full.get("/match/a?n=28").text
    assert t.count("<figure") == 2 and t.count("<figcaption") == 2
    assert "How to read it" in t and "Show these numbers as a table" in t
    assert re.search(r'<th scope="row">28\+</th><td>66%</td>', t)
    assert t.count("<caption") >= 4
    assert 'role="img"' in t and "<desc" in t


def test_track_chart_figure(tmp_path):
    from tests.test_web import back_frame, make_client

    c = make_client(tmp_path, back=back_frame())
    t = c.get("/track").text
    assert "<figure" in t and "How to read it" in t and 'scope="col"' in t


def test_plans_page_and_404(full):
    assert "Your access right now" in full.get("/pricing").text
    assert full.get("/plans", follow_redirects=False).status_code == 308
    r = full.get("/nope")
    assert r.status_code == 404 and "could not find that page" in r.text
    assert full.get("/nope", headers={"accept": "application/json"}).status_code == 404
