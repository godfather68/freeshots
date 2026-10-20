"""Daily results checklist (shots.results_todo, .github/workflows/results-todo.yml): selection from the public
JSON, priority, league spread, links, CSV block, and the guarantee that nothing is fetched."""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from shots import results_todo as rt
from shots.ingest import cross_border as xb
from shots.ingest import football_data as fd
from shots.ingest import manual_results as mr

ROOT = Path(__file__).resolve().parents[1]
RUN = date(2026, 10, 20)
LO = 10


def curve_with(p24: float) -> list[float]:
    """A curve P(total >= n), n = 10..45, whose value at 24 is p24."""
    return [1.0] * 14 + [round(p24 * 0.9 ** k, 4) for k in range(22)]


def fx(id_, league, d, home, away, p24, ko="15:00", experimental=False, expected=27, lb80=22):
    out = {"id": id_, "league": league, "date": d, "kickoff_local": ko,
           "kickoff_utc": f"{d}T{ko}:00Z" if ko else None, "home": home, "away": away,
           "expected": expected, "lb": {"70": lb80 + 2, "80": lb80, "90": lb80 - 3}, "curve": curve_with(p24)}
    if experimental:
        out["experimental"] = True
    return out


def write_export(root: Path, fixtures: list[dict], results: list[dict] | None = None,
                 built_at: str = "2026-10-01T06:00:00Z", leagues: list[dict] | None = None) -> Path:
    d = root / "data"
    d.mkdir(parents=True, exist_ok=True)
    meta = {"schema": 1, "built_at": built_at, "thresholds": {"total": [10, 45]},
            "leagues": leagues if leagues is not None else [
                {"id": "england-premier-league", "name": "Premier League", "country": "England"},
                {"id": "uefa-champions-league", "name": "UEFA Champions League", "country": "Europe"}]}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / "board.json").write_text(json.dumps({"schema": 1, "fixtures": fixtures}), encoding="utf-8")
    (d / "results.json").write_text(json.dumps({"schema": 1, "results": results or []}), encoding="utf-8")
    return root


@pytest.fixture()
def repo(tmp_path):
    """An empty repository dir with the two result folders (no CSV rows entered yet)."""
    r = tmp_path / "repo"
    (r / "results_manual").mkdir(parents=True)
    (r / "results_provisional").mkdir()
    return r


def item(id_, league, d, p24, experimental=False, home=None):
    return rt.Item(id=id_, league=league, date=d, home=home or f"H{id_}", away=f"A{id_}",
                   kickoff_utc=f"{d}T15:00:00Z", expected=27, lb80=22, p24=p24, experimental=experimental)


# ---- selection ------------------------------------------------------------------------------------

def test_selects_played_unsettled_predictions_in_the_window(tmp_path, repo):
    fixtures = [
        fx("a", "england-premier-league", "2026-10-19", "Arsenal", "Leeds", 0.8),
        fx("b", "england-premier-league", "2026-10-18", "Fulham", "Wolves", 0.6),
        fx("settled", "england-premier-league", "2026-10-18", "Spurs", "Everton", 0.9),
        fx("settled-by-key", "england-premier-league", "2026-10-17", "Burnley", "Brentford", 0.9),
        fx("today", "england-premier-league", "2026-10-20", "Chelsea", "Villa", 0.9),
        fx("old", "england-premier-league", "2026-10-12", "Bournemouth", "Brighton", 0.9),
        fx("edge", "england-premier-league", "2026-10-13", "Newcastle", "Forest", 0.5),
    ]
    results = [{"id": "settled", "league": "england-premier-league", "date": "2026-10-18", "home": "Spurs",
                "away": "Everton"},
               {"id": "other-id", "league": "england-premier-league", "date": "2026-10-17", "home": "Burnley",
                "away": "Brentford"}]
    data = write_export(tmp_path / "site", fixtures, results)
    chosen, md = rt.build(data, RUN, repo_dir=repo)
    assert [i.id for i in chosen] == ["a", "b", "edge"]  # 7-day backlog: 13..19 Oct; run date excluded
    assert "Results to enter — 2026-10-20" in md.splitlines()[0]


