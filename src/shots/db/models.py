"""SQLAlchemy 2.0 ORM models: the schema of data/shots.db.

The schema itself is created and changed only by Alembic migrations (migrations/versions/);
these classes must stay in sync with them (tests/test_db_migrations.py compares both).

Conventions
- Audit timestamps (*_at) are UTC, stored naive. Match `date` is the local match date and
  `kickoff_local` the local kick-off "HH:MM", as in the canonical schema.
- Probability curves are JSON arrays of floats (see docs/platform.md for the benchmark):
  `total_curve[i]` = P(total shots >= run.curve_n_min + i), `home_curve[i]` / `away_curve[i]` =
  P(team shots >= run.team_n_min + i). Sparse probabilities (picks, backfill) are a JSON object
  `points` {"24": 0.71}.
- Predictions are append-only (DB triggers): never updated; deleted only before the match locks.
- `predictions.pre_match` is fixed at insert: the run was built before the match's kick-off (see
  shots.db.load.lock_moment). Only pre-match predictions count in the official track record.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (JSON, Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer,
                        String, Text, UniqueConstraint, false)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# ----------------------------------------------------------------------------- football data

class Competition(Base):
    __tablename__ = "competitions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)  # canonical "competition", e.g. "Spain La Liga"
    country: Mapped[str] = mapped_column(String(40))
    tier: Mapped[int | None] = mapped_column(Integer)
    code: Mapped[str | None] = mapped_column(String(8))  # football-data division code, e.g. "SP1"


class Team(Base):
    __tablename__ = "teams"
    team_id: Mapped[str] = mapped_column(String(80), primary_key=True)  # "<country>:<slug>"
    name: Mapped[str] = mapped_column(String(80))  # latest normalized name
    country: Mapped[str] = mapped_column(String(40))


class Match(Base):
    """Every match we know of, played or upcoming (one row per canonical match_id).

    status: "played" (result row from matches.parquet, or a manual result: source "manual", migration
    0006, or a provisional result of a football-data competition: source "provisional", migration 0007,
    until football-data's own result replaces it), "scheduled" (listed as an upcoming fixture), "unlisted" (was a fixture, no longer listed by any
    fixture source, e.g. postponed)."""
    __tablename__ = "matches"
    match_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    competition_id: Mapped[int] = mapped_column(ForeignKey("competitions.id"))
    season: Mapped[str | None] = mapped_column(String(9))
    date: Mapped[date] = mapped_column(Date)
    kickoff_local: Mapped[str | None] = mapped_column(String(5))
    kickoff_utc: Mapped[datetime | None] = mapped_column(DateTime)
    round: Mapped[str | None] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(16), default="scheduled")
    home_team_id: Mapped[str] = mapped_column(ForeignKey("teams.team_id"))
    away_team_id: Mapped[str] = mapped_column(ForeignKey("teams.team_id"))
    home_team: Mapped[str] = mapped_column(String(80))
    away_team: Mapped[str] = mapped_column(String(80))
    search_key: Mapped[str] = mapped_column(String(170))  # accent/case-folded "home | away"
    home_shots: Mapped[int | None] = mapped_column(Integer)
    away_shots: Mapped[int | None] = mapped_column(Integer)
    home_sot: Mapped[int | None] = mapped_column(Integer)
    away_sot: Mapped[int | None] = mapped_column(Integer)
    home_goals: Mapped[int | None] = mapped_column(Integer)
    away_goals: Mapped[int | None] = mapped_column(Integer)
    odds_home: Mapped[float | None] = mapped_column(Float)
    odds_draw: Mapped[float | None] = mapped_column(Float)
    odds_away: Mapped[float | None] = mapped_column(Float)
    odds_over25: Mapped[float | None] = mapped_column(Float)
    odds_under25: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    competition: Mapped[Competition] = relationship()

    __table_args__ = (
        Index("ix_matches_date", "date"),
        Index("ix_matches_competition_date", "competition_id", "date"),
        Index("ix_matches_status_date", "status", "date"),
        Index("ix_matches_home_team", "home_team_id"),
        Index("ix_matches_away_team", "away_team_id"),
    )


class ManualResult(Base):
    """Migration 0006: a result entered by hand (results_manual/*.csv) for a match of a competition with no
    automatic results source (cross-border competitions). The values are also written to the `matches`
    row (status 'played', source 'manual'), which is what outcomes are derived from; this row keeps what
    `matches` cannot: the provider URL, when it was entered, notes, and the match's source / status before
    it was settled (restored if the CSV row is removed). Not append-only: corrections update it, and every
    change is recorded in manual_result_edits. Triggers keep it off domestic / football-data matches."""
    __tablename__ = "manual_results"
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.match_id"), primary_key=True)
    file: Mapped[str] = mapped_column(String(120))
    home_shots: Mapped[int] = mapped_column(Integer)
    away_shots: Mapped[int] = mapped_column(Integer)
    home_goals: Mapped[int] = mapped_column(Integer)
    away_goals: Mapped[int] = mapped_column(Integer)
    home_sot: Mapped[int | None] = mapped_column(Integer)
    away_sot: Mapped[int | None] = mapped_column(Integer)
    source_url: Mapped[str] = mapped_column(Text)
    entered_at: Mapped[datetime] = mapped_column(DateTime)  # UTC, as stated in the CSV
    notes: Mapped[str | None] = mapped_column(Text)
    prev_source: Mapped[str] = mapped_column(String(32))
    prev_status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint("home_shots >= 0 AND away_shots >= 0 AND home_goals >= 0 AND away_goals >= 0",
                        name="ck_manual_results_non_negative"),
        CheckConstraint("(home_sot IS NULL OR (home_sot >= 0 AND home_sot <= home_shots)) AND "
                        "(away_sot IS NULL OR (away_sot >= 0 AND away_sot <= away_shots))",
                        name="ck_manual_results_sot"),
    )


class ManualResultEdit(Base):
    """Audit trail of manual results (append-only: triggers forbid UPDATE and DELETE). action: insert |
    update | delete; changes: {column: [old, new]} (only the columns that changed)."""
    __tablename__ = "manual_result_edits"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.match_id"))
    action: Mapped[str] = mapped_column(String(8))
    changes: Mapped[dict] = mapped_column(JSON)
    file: Mapped[str] = mapped_column(String(120))
    entered_at: Mapped[datetime | None] = mapped_column(DateTime)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint("action IN ('insert', 'update', 'delete')", name="ck_manual_result_edits_action"),
        Index("ix_manual_result_edits_match", "match_id", "recorded_at"),
    )


class ProvisionalResult(Base):
    """Migration 0007: a result entered by hand (results_provisional/*.csv) for a match of a football-data
    competition that football-data has not published yet. Its values are written to the `matches` row
    (status 'played', source 'provisional') until football-data's own result replaces them (source
    'football-data': the result is then confirmed). This row is kept: it is the provider's side of the
    agreement audit (v_provisional_agreement). Corrections update it; provisional_result_edits records
    every change and each confirmation. Triggers keep it off competitions without a division code."""
    __tablename__ = "provisional_results"
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.match_id"), primary_key=True)
    file: Mapped[str] = mapped_column(String(120))
    home_shots: Mapped[int] = mapped_column(Integer)
    away_shots: Mapped[int] = mapped_column(Integer)
    home_goals: Mapped[int] = mapped_column(Integer)
    away_goals: Mapped[int] = mapped_column(Integer)
    home_sot: Mapped[int | None] = mapped_column(Integer)
    away_sot: Mapped[int | None] = mapped_column(Integer)
    source_url: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(40))  # label of the source host, e.g. "espn"
    entered_at: Mapped[datetime] = mapped_column(DateTime)  # UTC, as stated in the CSV
    notes: Mapped[str | None] = mapped_column(Text)
    prev_source: Mapped[str] = mapped_column(String(32))
    prev_status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint("home_shots >= 0 AND away_shots >= 0 AND home_goals >= 0 AND away_goals >= 0",
                        name="ck_provisional_results_non_negative"),
        CheckConstraint("(home_sot IS NULL OR (home_sot >= 0 AND home_sot <= home_shots)) AND "
                        "(away_sot IS NULL OR (away_sot >= 0 AND away_sot <= away_shots))",
                        name="ck_provisional_results_sot"),
    )


class ProvisionalResultEdit(Base):
    """Audit trail of provisional results (append-only: triggers forbid UPDATE and DELETE). action: insert |
    update | delete (the CSV changed; changes = {column: [old, new]} of the changed columns) | confirm
    (football-data replaced the provisional result, written by a trigger; changes = {column: [provisional,
    football-data]} of every result column)."""
    __tablename__ = "provisional_result_edits"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.match_id"))
    action: Mapped[str] = mapped_column(String(8))
    changes: Mapped[dict] = mapped_column(JSON)
    file: Mapped[str] = mapped_column(String(120))
    entered_at: Mapped[datetime | None] = mapped_column(DateTime)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint("action IN ('insert', 'update', 'delete', 'confirm')", name="ck_provisional_result_edits_action"),
        Index("ix_provisional_result_edits_match", "match_id", "recorded_at"),
    )


# ----------------------------------------------------------------------------- predictions

class PredictionRun(Base):
    """One batch of predictions. kind: board | picks | backfill (picks: params.source live | replay).
    Append-only (migration 0003): a run with predictions cannot be deleted; id, kind, source_key,
    built_at, params, models, content_hash and created_at never change.

    source_key is unique and makes imports idempotent ("board:<built_at>", "picks:<file>",
    "backfill:<content hash>"). content_hash lets an identical board rebuild only bump
    confirmed_at instead of storing 600 identical rows again."""
    __tablename__ = "prediction_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    source_key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="complete")
    built_at: Mapped[datetime] = mapped_column(DateTime)  # UTC
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)  # UTC, last identical rebuild
    results_up_to: Mapped[date | None] = mapped_column(Date)
    window_from: Mapped[date | None] = mapped_column(Date)
    window_days: Mapped[int | None] = mapped_column(Integer)  # None = whole season / not applicable
    models: Mapped[list] = mapped_column(JSON, default=list)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)  # board.meta.json as written by the board build
    curve_n_min: Mapped[int | None] = mapped_column(Integer)
    curve_n_max: Mapped[int | None] = mapped_column(Integer)
    team_n_min: Mapped[int | None] = mapped_column(Integer)
    team_n_max: Mapped[int | None] = mapped_column(Integer)
    n_predictions: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint("kind IN ('board', 'picks', 'backfill')", name="ck_prediction_runs_kind"),
        Index("ix_prediction_runs_kind_built", "kind", "built_at"),
    )


class Prediction(Base):
    """One match scored by one run. Append-only (triggers of migrations 0001 and 0003: no UPDATE, no
    DELETE once locked, no REPLACE / UPSERT of an existing row).
    pre_match: set once at insert (migration 0002): run built and row inserted before the match's lock
    moment; the DB rejects a pre_match claim it can disprove (0003). created_at = DB clock at insert
    (+-5 min, enforced). The views re-check both against the current kickoff_utc, so a kick-off learned
    or moved later can only make the flag stricter."""
    __tablename__ = "predictions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("prediction_runs.id"))
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.match_id"))
    model: Mapped[str | None] = mapped_column(String(120))
    odds_available: Mapped[bool | None] = mapped_column(Boolean)
    cold_start: Mapped[bool | None] = mapped_column(Boolean)
    median: Mapped[float | None] = mapped_column(Float)  # "expected_shots" on the board
    lb_70: Mapped[int | None] = mapped_column(Integer)
    lb_80: Mapped[int | None] = mapped_column(Integer)
    lb_90: Mapped[int | None] = mapped_column(Integer)
    league_avg: Mapped[float | None] = mapped_column(Float)
    above_league: Mapped[float | None] = mapped_column(Float)
    mkt_p_over25: Mapped[float | None] = mapped_column(Float)
    total_curve: Mapped[list | None] = mapped_column(JSON)
    points: Mapped[dict | None] = mapped_column(JSON)
    selected: Mapped[bool | None] = mapped_column(Boolean)
    home_mean: Mapped[float | None] = mapped_column(Float)
    home_median: Mapped[int | None] = mapped_column(Integer)
    home_lb_70: Mapped[int | None] = mapped_column(Integer)
    home_lb_80: Mapped[int | None] = mapped_column(Integer)
    home_lb_90: Mapped[int | None] = mapped_column(Integer)
    away_mean: Mapped[float | None] = mapped_column(Float)
    away_median: Mapped[int | None] = mapped_column(Integer)
    away_lb_70: Mapped[int | None] = mapped_column(Integer)
    away_lb_80: Mapped[int | None] = mapped_column(Integer)
    away_lb_90: Mapped[int | None] = mapped_column(Integer)
    home_curve: Mapped[list | None] = mapped_column(JSON)
    away_curve: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # run built before the match's kick-off (kickoff_utc, else a conservative estimate); migration 0002
    pre_match: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # migration 0004: scored by an experimental model (cross-border competitions; no settlement source).
    # Never part of the official record (v_official_picks, results export). data_quality: full | partial |
    # none = how many of the two clubs had domestic shot data (NULL = full).
    experimental: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    data_quality: Mapped[str | None] = mapped_column(String(8))
    # migration 0005: board rows only. The additive decomposition of the expected total (shots.models.explain),
    # computed with the prediction from the same model and feature row; never contains odds values.
    explain: Mapped[dict | None] = mapped_column(JSON)

    run: Mapped[PredictionRun] = relationship()
    match: Mapped[Match] = relationship()

    __table_args__ = (
        UniqueConstraint("run_id", "match_id", name="uq_predictions_run_match"),
        Index("ix_predictions_match", "match_id"),
    )


# ----------------------------------------------------------------------------- operations

class JobRun(Base):
    """One update / worker cycle: start, end, status (running | ok | failed | partial), counts, error."""
    __tablename__ = "job_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job: Mapped[str] = mapped_column(String(40))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="running")
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    host: Mapped[str | None] = mapped_column(String(80))
    pid: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (Index("ix_job_runs_job_started", "job", "started_at"),)


# ----------------------------------------------------------------------------- accounts (auth-ready, no logic yet)

class Plan(Base):
    """Plans are data: code checks entitlements (plan_entitlements), never plan keys."""
    __tablename__ = "plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(60))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)  # plan given to new registrations
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PlanEntitlement(Base):
    """value is JSON: true/false, an integer, null (= unlimited) or a list (e.g. allowed leagues)."""
    __tablename__ = "plan_entitlements"
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(String(48), primary_key=True)
    value: Mapped[object] = mapped_column(JSON, nullable=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)  # stored normalized (lower-case, trimmed)
    password_hash: Mapped[str] = mapped_column(String(255))  # Argon2id encoded hash, never the password
    display_name: Mapped[str | None] = mapped_column(String(80))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Subscription(Base):
    """status: active | trialing | past_due | canceled | expired. provider_* are for a future
    payment provider (e.g. Stripe) and stay NULL for manual/free plans."""
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    status: Mapped[str] = mapped_column(String(16), default="active")
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    provider: Mapped[str | None] = mapped_column(String(32))
    provider_customer_id: Mapped[str | None] = mapped_column(String(120))
    provider_subscription_id: Mapped[str | None] = mapped_column(String(120), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (Index("ix_subscriptions_user_status", "user_id", "status"),)


class UserSession(Base):
    """Server-side session. id_hash = SHA-256 of the random session id (the raw id lives only in
    the cookie). user_id is NULL for anonymous sessions (CSRF on login/registration forms)."""
    __tablename__ = "sessions"
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    csrf_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)  # absolute expiry; idle expiry is last_seen_at based
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    ua_hash: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (Index("ix_sessions_user", "user_id"), Index("ix_sessions_expires", "expires_at"))


class LoginAttempt(Base):
    """Rate limiting / lockout input. kind: login | register. Keys are hashes (no raw emails or IPs)."""
    __tablename__ = "login_attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    account_hash: Mapped[str | None] = mapped_column(String(64))
    client_hash: Mapped[str | None] = mapped_column(String(64))
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (Index("ix_login_attempts_account_at", "account_hash", "at"),
                      Index("ix_login_attempts_client_at", "client_hash", "at"))
