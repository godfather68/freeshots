"""FastAPI app: renders the latest board stored in the database (data/shots.db) through the
repository layer (shots.db.repo). Never runs a model and never writes to the DB."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import Engine
from starlette.exceptions import HTTPException as StarletteHTTPException

from shots.auth import web as authweb
from shots.auth.http_security import install_security_headers, install_trusted_hosts
from shots.db.access import FULL_ACCESS, Entitlements
from shots.db.repo import Repository
from shots.predict import board as boardmod
from shots.web import filters as flt
from shots.web.charts import curve_svg, team_curve_svg
from shots.web.fresh import freshness
from shots.web.glossary import TERMS

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
TRACK_CACHE_S = 60
TRACK_CACHE_MAX = 32  # LRU bound: keys are (resolved src, resolved n, entitlements)
TRACK_SOURCES = ("backfill", "live")
STATUS_CACHE_S = 20
UPGRADE_PLAN = "Pro"  # placeholder plan names; the pricing page decides the real ones
UPGRADE_PLAN_TOP = "Premium"
WHEN_DAYS = {"today": 0, "3d": 3, "7d": 7, "30d": 30}


def _clean(v):
    return None if v is None or (not isinstance(v, (str, list, dict)) and pd.isna(v)) else v


def records(df: pd.DataFrame) -> list[dict]:
    return [{k: _clean(v) for k, v in row.items()} for row in df.astype(object).to_dict("records")]


def pct(p, digits: int = 0) -> str:
    return "–" if p is None or pd.isna(p) else f"{p * 100:.{digits}f}%"


def day_label(d: date, today: date) -> str:
    base = f"{d:%a} {d.day} {d:%b}"
    if d == today:
        return f"Today · {base}"
    if d == today + timedelta(days=1):
        return f"Tomorrow · {base}"
    return base


def _band(p) -> str:
    if p is None:
        return "lo"
    return "hi" if p >= flt.STRONG else "mid" if p >= 0.5 else "lo"


def access_summary(ent: Entitlements) -> dict:
    """What the plan locks, in terms the UI can show. Never names or counts locked content."""
    return {
        "leagues": not ent.can_view_all_leagues,
        "n_leagues": len(ent.allowed_leagues),
        "days": ent.max_days_ahead,
        "team": not ent.team_predictions,
        "track": not ent.track_detail,
        "any": (not ent.can_view_all_leagues) or ent.max_days_ahead is not None or not ent.team_predictions,
    }


def describe_access(e: dict) -> list[dict]:
    """Plain-language bullets for one plan's entitlement values: [{text, included}]."""
    leagues = e.get("can_view_all_leagues")
    n = len(e.get("allowed_leagues") or ())
    days = e.get("max_days_ahead")
    return [
        {"text": "Every league on the board" if leagues else (f"{n} leagues" if n else "No leagues"), "included": bool(leagues or n)},
        {"text": "The whole season ahead" if days is None else f"Matches up to {days} day{'s' if days != 1 else ''} ahead", "included": days is None or days > 0},
        {"text": "Team-by-team shot numbers (experimental)", "included": bool(e.get("team_predictions"))},
        {"text": "Per-league track record", "included": bool(e.get("track_detail"))},
        {"text": "Data export", "included": bool(e.get("export"))},
    ]


def when_options(ent: Entitlements) -> list[dict]:
    """The 'When?' choices, with the ones beyond the plan's day limit marked locked."""
    out = []
    for key, label in flt.WHEN.items():
        days = WHEN_DAYS.get(key)
        locked = ent.max_days_ahead is not None and days is not None and days > ent.max_days_ahead
        out.append({"key": key, "label": label, "locked": locked})
    return out