def test_rows_already_entered_in_the_csvs_are_skipped(tmp_path, repo):
    data = write_export(tmp_path / "site", [
        fx("a", "england-premier-league", "2026-10-19", "Arsenal", "Leeds", 0.8),
        fx("b", "england-premier-league", "2026-10-19", "Fulham", "Wolves", 0.8),
        fx("c", "uefa-champions-league", "2026-10-15", "Lens", "Sp Lisbon", 0.8, experimental=True)])
    header = ",".join(mr.COLUMNS)
    (repo / "results_provisional" / "england-premier-league_2026-27.csv").write_text(
        f"{header}\n2026-10-19,arsenal,Leeds,14,9,5,3,1,0,https://x.example/1,2026-10-20T10:00:00Z,\n",
        encoding="utf-8")
    (repo / "results_manual" / "uefa-champions-league_2026-27.csv").write_text(
        f"{header}\n2026-10-15,Lens,Sp Lisbon,21,11,6,5,0,2,https://x.example/2,2026-10-16T10:00:00Z,\n",
        encoding="utf-8")
    chosen, _ = rt.build(data, RUN, repo_dir=repo)
    assert [i.id for i in chosen] == ["b"]


def test_official_picks_first_then_experimental_picks_then_probability():
    items = [item("low", "L1", "2026-10-19", 0.65), item("pick", "L1", "2026-10-15", 0.71),
             item("best", "L1", "2026-10-14", 0.9), item("exp", "L1", "2026-10-19", 0.95, experimental=True),
             item("tie-new", "L1", "2026-10-19", 0.65)]
    assert [i.id for i in rt.select(items, RUN)] == ["best", "pick", "exp", "low", "tie-new"]  # ties: by home name
    # same probability: newest first
    a, b = item("x", "L1", "2026-10-19", 0.6), item("y", "L1", "2026-10-14", 0.6)
    assert [i.id for i in rt.select([b, a], RUN)] == ["x", "y"]
    assert not item("e", "L1", "2026-10-19", 0.95, experimental=True).official_pick


def test_leagues_are_interleaved_round_robin():
    items = [item(f"p{n}", "big", "2026-10-18", 0.95 - n / 100) for n in range(10)]
    items += [item("s1", "small", "2026-10-18", 0.72), item("s2", "small", "2026-10-18", 0.3),
              item("c1", "cup", "2026-10-15", 0.8, experimental=True)]
    chosen = [i.id for i in rt.select(items, RUN, limit=6)]
    # round 1: best head of every league (official picks, then the experimental pick); round 2 ...
    assert chosen == ["p0", "s1", "c1", "p1", "s2", "p2"]


def test_limit_caps_the_list_and_european_cups_are_kept_over_non_picks():
    items = [item(f"l{n}", f"league-{n}", "2026-10-18", 0.5) for n in range(25)]
    items.append(item("cup", "uefa-europa-league", "2026-10-16", 0.75, experimental=True))
    chosen = rt.select(items, RUN, limit=20)
    assert len(chosen) == 20 and chosen[0].id == "cup"
    assert len({i.league for i in chosen}) == 20
    assert rt.select(items, RUN, limit=3)[0].id == "cup" and len(rt.select(items, RUN, limit=3)) == 3


