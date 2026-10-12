"""python -m shots.export.verify --dir <export dir> [--db data/shots.db] [--today YYYY-MM-DD] [--no-schema]

Independent check of an export against the database (evaluator hand-off; runs in CI after every
export). It does NOT use shots.export.build or the repository: it reads the SQLite file directly
(read-only) with its own SQL, so a bug in the exporter or the repository queries shows up as a
difference. Checks:

  schema     each file validates against docs/static-api.schema.json (needs the jsonschema package)
  privacy    no key outside the contract's vocabulary anywhere (odds, market, source, goals, ...)
  curves     aligned with meta.thresholds, within [0, 1], non-increasing (total and team curves)
  board      the fixtures are exactly the latest board run's upcoming rows, value for value, in order
  results    exactly the settled matches of the last 30 days with their last pre-match board prediction
  track      live counts and per-N selected picks / hits equal v_official_picks and the backfill run
  meta       thresholds, counts, days, leagues, models, last_job consistent with the above

Exit code 0 = all good, 1 = differences (listed), 2 = cannot run.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from datetime import date, timedelta
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "docs" / "static-api.schema.json"
NAMES = ("meta", "board", "results", "track")
FORBIDDEN_KEY = re.compile(r"odds_|mkt|market|source|goal|sot$|_sot|b365|bookmaker|price|closing|password|email|session",
                           re.IGNORECASE)
RESULT_DAYS = 30
MIN_PROB = 0.70


def slug(name: str) -> str:
    folded = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c)).casefold()
    return re.sub(r"[^a-z0-9]+", "-", folded).strip("-")


def r4(values) -> list[float]:
    return [round(float(v), 4) for v in values]


def num(x):
    return None if x is None else float(x)


def same(a, b) -> bool:
    """Equal numbers (int 25 == float 25.0) or both missing."""
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) < 1e-9


def utc_z(ts: str | None) -> str | None:
    """SQLite 'YYYY-MM-DD HH:MM:SS[.ffffff]' (UTC) -> 'YYYY-MM-DDTHH:MM:SSZ' (seconds, truncated)."""
    return None if ts is None else str(ts)[:19].replace(" ", "T") + "Z"


def walk_keys(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{path}.{k}", k
            yield from walk_keys(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:5000]):
            yield from walk_keys(v, f"{path}[{i}]")


class Checker:
    def __init__(self, docs: dict, con: sqlite3.Connection, today: date):
        self.docs, self.con, self.today = docs, con, today
        self.problems: list[str] = []
        self.checked: dict[str, int] = {}

    def fail(self, msg: str) -> None:
        if len(self.problems) < 200:
            self.problems.append(msg)

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.con.execute(sql, args).fetchall()

    # ------------------------------------------------------------------ file-only checks

    def schema(self, schema_path: Path) -> None:
        import jsonschema

        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        for name in NAMES:
            validator = jsonschema.Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": schema["$defs"]})
            for err in list(validator.iter_errors(self.docs[name]))[:10]:
                self.fail(f"schema: {name}.json {'/'.join(str(p) for p in err.absolute_path)}: {err.message[:200]}")

    def privacy(self) -> None:
        for name in NAMES:
            for path, key in walk_keys(self.docs[name]):
                if key != "odds" and FORBIDDEN_KEY.search(key):
                    self.fail(f"privacy: {name}.json has key {path}")
        for f in self.docs["board"]["fixtures"] + self.docs["results"]["results"]:
            if not isinstance(f.get("odds"), bool):
                self.fail(f"privacy: {f.get('id')}: `odds` must be a boolean flag")

    def curve(self, values, rng, what: str) -> None:
        if rng is None:
            return self.fail(f"curves: {what}: no threshold range in meta")
        if len(values) != rng[1] - rng[0] + 1:
            self.fail(f"curves: {what}: {len(values)} points for thresholds {rng[0]}..{rng[1]}")
        if any(not (0 <= v <= 1) for v in values):
            self.fail(f"curves: {what}: value outside [0, 1]")
        if any(b > a for a, b in zip(values, values[1:])):
            self.fail(f"curves: {what}: increasing")
        if any(round(v, 4) != v for v in values):
            self.fail(f"curves: {what}: more than 4 decimals")

    def curves(self) -> None:
        th = self.docs["meta"]["thresholds"]
        for f in self.docs["board"]["fixtures"]:
            self.curve(f["curve"], th["total"], f"board {f['id']}")
            if f["team"] is not None:
                for side in ("home", "away"):
                    self.curve(f["team"][side]["curve"], th["team"], f"board {f['id']} {side}")

    # ------------------------------------------------------------------ database checks

    def board(self) -> dict | None:
        run = self.q("SELECT * FROM prediction_runs WHERE kind = 'board' AND status = 'complete' "
                     "ORDER BY built_at DESC, id DESC LIMIT 1")
        if not run:
            self.fail("board: the database has no board run")
            return None
        run = run[0]
        rows = self.q("""
            SELECT p.*, m.date, m.kickoff_local, m.kickoff_utc, m.round, m.home_team, m.away_team, c.name AS comp
            FROM predictions p JOIN matches m ON m.match_id = p.match_id
            JOIN competitions c ON c.id = m.competition_id
            WHERE p.run_id = ? AND m.date >= ? AND m.status != 'played'
            ORDER BY m.date, COALESCE(m.kickoff_local, ''), c.name, m.home_team, p.match_id""",
                      run["id"], self.today.isoformat())
        got = self.docs["board"]["fixtures"]
        if [f["id"] for f in got] != [r["match_id"] for r in rows]:
            want_ids, got_ids = {r["match_id"] for r in rows}, {f["id"] for f in got}
            self.fail(f"board: fixture ids or order differ from run {run['id']} for {self.today} "
                      f"({len(got)} exported, {len(rows)} in the DB; missing {sorted(want_ids - got_ids)[:5]}, "
                      f"extra {sorted(got_ids - want_ids)[:5]})")
            rows = [r for r in rows if r["match_id"] in got_ids]
        by_id = {r["match_id"]: r for r in rows}
        team_run = run["team_n_min"] is not None
        for f in got:
            r = by_id.get(f["id"])
            if r is None:
                continue
            want = {"league": slug(r["comp"]), "round": r["round"] or None, "date": str(r["date"])[:10],
                    "kickoff_local": r["kickoff_local"] or None, "kickoff_utc": utc_z(r["kickoff_utc"]),
                    "home": r["home_team"], "away": r["away_team"], "odds": bool(r["odds_available"]),
                    "cold_start": bool(r["cold_start"]), "model": r["model"]}
            for k, v in want.items():
                if f[k] != v:
                    self.fail(f"board: {f['id']}.{k} = {f[k]!r}, database has {v!r}")
            nums = {"expected": (r["median"], 2), "league_avg": (r["league_avg"], 1), "above_league": (r["above_league"], 1)}
            for k, (v, digits) in nums.items():  # digits = the precision the contract publishes
                if not same(f[k], None if v is None else round(v, digits)):
                    self.fail(f"board: {f['id']}.{k} = {f[k]!r}, database has {v!r}")
            for qn in ("70", "80", "90"):
                if not same(f["lb"][qn], r[f"lb_{qn}"]):
                    self.fail(f"board: {f['id']}.lb.{qn} = {f['lb'][qn]!r}, database has {r[f'lb_{qn}']!r}")
            if f["curve"] != r4(json.loads(r["total_curve"] or "[]")):
                self.fail(f"board: {f['id']}.curve differs from the database")
            has_team = team_run and r["home_curve"] is not None and r["away_curve"] is not None
            if (f["team"] is not None) != has_team:
                self.fail(f"board: {f['id']}.team presence differs from the database")
            elif has_team:
                for side in ("home", "away"):
                    t = f["team"][side]
                    if t["curve"] != r4(json.loads(r[f"{side}_curve"])):
                        self.fail(f"board: {f['id']}.team.{side}.curve differs from the database")
                    for k, col in (("mean", "mean"), ("median", "median"), ("lb80", "lb_80")):
                        v = r[f"{side}_{col}"]
                        if not same(t[k], round(v, 2) if v is not None else None):
                            self.fail(f"board: {f['id']}.team.{side}.{k} = {t[k]!r}, database has {r[f'{side}_{col}']!r}")
        self.checked["board fixtures"] = len(got)
        return dict(run)

    def results(self) -> None:
        since = (self.today - timedelta(days=RESULT_DAYS)).isoformat()
        rows = self.q("""
            SELECT p.match_id, p.created_at, p.median, p.lb_70, p.lb_80, p.lb_90, p.odds_available, p.total_curve,
                   r.id AS run_id, r.built_at, o.date, o.competition, o.actual_total, o.actual_home, o.actual_away,
                   m.home_team, m.away_team
            FROM v_prediction_outcomes o
            JOIN predictions p ON p.id = o.prediction_id
            JOIN prediction_runs r ON r.id = p.run_id
            JOIN matches m ON m.match_id = p.match_id
            WHERE o.kind = 'board' AND r.status = 'complete' AND o.pre_match = 1 AND o.status = 'settled'
              AND o.date >= ? AND o.date <= ?""", since, self.today.isoformat())
        best: dict[str, sqlite3.Row] = {}
        for r in rows:  # the last pre-match board run per match
            b = best.get(r["match_id"])
            if b is None or (r["built_at"], r["run_id"]) > (b["built_at"], b["run_id"]):
                best[r["match_id"]] = r
        got = {x["id"]: x for x in self.docs["results"]["results"]}
        if len(got) != len(self.docs["results"]["results"]):
            self.fail("results: duplicate ids")
        if set(got) != set(best):
            self.fail(f"results: ids differ (missing {sorted(set(best) - set(got))[:5]}, "
                      f"not official in the database {sorted(set(got) - set(best))[:5]})")
        for mid, x in got.items():
            r = best.get(mid)
            if r is None:
                continue
            want = {"league": slug(r["competition"]), "date": str(r["date"])[:10], "home": r["home_team"],
                    "away": r["away_team"], "predicted_at": utc_z(r["created_at"]), "odds": bool(r["odds_available"]),
                    "actual": {"total": r["actual_total"], "home": r["actual_home"], "away": r["actual_away"]},
                    "curve": r4(json.loads(r["total_curve"] or "[]"))}
            for k, v in want.items():
                if x[k] != v:
                    self.fail(f"results: {mid}.{k} differs from the database")
            if not same(x["expected"], r["median"]) or any(not same(x["lb"][qn], r[f"lb_{qn}"]) for qn in ("70", "80", "90")):
                self.fail(f"results: {mid} expected / lb differ from the database")
            if x["actual"]["home"] + x["actual"]["away"] != x["actual"]["total"]:
                self.fail(f"results: {mid} home + away != total")
        self.checked["results"] = len(got)

    def _headline(self, name: str, by_n: dict, rows: list) -> None:
        """rows: (n, p, hit) of settled picks."""
        want: dict[str, list[int]] = {}
        for n, p, hit in rows:
            w = want.setdefault(str(int(n)), [0, 0])
            if p >= MIN_PROB:
                w[0] += 1
                w[1] += int(bool(hit))
        if set(by_n) != set(want):
            self.fail(f"track: {name} thresholds {sorted(by_n)} != database {sorted(want)}")
        for n, (k, hits) in want.items():
            e = by_n.get(n)
            if e is None:
                continue
            if (e["n"], e["hits"]) != (k, hits):
                self.fail(f"track: {name} N={n}: n/hits {e['n']}/{e['hits']}, database has {k}/{hits}")
            if k and not same(e["hit_rate"], round(hits / k, 4)):
                self.fail(f"track: {name} N={n}: hit_rate {e['hit_rate']} != {round(hits / k, 4)}")
            if sum(b["n"] for b in e["reliability"]) != sum(1 for m, _, _ in rows if str(int(m)) == n):
                self.fail(f"track: {name} N={n}: reliability bands do not add up to the settled rows")
        self.checked[f"track {name} thresholds"] = len(want)

    def track(self) -> None:
        rec = self.docs["track"]["records"]
        live = self.q("SELECT n, p, status, hit FROM v_official_picks")
        counts = {s: sum(1 for r in live if r["status"] == s) for s in ("settled", "pending", "void")}
        for s, k in counts.items():
            if rec["live"].get(s, 0) != k:
                self.fail(f"track: live.{s} = {rec['live'].get(s)}, v_official_picks has {k}")
        self._headline("live", rec["live"]["by_n"], [(r["n"], r["p"], r["hit"]) for r in live if r["status"] == "settled"])
        run = self.q("SELECT id FROM prediction_runs WHERE kind = 'backfill' AND status = 'complete' "
                     "ORDER BY built_at DESC, id DESC LIMIT 1")
        back = []
        if run:
            back = self.q("""
                SELECT CAST(j.key AS INTEGER) AS n, j.value AS p, m.home_shots + m.away_shots >= CAST(j.key AS INTEGER) AS hit
                FROM predictions p JOIN json_each(p.points) j JOIN matches m ON m.match_id = p.match_id
                WHERE p.run_id = ? AND m.status = 'played' AND m.home_shots IS NOT NULL AND m.away_shots IS NOT NULL""",
                          run[0]["id"])
        if rec["backfill"] is None:
            if back:
                self.fail("track: backfill is null but the database has a settled backfill run")
        else:
            self._headline("backfill", rec["backfill"]["by_n"], [(r["n"], r["p"], r["hit"]) for r in back])

    def meta(self, run: dict | None) -> None:
        meta, fixtures = self.docs["meta"], self.docs["board"]["fixtures"]
        if run is not None:
            th = meta["thresholds"]
            if th["total"] != [run["curve_n_min"], run["curve_n_max"]]:
                self.fail(f"meta: thresholds.total {th['total']} != run {run['curve_n_min']}..{run['curve_n_max']}")
            want_team = [run["team_n_min"], run["team_n_max"]] if run["team_n_min"] is not None else None
            if th["team"] != want_team:
                self.fail(f"meta: thresholds.team {th['team']} != run {want_team}")
            if meta["models"] != sorted(json.loads(run["models"] or "[]")):
                self.fail("meta: models differ from the board run")
            if meta["results_up_to"] != (str(run["results_up_to"])[:10] if run["results_up_to"] else None):
                self.fail("meta: results_up_to differs from the board run")
        counts = {"fixtures": len(fixtures), "with_odds": sum(f["odds"] for f in fixtures),
                  "cold_start": sum(f["cold_start"] for f in fixtures)}
        if meta["counts"] != counts:
            self.fail(f"meta: counts {meta['counts']} != board.json {counts}")
        if meta["days"] != sorted({f["date"] for f in fixtures}):
            self.fail("meta: days differ from board.json")
        if any(f["date"] < self.today.isoformat() for f in fixtures):
            self.fail("meta: board.json lists a fixture before the export day")
        known = {c["name"]: c for c in self.q("SELECT name, country, tier FROM competitions")}
        by_slug = {slug(n): c for n, c in known.items()}
        per = {}
        for f in fixtures:
            per[f["league"]] = per.get(f["league"], 0) + 1
        listed = {x["id"]: x for x in meta["leagues"]}
        used = set(per) | {x["league"] for x in self.docs["results"]["results"]}
        rec = self.docs["track"]["records"]
        for block in (rec["backfill"], rec["live"]):
            for e in ((block or {}).get("by_n") or {}).values():
                used |= {x["league"] for x in e["by_league"]}
        if used - set(listed):
            self.fail(f"meta: leagues missing from meta.leagues: {sorted(used - set(listed))}")
        for lid, x in listed.items():
            c = by_slug.get(lid)
            if c is None:
                self.fail(f"meta: league {lid} is not a competition in the database")
            elif (x["country"], x["tier"]) != (c["country"], c["tier"]):
                self.fail(f"meta: league {lid} country / tier differ from the database")
            if x["fixtures"] != per.get(lid, 0):
                self.fail(f"meta: league {lid} fixtures {x['fixtures']} != board.json {per.get(lid, 0)}")
        job = self.q("SELECT status, finished_at FROM job_runs WHERE job = 'update' AND finished_at IS NOT NULL "
                     "ORDER BY started_at DESC, id DESC LIMIT 1")
        # last_job is not compared exactly: meta.json is deliberately not rewritten when only the job's
        # end time moved. Its presence and a time not later than the database's are checked.
        if meta["last_job"] is not None:
            if not job:
                self.fail("meta: last_job set but the database has no finished update run")
            elif meta["last_job"]["finished_at"] > utc_z(job[0]["finished_at"]):
                self.fail("meta: last_job.finished_at is later than the database's last update run")
        since = self.q("SELECT min(created_at) AS t FROM predictions WHERE pre_match = 1")[0]["t"]
        if "record" in meta and meta["record"]["since"] != utc_z(since):
            self.fail(f"meta: record.since {meta['record']['since']} != database {utc_z(since)}")


def verify(export_dir: str | Path, db: str | Path | None = None, today: date | None = None,
           schema: str | Path | None | bool = None) -> dict:
    """-> {"ok": bool, "problems": [...], "checked": {...}, "today": "YYYY-MM-DD"}.
    schema: path of the JSON Schema, None = docs/static-api.schema.json, False = skip that check."""
    from shots.db.engine import db_path  # path resolution only (SHOTS_DB); no repository, no ORM

    export_dir = Path(export_dir)
    docs = {name: json.loads((export_dir / f"{name}.json").read_text(encoding="utf-8")) for name in NAMES}
    today = today or date.fromisoformat(docs["meta"]["built_at"][:10])
    path = Path(db or db_path()).resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        ck = Checker(docs, con, today)
        if schema is not False:
            ck.schema(Path(schema or SCHEMA_PATH))
        ck.privacy()
        ck.curves()
        run = ck.board()
        ck.results()
        ck.track()
        ck.meta(run)
    finally:
        con.close()
    return {"ok": not ck.problems, "problems": ck.problems, "checked": ck.checked, "today": today.isoformat()}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m shots.export.verify", description=__doc__.split("\n")[0])
    ap.add_argument("--dir", required=True, help="directory holding meta.json, board.json, results.json, track.json")
    ap.add_argument("--db", default=None, help="SQLite file (default: SHOTS_DB or data/shots.db)")
    ap.add_argument("--today", type=date.fromisoformat, default=None,
                    help="the export's first board day (default: the UTC date of meta.built_at)")
    ap.add_argument("--no-schema", action="store_true", help="skip the JSON Schema check (jsonschema not installed)")
    args = ap.parse_args(argv)
    try:
        res = verify(args.dir, args.db, args.today, False if args.no_schema else None)
    except (OSError, ValueError, ImportError, sqlite3.Error) as exc:
        print(f"cannot verify: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
    for p in res["problems"]:
        print("DIFF", p)
    print(json.dumps({"ok": res["ok"], "today": res["today"], "problems": len(res["problems"]), "checked": res["checked"]}))
    sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
