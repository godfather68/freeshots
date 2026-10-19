"""football-data.co.uk drops connections at times: downloads retry, then fall back to cached files."""

from datetime import date

import pandas as pd
import pytest
import requests

from shots.ingest import football_data as fd
from shots.predict import scan


class FakeResponse:
    def __init__(self, status=200, content=b"", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fd.time, "sleep", lambda s: None)


def scripted(monkeypatch, outcomes):
    """requests.get returns/raises the scripted outcomes in order; returns the call log."""
    calls = []

    def fake_get(url, timeout=None, headers=None):
        calls.append(url)
        out = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(fd.requests, "get", fake_get)
    return calls


def test_transient_errors_are_retried_then_succeed(monkeypatch):
    calls = scripted(monkeypatch, [requests.ConnectionError("ssl eof"), FakeResponse(503), FakeResponse(200, b"ok")])
    assert fd.http_get("https://example.test/x").content == b"ok"
    assert len(calls) == 3


def test_gives_up_after_all_attempts(monkeypatch):
    calls = scripted(monkeypatch, [requests.Timeout("slow")])
    with pytest.raises(requests.Timeout):
        fd.http_get("https://example.test/x")
    assert len(calls) == len(fd.RETRY_DELAYS_S) + 1


def test_client_errors_are_not_retried(monkeypatch):
    calls = scripted(monkeypatch, [FakeResponse(404)])
    assert fd.http_get("https://example.test/x").status_code == 404
    assert len(calls) == 1


def test_fetch_keeps_the_cached_current_season_file_when_refresh_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(fd, "RAW_DIR", tmp_path)
    season = fd.season_code(fd.current_season_start())
    cached = tmp_path / season / "E0.csv"
    cached.parent.mkdir(parents=True)
    cached.write_text("Div,Date\n")

    class FailingSession:
        headers = {}

        def get(self, url, timeout=None, headers=None):
            raise requests.ConnectionError("dropped")

    monkeypatch.setattr(fd.requests, "Session", FailingSession)
    paths = fd.fetch(first_season=fd.current_season_start(), divisions=["E0", "E1"], refresh_current=True)
    assert paths == [cached]                       # E0 kept from cache, E1 (never downloaded) skipped
    assert len(fd.LAST_FETCH_FAILURES) == 2
    assert cached.read_text() == "Div,Date\n"      # cache untouched


def test_fixtures_fall_back_to_the_latest_cached_copy(monkeypatch, tmp_path):
    monkeypatch.setattr(fd, "RAW_DIR", tmp_path)
    folder = tmp_path / "fixtures"
    folder.mkdir()
    (folder / "fixtures_2026-10-10.csv").write_text("Div,Date,Time,HomeTeam,AwayTeam\nE0,24/10/2026,15:00,Arsenal,Chelsea\n")
    (folder / "fixtures_2026-10-30.csv").write_text("Div,Date,Time,HomeTeam,AwayTeam\n")  # newer than today: ignored
    scripted(monkeypatch, [requests.ConnectionError("dropped")])
    fx = scan._fetch_fixtures(date(2026, 10, 19))
    assert list(zip(fx["home_team"], fx["away_team"])) == [("Arsenal", "Chelsea")]


def test_fixtures_without_any_cache_give_an_empty_frame(monkeypatch, tmp_path):
    monkeypatch.setattr(fd, "RAW_DIR", tmp_path)
    scripted(monkeypatch, [requests.ConnectionError("dropped")])
    assert isinstance(scan._fetch_fixtures(date(2026, 10, 19)), pd.DataFrame)
    assert scan._fetch_fixtures(date(2026, 10, 19)).empty


def test_circuit_breaker_stops_calling_a_dead_server(monkeypatch, tmp_path):
    monkeypatch.setattr(fd, "RAW_DIR", tmp_path)
    calls = []

    class DeadSession:
        headers = {}

        def get(self, url, timeout=None, headers=None):
            calls.append(url)
            raise requests.ConnectionError("dropped")

    monkeypatch.setattr(fd.requests, "Session", DeadSession)
    divisions = ["E0", "E1", "E2", "E3", "D1", "I1"]
    fd.fetch(first_season=fd.current_season_start(), divisions=divisions, refresh_current=True)
    per_file = len(fd.RETRY_DELAYS_S) + 1
    assert len(calls) == fd.MAX_CONSECUTIVE_FAILURES * per_file   # files 4-6 never hit the server
    assert len(fd.LAST_FETCH_FAILURES) == len(divisions)