def create_app(db: str | Path | Engine | None = None, today: Callable[[], date] | None = None,
               entitlements: Callable[[Request], Entitlements] | None = None) -> FastAPI:
    """db: SQLAlchemy URL, DB file path or Engine (default: SHOTS_DB or data/shots.db).
    entitlements: request -> Entitlements (the auth phase plugs in here; default full access)."""
    app = FastAPI(title="Shots board", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    install_security_headers(app)
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(pct=pct, STRONG=flt.STRONG, WHEN=flt.WHEN, SORTS=flt.SORTS, GROUPS=flt.GROUPS,
                                 N_LO=flt.N_LO, N_HI=flt.N_HI, N_DEFAULT=flt.N_DEFAULT, S_LO=flt.S_LO, S_HI=flt.S_HI,
                                 TN_LO=flt.TN_LO, TN_HI=flt.TN_HI, TN_DEFAULT=flt.TN_DEFAULT, SIDES=flt.SIDES,
                                 GLOSSARY=TERMS, PLAN=UPGRADE_PLAN, PLAN_TOP=UPGRADE_PLAN_TOP)
    repo = Repository(db)
    now = today or date.today
    app.state.clock = datetime.now  # local wall clock for "updated 2 h ago"; tests replace it
    ents = entitlements or (lambda request: FULL_ACCESS)
    track_cache: OrderedDict = OrderedDict()  # (src, n, ent) -> (stamp, summary), LRU
    track_avail: OrderedDict = OrderedDict()  # (src, ent) -> Ns with settled rows, from the last computation
    app.state.track_cache = track_cache
    status_cache: dict = {}

    def data_status() -> dict | None:
        hit = status_cache.get("s")
        stamp = time.monotonic()
        if hit is None or stamp - hit[0] > STATUS_CACHE_S:
            try:
                val = repo.data_status(now())
            except Exception as exc:  # freshness is a nicety: never break a page for it
                log.info("no data status: %s", exc)
                val = None
            hit = status_cache["s"] = (stamp, val)
        return hit[1]

    def common(request: Request, page: str) -> dict:
        state = repo.state()
        meta = (repo.board_meta() or {}) if state == "ok" else {}
        stale = None
        if meta.get("built_at"):
            built = datetime.fromisoformat(meta["built_at"]).date()
            if built < now():
                stale = (now() - built).days
        ent = ents(request)
        try:
            auth = authweb.auth_context(request)
            auth_on = True
        except RuntimeError:  # app built without install_auth (tests, read-only previews)
            auth, auth_on = {"user": None, "plan": None, "csrf_token": None}, False
        fresh = freshness(meta, data_status(), app.state.clock()) if state == "ok" else None
        return {"page": page, "meta": meta, "board_missing": state != "ok",
                "board_error": "unreadable" if state == "unreadable" else ("missing" if state != "ok" else None),
                "stale_days": stale, "access": access_summary(ent), "fresh": fresh,
                **auth, "ent": ent, "auth_on": auth_on}

    def render(request: Request, name: str, ctx: dict, status_code: int = 200):
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    @app.exception_handler(StarletteHTTPException)
    async def friendly_http_error(request: Request, exc: StarletteHTTPException):
        accept = request.headers.get("accept", "*/*")
        if exc.status_code == 404 and ("text/html" in accept or "*/*" in accept):
            return render(request, "notfound.html", common(request, "notfound"), 404)
        from fastapi.exception_handlers import http_exception_handler
        return await http_exception_handler(request, exc)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        ctx = common(request, "home")
        ent = ctx["ent"]
        ok = not ctx["board_missing"]
        f = flt.parse(request.query_params, repo.board_leagues(ent) if ok else ())
        if ok:
            t = now()
            view = repo.latest_board(f, t, ent)
            if not view.has_team:  # board without team columns, or no team entitlement: ignore the team filter
                f = replace(f, side="", tn=flt.TN_DEFAULT, tp=0)
            rows = view.rows
            for r in rows:
                r["band"] = _band(r["p"])
                for k in ("home_mean", "away_mean", "tp", "tside"):
                    r.setdefault(k, None)
                if f.side:
                    r["tband"] = _band(r["tp"])
                r["_day"] = r["day"] = r["date"]
            groups: list[dict] = []
            if f.sort == "kickoff":
                for r in rows:
                    if not groups or groups[-1]["day"] != r["_day"]:
                        groups.append({"day": r["_day"], "label": day_label(r["_day"], t), "rows": []})
                    groups[-1]["rows"].append(r)
            strong = sum(1 for r in rows if r["p"] is not None and r["p"] >= flt.STRONG)
            presets = []
            for key, label, blurb, over, needs_team in flt.PRESETS:
                if needs_team and not view.has_team and ctx["access"]["team"] is False:
                    continue  # the board has no team numbers at all: nothing to offer or lock
                pf = replace(flt.Filters(**over), n=f.n)
                presets.append({"key": key, "label": label, "blurb": blurb, "qs": pf.qs(),
                                "locked": needs_team and ctx["access"]["team"],
                                "active": (not (needs_team and ctx["access"]["team"])) and pf.qs() == f.qs()})
            locked_requested = []
            if ctx["access"]["leagues"]:
                visible = set(repo.board_leagues(ent))
                every = set(repo.board_leagues(FULL_ACCESS))
                locked_requested = [v for v in dict.fromkeys(request.query_params.getlist("league"))
                                    if v in every and v not in visible]
            ctx.update(rows=rows, groups=groups, shown=len(rows), strong=strong,
                       n_leagues=len({r["competition"] for r in rows}), total=view.total,
                       league_groups=view.league_groups, today=t, day_label=lambda d: day_label(d, t),
                       chips=[(label, g.qs()) for label, g in f.chips()], has_team=view.has_team,
                       presets=presets, when_opts=when_options(ent), locked_requested=locked_requested,
                       result_text=(f"{len(rows)} of {view.total} matches shown. {strong} reach a "
                                    f"{round(flt.STRONG * 100)}% chance of {f.n}+ shots."))
        else:
            ctx["has_team"] = False
        ctx["f"] = f
        return render(request, "board.html", ctx)

    @app.get("/match/{match_id}", response_class=HTMLResponse)
    def match(request: Request, match_id: str):
        ctx = common(request, "match")
        if ctx["board_missing"]:
            return render(request, "match.html", ctx)
        ent = ctx["ent"]
        r = repo.match_detail(match_id, now(), ent)
        if r is None:
            raise HTTPException(status_code=404, detail="Match not on the board")
        f = flt.parse(request.query_params, repo.board_leagues(ent))
        has_team = r.get("team_ns") is not None
        if not has_team:
            f = replace(f, side="", tn=flt.TN_DEFAULT, tp=0)
        r["_day"] = r["date"]
        curve_ns, curve = r["curve_ns"], [float("nan") if v is None else float(v) for v in r["curve"]]
        n0 = curve_ns[0]

        def at(n):
            return curve[n - n0] if n0 <= n <= curve_ns[-1] else None

        lbs = {q: (int(r[f"lb_{q}"]) if r[f"lb_{q}"] is not None else None) for q in boardmod.BOUNDS}
        bound_rows = [{"q": q, "lb": lb, "p": at(lb) if lb is not None else None} for q, lb in lbs.items()]
        keys = sorted({18, 20, 22, 24, 26, 28, 30, 32, 34, f.n})
        p = at(f.n)
        ctx.update(r=r, f=f, p=p, svg=curve_svg(curve_ns, curve, f.n, lbs),
                   bound_rows=bound_rows, key_rows=[(n, at(n)) for n in keys],
                   curve_rows=[(n, None if pp != pp else pp) for n, pp in zip(curve_ns, curve)],
                   back=("/?" + f.qs()) if f.items() else "/", qs=f.qs(), when=r["_day"],
                   day_label=day_label(r["_day"], now()), ns=(flt.N_LO, flt.N_HI), band=_band(p), has_team=has_team)
        if has_team:
            tns = r["team_ns"]
            ph = [float(v) for v in r["home_curve"]]
            pa = [float(v) for v in r["away_curve"]]
            ctx.update(
                team_svg=team_curve_svg(tns, ph, pa, f.tn, r["home_team"], r["away_team"]),
                team_ns=(tns[0], tns[-1]), tn=f.tn,
                team_p={"home": ph[f.tn - tns[0]], "away": pa[f.tn - tns[0]]},
                team_rows=list(zip(tns, ph, pa)),
                team_bounds=[{"q": q, "home": r.get(f"home_lb_{q}"), "away": r.get(f"away_lb_{q}")}
                             for q in boardmod.BOUNDS])
        return render(request, "match.html", ctx)

    @app.get("/track", response_class=HTMLResponse)
    def track_page(request: Request, n: int = 24, src: str = "backfill"):
        from shots.predict import track

        ctx = common(request, "track")
        ent = ctx["ent"]
        # resolve the query values BEFORE keying the cache: junk src / n must not create entries
        src = src if src in TRACK_SOURCES else TRACK_SOURCES[0]
        avail = track_avail.get((src, ent))
        if avail is not None:  # same resolution as Repository.track_summary
            n = n if n in avail else (24 if 24 in avail or not avail else avail[0])
        key = (src, n, ent)
        hit = track_cache.get(key)
        stamp = datetime.now().timestamp()
        if hit is None or stamp - hit[0] > TRACK_CACHE_S:
            try:
                summary = repo.track_summary(src, n, flt.STRONG, ent)
            except Exception as exc:  # no DB / not migrated: empty state, not a 500
                log.info("no track record: %s", exc)
                summary = dict(src=src, counts={}, avail=[], n=n, has_data=False, metrics=None, total=0, n_comp=0)
            track_avail[(src, ent)] = list(summary["avail"])
            track_avail.move_to_end((src, ent))
            while len(track_avail) > TRACK_CACHE_MAX:
                track_avail.popitem(last=False)
            key = (src, summary["n"], ent)
            hit = track_cache[key] = (stamp, summary)
        track_cache.move_to_end(key)
        while len(track_cache) > TRACK_CACHE_MAX:
            track_cache.popitem(last=False)
        s = hit[1]
        ctx.update(src=s["src"], avail=s["avail"], n=s["n"], counts=s["counts"], has_data=s["has_data"],
                   min_prob=flt.STRONG)
        if s["has_data"]:
            m = s["metrics"]
            rel = m.get("reliability")
            ctx.update(m=m, rel=records(rel[rel["n"] > 0]) if rel is not None else [],
                       rel_svg=Markup(track._svg_reliability(rel)) if rel is not None else "",
                       total=s["total"], n_comp=s["n_comp"])
        return render(request, "track.html", ctx)

    @app.get("/about", response_class=HTMLResponse)
    def about(request: Request):
        return render(request, "about.html", common(request, "about"))

    @app.get("/plans")
    def plans_old():
        return RedirectResponse("/pricing", status_code=308)

    @app.get("/pricing", response_class=HTMLResponse)
    def pricing(request: Request):
        ctx = common(request, "pricing")
        plans = []
        if ctx["auth_on"]:
            current = (ctx["plan"] or {}).get("key")
            for p in authweb.get_service(request).list_plans():
                plans.append({"key": p.key, "name": p.name, "lines": describe_access(p.entitlements),
                              "current": p.key == current})
        ctx["plans"] = plans
        return render(request, "pricing.html", ctx)

    def _auth_form_page(request: Request, name: str):
        ctx = common(request, name)
        if not ctx["auth_on"]:
            raise HTTPException(status_code=404, detail="Accounts are not enabled")
        if ctx["user"] is not None and not request.query_params.get("error"):
            return RedirectResponse(ctx["next"] or "/account", status_code=303)
        ctx.update(authweb.auth_context(request, csrf=True))
        ctx["ent"] = ents(request)
        return render(request, f"{name}.html", ctx)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return _auth_form_page(request, "login")

    @app.get("/register", response_class=HTMLResponse)
    def register_page(request: Request):
        return _auth_form_page(request, "register")

    def _need_user(request: Request):
        try:
            return authweb.require_user(request)
        except RuntimeError:
            raise HTTPException(status_code=404, detail="Accounts are not enabled")

    @app.get("/account", response_class=HTMLResponse)
    def account(request: Request, user=Depends(_need_user)):
        ctx = common(request, "account")
        mine = []
        plan = ctx["plan"]
        for p in authweb.get_service(request).list_plans():
            if plan and p.key == plan["key"]:
                mine = describe_access(p.entitlements)
        ctx["mine"] = mine
        return render(request, "account.html", ctx)

    return app


def _default_app() -> FastAPI:
    from shots.auth.web import entitlements_provider, install_auth

    a = create_app(entitlements=entitlements_provider)  # every page sees the caller's plan
    install_auth(a)
    install_trusted_hosts(a)  # SHOTS_ALLOWED_HOSTS, default 127.0.0.1,localhost (DNS rebinding)
    return a


app = _default_app()