def test_prediction_must_be_published_before_kickoff(tmp_path, repo):
    # the only board was built after kick-off: no published pre-match prediction for this match
    data = write_export(tmp_path / "site", [fx("a", "england-premier-league", "2026-10-19", "A", "B", 0.8)],
                        built_at="2026-10-19T16:00:00Z")
    assert rt.build(data, RUN, repo_dir=repo)[0] == []
    data = write_export(tmp_path / "site2", [fx("a", "england-premier-league", "2026-10-19", "A", "B", 0.8)],
                        built_at="2026-10-19T14:59:00Z")
    assert [i.id for i in rt.build(data, RUN, repo_dir=repo)[0]] == ["a"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_played_matches_are_found_in_the_board_history(tmp_path, repo):
    site = tmp_path / "site"
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org"}

    def commit(when: str):
        env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
        subprocess.run(["git", "-C", str(site), "add", "-A"], check=True, env=env, capture_output=True)
        subprocess.run(["git", "-C", str(site), "commit", "-q", "-m", when], check=True, env=env,
                       capture_output=True)

    write_export(site, [fx("a", "england-premier-league", "2026-10-18", "Arsenal", "Leeds", 0.8, expected=27),
                        fx("b", "england-premier-league", "2026-10-18", "Fulham", "Wolves", 0.4)],
                 built_at="2026-10-17T08:00:00Z")
    subprocess.run(["git", "init", "-q", str(site)], check=True, capture_output=True)
    commit("2026-10-17T08:00:10Z")
    # a later pre-match board updates "a"; one built after kick-off must not count
    write_export(site, [fx("a", "england-premier-league", "2026-10-18", "Arsenal", "Leeds", 0.85, expected=29),
                        fx("b", "england-premier-league", "2026-10-18", "Fulham", "Wolves", 0.4)],
                 built_at="2026-10-18T09:00:00Z")
    commit("2026-10-18T09:00:10Z")
    write_export(site, [fx("a", "england-premier-league", "2026-10-18", "Arsenal", "Leeds", 0.99, expected=40)],
                 built_at="2026-10-18T16:00:00Z")
    commit("2026-10-18T16:00:10Z")
    # today's board no longer lists them (dated before the build date)
    write_export(site, [fx("z", "england-premier-league", "2026-10-21", "X", "Y", 0.8)],
                 built_at="2026-10-20T08:00:00Z")
    commit("2026-10-20T08:00:10Z")
    chosen, md = rt.build(site, RUN, repo_dir=repo)
    assert [(i.id, i.expected, i.p24) for i in chosen] == [("a", 29, 0.85), ("b", 27, 0.4)]
    assert "| 29 / 22 / 0.85 | yes |" in md


# ---- output ---------------------------------------------------------------------------------------

def test_links_target_files_and_unknown_slug(tmp_path, repo):
    data = write_export(tmp_path / "site", [
        fx("a", "england-premier-league", "2026-10-19", "Arsenal", "Leeds", 0.8, ko="19:30"),
        fx("c", "uefa-champions-league", "2026-10-15", "Lens", "Sp Lisbon", 0.6, experimental=True),
        fx("u", "atlantis-first-division", "2026-10-18", "Fish", "Coral", 0.6, ko=None)])
    _, md = rt.build(data, RUN, repo_dir=repo)
    rows = {line.split("|")[4].strip(): line for line in md.splitlines() if re.match(r"^\| \d+ \|", line)}
    assert "(https://www.espn.com/soccer/schedule/_/date/20261019/league/eng.1)" in rows["Arsenal v Leeds"]
    assert "| 19:30 |" in rows["Arsenal v Leeds"]
    assert "`results_provisional/england-premier-league_2026-27.csv`" in rows["Arsenal v Leeds"]
    assert "(https://www.espn.com/soccer/schedule/_/date/20261015/league/uefa.champions)" in rows["Lens v Sp Lisbon"]
    assert "`results_manual/uefa-champions-league_2026-27.csv`" in rows["Lens v Sp Lisbon"]
    assert "| experimental |" in rows["Lens v Sp Lisbon"]
    unknown = rows["Fish v Coral"].split("|")
    assert unknown[8].strip() == "—" and unknown[5].strip() == "—"  # no slug, no kick-off time
    assert "Atlantis First Division" in rows["Fish v Coral"]
    assert "https://www.espn.com/soccer/team-stats/_/gameId/<id>" in md
    # newest first in the table
    assert list(rows) == ["Arsenal v Leeds", "Fish v Coral", "Lens v Sp Lisbon"]


def test_csv_blocks_use_the_manual_results_columns_and_our_names(tmp_path, repo):
    data = write_export(tmp_path / "site", [
        fx("a", "england-premier-league", "2026-10-19", "Man United", "Nott'm Forest", 0.8),
        fx("b", "england-premier-league", "2026-10-18", "Fulham", "Wolves", 0.8),
        fx("c", "uefa-champions-league", "2026-10-15", "Lens", "Sp Lisbon", 0.6, experimental=True)])
    _, md = rt.build(data, RUN, repo_dir=repo)
    blocks = re.findall(r"### `([^`]+)`\n\n```csv\n(.*?)\n```", md, flags=re.S)
    assert [b[0] for b in blocks] == ["results_manual/uefa-champions-league_2026-27.csv",
                                      "results_provisional/england-premier-league_2026-27.csv"]
    assert tuple(rt.CSV_COLUMNS) == tuple(mr.COLUMNS)
    for _, body in blocks:
        lines = body.splitlines()
        assert lines[0] == ",".join(mr.COLUMNS)
        assert all(line.count(",") == len(mr.COLUMNS) - 1 for line in lines)
    assert blocks[1][1].splitlines()[1:] == ["2026-10-19,Man United,Nott'm Forest,,,,,,,,,",
                                             "2026-10-18,Fulham,Wolves,,,,,,,,,"]


def test_season_labels_and_note_when_the_provisional_folder_is_missing(tmp_path):
    assert rt.season_of("2026-10-18") == "2026-27" and rt.season_of("2027-05-20") == "2026-27"
    assert rt.season_of("2027-07-01") == "2027-28" and rt.season_of("2099-12-01") == "2099-00"
    bare = tmp_path / "bare"
    bare.mkdir()
    data = write_export(tmp_path / "site", [fx("a", "england-premier-league", "2026-10-19", "A", "B", 0.8)])
    _, md = rt.build(data, RUN, repo_dir=bare)
    assert "`results_provisional/` was not found" in md


def test_export_dir_layout_without_data_subfolder(tmp_path, repo):
    site = write_export(tmp_path / "site", [fx("a", "england-premier-league", "2026-10-19", "A", "B", 0.8)])
    flat = tmp_path / "flat"
    shutil.copytree(site / "data", flat)
    assert [i.id for i in rt.build(flat, RUN, repo_dir=repo)[0]] == ["a"]


def test_cli_writes_markdown_and_github_outputs(tmp_path, repo, capsys):
    data = write_export(tmp_path / "site", [fx("a", "england-premier-league", "2026-10-19", "A", "B", 0.8)])
    out = tmp_path / "todo.md"
    assert rt.main(["--date", "2026-10-20", "--data-dir", str(data), "--repo-dir", str(repo), "--out", str(out),
                    "--limit", "5"]) == 0
    assert capsys.readouterr().out.splitlines() == ["count=1", "title=Results to enter — 2026-10-20"]
    assert out.read_text(encoding="utf-8").startswith("# Results to enter — 2026-10-20")
    with pytest.raises(SystemExit):
        rt.main(["--limit", "0", "--data-dir", str(data)])


def test_empty_list_reports_zero(tmp_path, repo, capsys):
    data = write_export(tmp_path / "site", [])
    rt.main(["--date", "2026-10-20", "--data-dir", str(data), "--repo-dir", str(repo), "--out",
             str(tmp_path / "t.md")])
    assert capsys.readouterr().out.splitlines()[0] == "count=0"


# ---- slugs ----------------------------------------------------------------------------------------

def test_espn_slugs_cover_every_competition_we_predict():
    slugs = rt.load_slugs()
    expected = {re.sub(r"[^a-z0-9]+", "-", f"{country} {name}".lower()).strip("-")
                for name, country, _ in fd.DIVISIONS.values()} - {"england-national-league"}
    assert expected | set(xb.COMPETITIONS) <= set(slugs)
    assert all(re.fullmatch(r"[a-z]{3}\.\d|uefa\.[a-z.]+", s) for s in slugs.values())
    assert slugs["uefa-conference-league"] == "uefa.europa.conf" and slugs["scotland-league-two"] == "sco.4"
    assert rt.MANUAL_LEAGUES == frozenset(xb.COMPETITIONS)


# ---- never fetch anything -------------------------------------------------------------------------

def test_module_imports_no_network_library():
    tree = ast.parse(Path(rt.__file__).read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"requests", "urllib", "http", "httpx", "socket", "aiohttp", "urllib3", "shots"}


def test_a_run_opens_no_socket(tmp_path, repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    data = write_export(tmp_path / "site", [fx("a", "england-premier-league", "2026-10-19", "A", "B", 0.8)])
    assert rt.main(["--date", "2026-10-20", "--data-dir", str(data), "--repo-dir", str(repo),
                    "--out", str(tmp_path / "t.md")]) == 0


def test_script_runs_standalone_without_loading_network_modules(tmp_path, repo):
    data = write_export(tmp_path / "site", [fx("a", "england-premier-league", "2026-10-19", "A", "B", 0.8)])
    code = ("import runpy, sys; sys.argv = sys.argv[1:]\n"
            "try:\n    runpy.run_path(sys.argv[0], run_name='__main__')\nexcept SystemExit:\n    pass\n"
            "bad = [m for m in ('requests', 'urllib.request', 'http.client', 'httpx') if m in sys.modules]\n"
            "print('loaded=' + ','.join(bad))")
    out = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT / "src" / "shots" / "results_todo.py"),
                          "--date", "2026-10-20", "--data-dir", str(data), "--repo-dir", str(repo),
                          "--out", str(tmp_path / "t.md")], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == ["count=1", "title=Results to enter — 2026-10-20", "loaded="]


