"""python -m shots.export.verify --dir <export dir> [--db data/shots.db] [--today YYYY-MM-DD] [--no-schema]

Independent check of an export against the database (evaluator hand-off; runs in CI after every
export). It does NOT use shots.export.build or the repository: it reads the SQLite file directly
(read-only) with its own SQL, so a bug in the exporter or the repository queries shows up as a
difference. Checks:

  schema     each file validates against docs/static-api.schema.json (needs the jsonschema package)
  privacy    no key outside the contract's vocabulary anywhere (odds, market, source, goals, ...)
  curves     aligned with meta.thresholds, within [0, 1], non-increasing (total and team curves)
  explain    each explanation adds up exactly (baseline + effects + residual = expected_mean, in tenths), is
             ordered by absolute effect ("other" last), states no number in a market factor, and its
             odds_used flag equals the fixture's; it equals the explanation stored with the prediction
  board      the fixtures are exactly the latest board run's upcoming rows, value for value, in order
  results    exactly the settled matches of the last 30 days with their last pre-match board prediction
             (ordinary and experimental), the experimental flag, result_source, result_status and the
             source_url of manual / provisional results
  track      live / experimental counts (provisional included) and per-N selected picks / hits equal
             v_official_picks / v_experimental_picks and the backfill run; the daily / weekly accuracy series
             recomputed from the same per-match predictions as results.json
  meta       thresholds, counts, days, leagues, models, last_job consistent with the above
  surge      surge.json equals the committed Surge Watch catalogue (src/shots/evaluation/surge_signals.json, read
             here as a plain file); each fixture's `surge` labels are exactly those recorded with the board run
             (its meta, same catalogue version, else none), every label is a boost / damper that applies on the
             fixture's route, and at most one season-end label

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
SURGE_TABLE = Path(__file__).resolve().parents[1] / "evaluation" / "surge_signals.json"
NAMES = ("meta", "board", "results", "track", "surge")
SURGE_STATS = ("n", "over_rate", "stated_rate", "base_rate", "excess_ci", "seasons_held", "seasons", "applies")
SEASON_END = ("dead_rubber", "dead_team", "last_three")
FORBIDDEN_KEY = re.compile(r"odds_|mkt|market|source|goal|sot$|_sot|b365|bookmaker|price|closing|password|email|session",
                           re.IGNORECASE)
RESULT_DAYS = 30
MIN_PROB = 0.70
SERIES_POINTS = {"daily": 30, "weekly": 52}
# the only contract keys that may contain a forbidden word: where a result's actual counts come from
ALLOWED_PATHS = re.compile(r"^\.results\[\d+\]\.(result_source|source_url)$")
EXPLAIN_MAX_RESIDUAL_TENTHS = 5  # rounding of up to ten one-decimal parts; anything larger is a bug


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


def provider_label(url: str | None) -> str | None:
    """'https://www.espn.com/soccer/...' -> 'espn': the host without 'www.' / 'm.', its first label."""
    from urllib.parse import urlsplit

    host = (urlsplit(url or "").hostname or "").lower()
    host = re.sub(r"^(www|m)\.", "", host)
    label = host.split(".")[0] if host else ""
    return label if re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", label) else None


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
                # the two boolean flags of the contract: a fixture's `odds` and explain.signals.odds_used
                if (key != "odds" and not path.endswith(".explain.signals.odds_used")
                        and not (name == "results" and ALLOWED_PATHS.match(path)) and FORBIDDEN_KEY.search(key)):
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

    def explain(self) -> None:
        n = 0
        for f in self.docs["board"]["fixtures"]:
            e = f.get("explain")
            if e is None:
                if "explain" in f:
                    self.fail(f"explain: {f['id']}: `explain` must be an object or absent, never null")
                continue
            n += 1
            what = f"explain: {f['id']}"
            t = lambda x: int(round(float(x) * 10))  # noqa: E731 - published values are tenths of a shot
            effects = [fx["effect"] for fx in e["factors"]]
            values = [e["expected_mean"], e["baseline"]["value"], e["residual"], *effects]
            if any(abs(float(v) * 10 - t(v)) > 1e-6 for v in values):
                self.fail(f"{what}: a value has more than 1 decimal")
            if t(e["baseline"]["value"]) + sum(t(x) for x in effects) + t(e["residual"]) != t(e["expected_mean"]):
                self.fail(f"{what}: baseline + effects + residual != expected_mean")
            if abs(t(e["residual"])) > EXPLAIN_MAX_RESIDUAL_TENTHS:
                self.fail(f"{what}: residual {e['residual']} is not small")
            if any(t(x) == 0 for x in effects):
                self.fail(f"{what}: a listed factor has no effect")
            ids = [fx["id"] for fx in e["factors"]]
            main = [fx for fx in e["factors"] if fx["id"] != "other"]
            if len(set(ids)) != len(ids):
                self.fail(f"{what}: duplicate factor id")
            if "other" in ids[:-1]:
                self.fail(f"{what}: `other` must be the last factor")
            if any(abs(t(b["effect"])) > abs(t(a["effect"])) for a, b in zip(main, main[1:])):
                self.fail(f"{what}: factors are not sorted by absolute effect")
            for fx in e["factors"]:
                if fx["id"].startswith("market"):
                    # team names may contain digits ("Schalke 04"); nothing else in a market text may
                    words = (fx["label"] + " " + fx["detail"]).replace(f["home"], "").replace(f["away"], "")
                    if any(ch.isdigit() for ch in words):
                        self.fail(f"{what}: market factor {fx['id']} states a number")
                    if not f["odds"]:
                        self.fail(f"{what}: market factor on a fixture scored without odds")
            sig = e["signals"]
            if sig["odds_used"] is not f["odds"]:
                self.fail(f"{what}: signals.odds_used {sig['odds_used']!r} != fixture odds {f['odds']!r}")
            if sig["model_gap"] is not None and not f["odds"]:
                self.fail(f"{what}: model_gap on a fixture scored without odds")
        if n:
            self.checked["explanations"] = n

    def surge_catalogue(self, table: dict) -> None:
        """surge.json is the committed catalogue, field by field (nothing added, nothing recomputed)."""
        got = self.docs["surge"]
        want = {"schema": 1, "report_date": table["report_date"], "margin": table["margin"], "matches": table["matches"],
                "period": table["period"], "method": table["method"],
                "routes": {r: {"label": v["label"], "base_rate": v["base_rate"]} for r, v in table["routes"].items()},
                "signals": [{"id": x["id"], "label": x["label"], "direction": x["direction"],
                             "explanation": x["explanation"],
                             "routes": {r: {k: x["routes"][r][k] for k in SURGE_STATS} for r in ("odds", "noodds")}}
                            for x in table["signals"]]}
        if got != want:
            keys = [k for k in want if got.get(k) != want[k]]
            self.fail(f"surge: surge.json differs from the committed catalogue ({', '.join(keys) or 'keys'})")
        self.checked["surge signals"] = len(got.get("signals", []))

    def surge_labels(self, run: dict | None, table: dict) -> None:
        """Every fixture's `surge` = the labels recorded with the board run for it (same catalogue version), or
        absent; and each label is allowed on the fixture's route."""
        import hashlib

        recorded: dict = {}
        if run is not None:
            meta = json.loads(run["meta"]) if isinstance(run.get("meta"), str) else (run.get("meta") or {})
            s = meta.get("surge") if isinstance(meta, dict) else None
            version = hashlib.sha256(json.dumps(table, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                                     .encode("utf-8")).hexdigest()[:16]
            if isinstance(s, dict) and s.get("version") == version and isinstance(s.get("fixtures"), dict):
                recorded = s["fixtures"]
        sig = {x["id"]: x for x in table["signals"]}
        n = 0
        for f in self.docs["board"]["fixtures"]:
            got = f.get("surge")
            want = recorded.get(f["id"])
            if "surge" in f and got is None:
                self.fail(f"surge: {f['id']}: `surge` must be an object or absent, never null")
                continue
            if got != (None if want is None else {"signals": want["signals"], "against": want["against"]}):
                self.fail(f"surge: {f['id']}: labels {got!r} differ from the board run's {want!r}")
            if got is None:
                continue
            n += 1
            route = "odds" if f["odds"] else "noodds"
            for key, direction in (("signals", "boost"), ("against", "damper")):
                for sid in got[key]:
                    x = sig.get(sid)
                    if x is None or x["direction"] != direction or not x["routes"][route]["applies"]:
                        self.fail(f"surge: {f['id']}: {sid!r} is not a {direction} that applies on the {route} route")
            if sum(1 for sid in got["signals"] if sid in SEASON_END) > 1:
                self.fail(f"surge: {f['id']}: more than one season-end label")
            if f.get("experimental"):
                self.fail(f"surge: {f['id']}: an experimental fixture is never assessed")
        self.checked["surge labelled fixtures"] = n

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
            # additive fields: present exactly for the database's experimental predictions
            if bool(f.get("experimental", False)) != bool(r["experimental"]) or ("experimental" in f and f["experimental"] is not True):
                self.fail(f"board: {f['id']}.experimental = {f.get('experimental')!r}, database has {bool(r['experimental'])}")
            want_quality = r["data_quality"] if r["experimental"] and r["data_quality"] in ("full", "partial", "none") else None
            if f.get("data_quality") != want_quality:
                self.fail(f"board: {f['id']}.data_quality = {f.get('data_quality')!r}, database has {want_quality!r}")
            # the explanation is the one stored with the prediction (SQL NULL or JSON null = none), or absent
            stored = json.loads(r["explain"]) if "explain" in r.keys() and r["explain"] is not None else None
            if f.get("explain") != stored:
                self.fail(f"board: {f['id']}.explain differs from the database")
            # a prediction without team curves in a run that has them (JSON null, stored as the text 'null')
            has_team = team_run and all(r[c] is not None and json.loads(r[c]) is not None for c in ("home_curve", "away_curve"))
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

    def settled_board(self) -> dict[str, sqlite3.Row]:
        """Per settled match dated up to today: its last pre-match board prediction (either flag)."""
        rows = self.q("""
            SELECT p.match_id, p.created_at, p.median, p.lb_70, p.lb_80, p.lb_90, p.odds_available, p.total_curve,
                   p.experimental, r.id AS run_id, r.built_at, r.curve_n_min, o.date, o.competition, o.actual_total,
                   o.actual_home, o.actual_away, m.home_team, m.away_team, mr.source_url, mr.match_id AS manual_id,
                   m.source AS match_source, pv.source_url AS prov_url
            FROM v_prediction_outcomes o
            JOIN predictions p ON p.id = o.prediction_id
            JOIN prediction_runs r ON r.id = p.run_id
            JOIN matches m ON m.match_id = p.match_id
            LEFT JOIN manual_results mr ON mr.match_id = m.match_id
            LEFT JOIN provisional_results pv ON pv.match_id = m.match_id
            WHERE o.kind = 'board' AND r.status = 'complete' AND o.pre_match = 1 AND o.status = 'settled'
              AND o.date <= ?""", self.today.isoformat())
        best: dict[str, sqlite3.Row] = {}
        for r in rows:  # the last pre-match board run per match
            b = best.get(r["match_id"])
            if b is None or (r["built_at"], r["run_id"]) > (b["built_at"], b["run_id"]):
                best[r["match_id"]] = r
        return best

    def results(self) -> None:
        since = (self.today - timedelta(days=RESULT_DAYS)).isoformat()
        self.best = self.settled_board()
        best = {m: r for m, r in self.best.items() if str(r["date"])[:10] >= since}
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
            if x.get("experimental", False) is not bool(r["experimental"]) or x.get("experimental") is False:
                self.fail(f"results: {mid}.experimental = {x.get('experimental')!r}, database has {bool(r['experimental'])}")
            manual = r["manual_id"] is not None
            provisional = r["match_source"] == "provisional"
            want_source = (provider_label(r["prov_url"]) or "provisional") if provisional else (
                "manual" if manual else "football-data")
            want_url = r["prov_url"] if provisional else (r["source_url"] if manual else None)
            if x.get("result_source") != want_source:
                self.fail(f"results: {mid}.result_source = {x.get('result_source')!r} differs from the database")
            if x.get("result_status") != ("provisional" if provisional else "confirmed"):
                self.fail(f"results: {mid}.result_status = {x.get('result_status')!r} differs from the database")
            if x.get("source_url") != want_url or ("source_url" in x and want_url is None):
                self.fail(f"results: {mid}.source_url differs from the database")
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

    def _series(self, name: str, block: dict, view: str, experimental: bool) -> None:
        """daily / weekly accuracy, recomputed here: matches / mae / bias / floor80_held from the per-match
        predictions of settled_board(), by_n from the settled picks of `view` (keys = its thresholds)."""
        best = self.best if hasattr(self, "best") else self.settled_board()
        rows = [r for r in best.values() if bool(r["experimental"]) is experimental]
        picks = self.q(f"SELECT date, n, p, hit FROM {view} WHERE status = 'settled' AND date <= ?",
                       self.today.isoformat())
        ns = sorted({int(r["n"]) for r in self.q(f"SELECT DISTINCT n FROM {view} WHERE status = 'settled'")})
        for period, limit in SERIES_POINTS.items():
            def key_of(day) -> str:
                d = date.fromisoformat(str(day)[:10])
                return (d if period == "daily" else d - timedelta(days=d.weekday())).isoformat()
            groups: dict[str, list] = {}
            pgroups: dict[str, list] = {}
            for r in rows:
                groups.setdefault(key_of(r["date"]), []).append(r)
            for r in picks:
                pgroups.setdefault(key_of(r["date"]), []).append(r)
            want = []
            for key in sorted(set(groups) | set(pgroups))[-limit:]:
                g = groups.get(key, [])
                errs = [(r["actual_total"], r["median"]) for r in g if r["median"] is not None]
                floor = [r["actual_total"] >= r["lb_80"] for r in g if r["lb_80"] is not None]
                by_n = {}
                for n in ns:
                    sel = [bool(r["hit"]) for r in pgroups.get(key, []) if int(r["n"]) == n and r["p"] >= MIN_PROB]
                    by_n[str(n)] = {"picks": len(sel), "hits": sum(sel)}
                want.append({"date": key, "matches": len(g),
                             "mae": round(sum(abs(a - e) for a, e in errs) / len(errs), 2) if errs else None,
                             "bias": round(sum(e - a for a, e in errs) / len(errs), 2) if errs else None,
                             "floor80_held": round(sum(floor) / len(floor), 4) if floor else None, "by_n": by_n,
                             "provisional": sum(1 for r in g if r["match_source"] == "provisional")})
            got = block.get(period)
            if got is None:
                self.fail(f"track: {name}.{period} is missing")
                continue
            if [x["date"] for x in got] != [x["date"] for x in want]:
                self.fail(f"track: {name}.{period} dates {[x['date'] for x in got][-5:]} != database "
                          f"{[x['date'] for x in want][-5:]}")
                continue
            for g_, w in zip(got, want):
                for k in ("matches", "by_n", "provisional"):
                    if g_.get(k) != w[k]:
                        self.fail(f"track: {name}.{period} {w['date']}.{k} = {g_[k]!r}, database gives {w[k]!r}")
                for k in ("mae", "bias", "floor80_held"):
                    if not same(g_[k], w[k]):
                        self.fail(f"track: {name}.{period} {w['date']}.{k} = {g_[k]!r}, database gives {w[k]!r}")
            self.checked[f"track {name} {period}"] = len(want)

    def _picks(self, name: str, view: str, block: dict | None) -> None:
        if block is None:
            self.fail(f"track: records.{name} is missing")
            return
        rows = self.q(f"SELECT v.n, v.p, v.status, v.hit, m.source = 'provisional' AS prov FROM {view} v "
                      "LEFT JOIN matches m ON m.match_id = v.match_id")
        counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("settled", "pending", "void")}
        counts["provisional"] = sum(1 for r in rows if r["status"] == "settled" and r["prov"])
        for s, k in counts.items():
            if block.get(s, 0) != k:
                self.fail(f"track: {name}.{s} = {block.get(s)}, {view} has {k}")
        self._headline(name, block["by_n"], [(r["n"], r["p"], r["hit"]) for r in rows if r["status"] == "settled"])

    def track(self) -> None:
        rec = self.docs["track"]["records"]
        self._picks("live", "v_official_picks", rec["live"])
        self._series("live", rec["live"], "v_official_picks", False)
        self._picks("experimental", "v_experimental_picks", rec.get("experimental"))
        if rec.get("experimental") is not None:
            self._series("experimental", rec["experimental"], "v_experimental_picks", True)
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
        results = self.docs["results"]["results"]
        counts = {"fixtures": len(fixtures), "with_odds": sum(f["odds"] for f in fixtures),
                  "cold_start": sum(f["cold_start"] for f in fixtures), "results": len(results),
                  "provisional_results": sum(1 for x in results if x.get("result_status") == "provisional")}
        if meta["counts"] != counts:
            self.fail(f"meta: counts {meta['counts']} != board.json / results.json {counts}")
        if meta["days"] != sorted({f["date"] for f in fixtures}):
            self.fail("meta: days differ from board.json")
        if any(f["date"] < self.today.isoformat() for f in fixtures):
            self.fail("meta: board.json lists a fixture before the export day")
        known = {c["name"]: c for c in self.q("SELECT name, country, tier, code FROM competitions")}
        by_slug = {slug(n): c for n, c in known.items()}
        per = {}
        for f in fixtures:
            per[f["league"]] = per.get(f["league"], 0) + 1
        exp_only = {lid for lid in per if all(f.get("experimental") for f in fixtures if f["league"] == lid)}
        # a league with an ordinary fixture or result anywhere can never be featured
        ordinary = ({f["league"] for f in fixtures if not f.get("experimental")}
                    | {x["league"] for x in self.docs["results"]["results"] if not x.get("experimental")})
        listed = {x["id"]: x for x in meta["leagues"]}
        used = set(per) | {x["league"] for x in self.docs["results"]["results"]}
        rec = self.docs["track"]["records"]
        for block in (rec["backfill"], rec["live"], rec.get("experimental")):
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
            # featured = a competition without a football-data division code whose board fixtures are all
            # experimental (the cross-border competitions); the flag is `true` or absent, never false
            if "featured" in x and (x["featured"] is not True or lid in ordinary or (c is not None and c["code"])):
                self.fail(f"meta: league {lid} is marked featured but is not an experimental-only competition")
            if lid in exp_only and c is not None and not c["code"] and "featured" not in x:
                self.fail(f"meta: league {lid} holds only experimental fixtures but is not marked featured")
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
        aw = meta.get("awaiting_results")
        if aw is not None:
            want = {"official": 0, "experimental": 0, "oldest": None}
            if run is not None:
                built = str(run["built_at"])[:19].replace("T", " ")
                for row in self.q(
                        "SELECT o.experimental AS xp, count(DISTINCT p.match_id) AS n, min(m.date) AS oldest "
                        "FROM predictions p JOIN prediction_runs r ON r.id = p.run_id "
                        "JOIN v_prediction_outcomes o ON o.prediction_id = p.id JOIN matches m ON m.match_id = p.match_id "
                        "WHERE r.kind = 'board' AND r.status = 'complete' AND o.pre_match = 1 AND o.status = 'pending' "
                        "AND julianday(COALESCE(m.kickoff_utc, datetime(m.date, '+36 hours'))) + 2.5 / 24.0 < julianday(?) "
                        "AND m.date >= date(?, '-30 days') GROUP BY o.experimental", built, built):
                    want["experimental" if row["xp"] else "official"] += int(row["n"])
                    o = str(row["oldest"])[:10]
                    want["oldest"] = o if want["oldest"] is None or o < want["oldest"] else want["oldest"]
            if aw != want:
                self.fail(f"meta: awaiting_results {aw} != database {want}")


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
        ck.explain()
        run = ck.board()
        ck.results()
        ck.track()
        ck.meta(run)
        table = json.loads(SURGE_TABLE.read_text(encoding="utf-8"))
        ck.surge_catalogue(table)
        ck.surge_labels(run, table)
    finally:
        con.close()
    return {"ok": not ck.problems, "problems": ck.problems, "checked": ck.checked, "today": today.isoformat()}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m shots.export.verify", description=__doc__.split("\n")[0])
    ap.add_argument("--dir", required=True, help="directory holding meta.json, board.json, results.json, track.json, surge.json")
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