# ---- workflow -------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def workflow():
    yaml = pytest.importorskip("yaml")
    path = ROOT / ".github" / "workflows" / "results-todo.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["on"] = doc.pop(True) if True in doc else doc["on"]
    return doc, path.read_text(encoding="utf-8")


def test_workflow_schedule_guard_and_dispatch(workflow):
    doc, _ = workflow
    assert sorted(c["cron"] for c in doc["on"]["schedule"]) == ["0 10 * * *", "0 11 * * *"]
    assert "date" in doc["on"]["workflow_dispatch"]["inputs"]
    guard = doc["jobs"]["todo"]["steps"][0]
    assert "Europe/Paris" in guard["run"] and "-ge 12" in guard["run"]
    later = doc["jobs"]["todo"]["steps"][1:]
    assert all(s["if"] == "${{ steps.guard.outputs.proceed == 'true' }}" for s in later)


def test_workflow_permissions_tokens_and_actions(workflow):
    doc, text = workflow
    assert doc["permissions"] == {}
    assert doc["jobs"]["todo"]["permissions"] == {"contents": "read", "issues": "write"}
    assert "secrets." not in text and "pull_request_target" not in text
    steps = doc["jobs"]["todo"]["steps"]
    for s in steps:
        assert "${{" not in s.get("run", ""), s["name"]  # values reach the shell through env only
        if "uses" in s:
            assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", s["uses"]), s["uses"]
            if s["uses"].startswith("actions/checkout@"):
                assert s["with"]["persist-credentials"] is False
    assert [s["name"] for s in steps if "github.token" in str(s.get("env", ""))] == [
        "Open the issue (once per day, only when there is something to enter)", "Close checklists older than 7 days"]


def test_workflow_never_fetches_a_third_party_site_and_is_idempotent(workflow):
    doc, _ = workflow
    scripts = "\n".join(s.get("run", "") for s in doc["jobs"]["todo"]["steps"])
    assert not re.search(r"\b(curl|wget|pip|npm)\b|espn\.com|https?://", scripts)
    assert "src/shots/results_todo.py" in scripts and "--data-dir site-data" in scripts
    assert 'grep -Fxq "$TITLE"' in scripts and '"${COUNT:-0}" -eq 0' in scripts  # one issue a day, none if empty
    assert "gh label create" in scripts and "7 days ago" in scripts and "gh issue close" in scripts
    data = [s for s in doc["jobs"]["todo"]["steps"] if s.get("with", {}).get("path") == "site-data"]
    assert data and data[0]["with"]["fetch-depth"] == 0 and data[0]["with"]["ref"] == "${{ env.DATA_BRANCH }}"
    assert doc["env"]["DATA_BRANCH"] == "data"
