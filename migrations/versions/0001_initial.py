"""Initial schema: football data, prediction runs, append-only predictions, outcome views, job runs,
and the (empty) account tables with seeded placeholder plans.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

TOP5 = ["England Premier League", "Spain La Liga", "Italy Serie A", "Germany Bundesliga", "France Ligue 1"]

# Placeholder plans (product decision pending). Values are JSON; null max_days_ahead = whole season.
PLANS = [
    ("free", "Free", True, 1, {"can_view_all_leagues": False, "allowed_leagues": TOP5, "max_days_ahead": 3,
                                "team_predictions": False, "export": False, "track_detail": False}),
    ("pro", "Pro", False, 2, {"can_view_all_leagues": True, "allowed_leagues": None, "max_days_ahead": 30,
                              "team_predictions": True, "export": False, "track_detail": False}),
    ("premium", "Premium", False, 3, {"can_view_all_leagues": True, "allowed_leagues": None, "max_days_ahead": None,
                                      "team_predictions": True, "export": True, "track_detail": True}),
]

# Lock moment of a match: kick-off in UTC when known, else the start of the match date (UTC), which
# is before any European kick-off. A played match is always locked.
_LOCKED = ("(SELECT m.status = 'played' OR COALESCE(m.kickoff_utc, m.date || ' 00:00:00') "
           "<= strftime('%Y-%m-%d %H:%M:%S', 'now') FROM matches m WHERE m.match_id = OLD.match_id)")

TRIGGERS = [
    """CREATE TRIGGER trg_predictions_no_update BEFORE UPDATE ON predictions
       BEGIN SELECT RAISE(ABORT, 'predictions are append-only: updates are not allowed'); END""",
    f"""CREATE TRIGGER trg_predictions_no_delete_after_kickoff BEFORE DELETE ON predictions
       WHEN {_LOCKED}
       BEGIN SELECT RAISE(ABORT, 'predictions are append-only: cannot delete a prediction after kick-off'); END""",
]

VIEWS = {
    # Outcome of every prediction, derived from the current result (results can be corrected upstream,
    # so this is a view rather than a copied table).
    "v_prediction_outcomes": """
        SELECT p.id AS prediction_id, p.run_id, r.kind, p.match_id, m.date, c.name AS competition,
               CASE WHEN m.status != 'played' THEN 'pending'
                    WHEN m.home_shots IS NULL OR m.away_shots IS NULL THEN 'void'
                    ELSE 'settled' END AS status,
               m.home_shots + m.away_shots AS actual_total, m.home_shots AS actual_home, m.away_shots AS actual_away
        FROM predictions p
        JOIN prediction_runs r ON r.id = p.run_id
        JOIN matches m ON m.match_id = p.match_id
        JOIN competitions c ON c.id = m.competition_id""",
    # Sparse probabilities (picks, backfill) as rows: one per (prediction, N).
    "v_prediction_points": """
        SELECT p.id AS prediction_id, p.run_id, r.kind, p.match_id, CAST(j.key AS INTEGER) AS n, j.value AS p
        FROM predictions p
        JOIN prediction_runs r ON r.id = p.run_id
        JOIN json_each(p.points) j
        WHERE p.points IS NOT NULL""",
    # Official live record: per (match, N) the last picks scan taken on or before the match date
    # (same rule as shots.predict.track.dedupe), with its outcome.
    "v_official_picks": """
        SELECT * FROM (
          SELECT pt.prediction_id, pt.run_id, pt.match_id, o.date, o.competition, pt.n, pt.p,
                 json_extract(r.params, '$.scan_ts') AS scan_ts, json_extract(r.params, '$.source') AS source,
                 r.source_key, o.status, o.actual_total,
                 CASE WHEN o.status = 'settled' THEN o.actual_total >= pt.n END AS hit,
                 ROW_NUMBER() OVER (PARTITION BY pt.match_id, pt.n
                                    ORDER BY json_extract(r.params, '$.scan_ts') DESC, r.source_key DESC) AS rn
          FROM v_prediction_points pt
          JOIN prediction_runs r ON r.id = pt.run_id
          JOIN v_prediction_outcomes o ON o.prediction_id = pt.prediction_id
          WHERE pt.kind = 'picks' AND substr(json_extract(r.params, '$.scan_ts'), 1, 10) <= o.date
        ) WHERE rn = 1""",
}


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "competitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("country", sa.String(40), nullable=False),
        sa.Column("tier", sa.Integer()),
        sa.Column("code", sa.String(8)),
    )
    op.create_table(
        "teams",
        sa.Column("team_id", sa.String(80), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("country", sa.String(40), nullable=False),
    )
    op.create_table(
        "matches",
        sa.Column("match_id", sa.String(32), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("competition_id", sa.Integer(), sa.ForeignKey("competitions.id"), nullable=False),
        sa.Column("season", sa.String(9)),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("kickoff_local", sa.String(5)),
        sa.Column("kickoff_utc", sa.DateTime()),
        sa.Column("round", sa.String(60)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("home_team_id", sa.String(80), sa.ForeignKey("teams.team_id"), nullable=False),
        sa.Column("away_team_id", sa.String(80), sa.ForeignKey("teams.team_id"), nullable=False),
        sa.Column("home_team", sa.String(80), nullable=False),
        sa.Column("away_team", sa.String(80), nullable=False),
        sa.Column("search_key", sa.String(170), nullable=False),
        *[sa.Column(c, sa.Integer()) for c in ("home_shots", "away_shots", "home_sot", "away_sot",
                                               "home_goals", "away_goals")],
        *[sa.Column(c, sa.Float()) for c in ("odds_home", "odds_draw", "odds_away", "odds_over25", "odds_under25")],
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_matches_date", "matches", ["date"])
    op.create_index("ix_matches_competition_date", "matches", ["competition_id", "date"])
    op.create_index("ix_matches_status_date", "matches", ["status", "date"])
    op.create_index("ix_matches_home_team", "matches", ["home_team_id"])
    op.create_index("ix_matches_away_team", "matches", ["away_team_id"])

    op.create_table(
        "prediction_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("source_key", sa.String(200), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        _ts("built_at"), _ts("confirmed_at", nullable=True),
        sa.Column("results_up_to", sa.Date()),
        sa.Column("window_from", sa.Date()),
        sa.Column("window_days", sa.Integer()),
        sa.Column("models", sa.JSON(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("curve_n_min", sa.Integer()), sa.Column("curve_n_max", sa.Integer()),
        sa.Column("team_n_min", sa.Integer()), sa.Column("team_n_max", sa.Integer()),
        sa.Column("n_predictions", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64)),
        _ts("created_at"),
        sa.CheckConstraint("kind IN ('board', 'picks', 'backfill')", name="ck_prediction_runs_kind"),
    )
    op.create_index("ix_prediction_runs_kind_built", "prediction_runs", ["kind", "built_at"])

    op.create_table(
        "predictions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("prediction_runs.id"), nullable=False),
        sa.Column("match_id", sa.String(32), sa.ForeignKey("matches.match_id"), nullable=False),
        sa.Column("model", sa.String(120)),
        sa.Column("odds_available", sa.Boolean()),
        sa.Column("cold_start", sa.Boolean()),
        sa.Column("median", sa.Float()),
        sa.Column("lb_70", sa.Integer()), sa.Column("lb_80", sa.Integer()), sa.Column("lb_90", sa.Integer()),
        sa.Column("league_avg", sa.Float()),
        sa.Column("above_league", sa.Float()),
        sa.Column("mkt_p_over25", sa.Float()),
        sa.Column("total_curve", sa.JSON()),
        sa.Column("points", sa.JSON()),
        sa.Column("selected", sa.Boolean()),
        *[c for side in ("home", "away") for c in (
            sa.Column(f"{side}_mean", sa.Float()), sa.Column(f"{side}_median", sa.Integer()),
            sa.Column(f"{side}_lb_70", sa.Integer()), sa.Column(f"{side}_lb_80", sa.Integer()),
            sa.Column(f"{side}_lb_90", sa.Integer()))],
        sa.Column("home_curve", sa.JSON()),
        sa.Column("away_curve", sa.JSON()),
        _ts("created_at"),
        sa.UniqueConstraint("run_id", "match_id", name="uq_predictions_run_match"),
    )
    op.create_index("ix_predictions_match", "predictions", ["match_id"])

    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job", sa.String(40), nullable=False),
        _ts("started_at"), _ts("finished_at", nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("host", sa.String(80)),
        sa.Column("pid", sa.Integer()),
    )
    op.create_index("ix_job_runs_job_started", "job_runs", ["job", "started_at"])

    plans = op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(32), nullable=False, unique=True),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        _ts("created_at"),
    )
    entitlements = op.create_table(
        "plan_entitlements",
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("key", sa.String(48), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=True),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(80)),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        _ts("email_verified_at", True), _ts("password_changed_at", True), _ts("locked_until", True),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        _ts("current_period_start", True), _ts("current_period_end", True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False),
        sa.Column("provider", sa.String(32)),
        sa.Column("provider_customer_id", sa.String(120)),
        sa.Column("provider_subscription_id", sa.String(120), unique=True),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_subscriptions_user_status", "subscriptions", ["user_id", "status"])
    op.create_table(
        "sessions",
        sa.Column("id_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("csrf_hash", sa.String(64)),
        _ts("created_at"), _ts("last_seen_at"), _ts("expires_at"), _ts("revoked_at", True),
        sa.Column("ip_hash", sa.String(64)),
        sa.Column("ua_hash", sa.String(64)),
    )
    op.create_index("ix_sessions_user", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires", "sessions", ["expires_at"])
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("account_hash", sa.String(64)),
        sa.Column("client_hash", sa.String(64)),
        sa.Column("success", sa.Boolean(), nullable=False),
        _ts("at"),
    )
    op.create_index("ix_login_attempts_account_at", "login_attempts", ["account_hash", "at"])
    op.create_index("ix_login_attempts_client_at", "login_attempts", ["client_hash", "at"])

    for sql in TRIGGERS:
        op.execute(sql)
    for name, sql in VIEWS.items():
        op.execute(f"CREATE VIEW {name} AS {sql}")

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    op.bulk_insert(plans, [dict(id=i, key=k, name=n, is_default=d, is_active=True, sort_order=o, created_at=now)
                           for i, (k, n, d, o, _) in enumerate(PLANS, 1)])
    op.bulk_insert(entitlements, [dict(plan_id=i, key=key, value=value)
                                  for i, (*_, ents) in enumerate(PLANS, 1) for key, value in ents.items()])


def downgrade() -> None:
    for name in reversed(list(VIEWS)):
        op.execute(f"DROP VIEW IF EXISTS {name}")
    op.execute("DROP TRIGGER IF EXISTS trg_predictions_no_delete_after_kickoff")
    op.execute("DROP TRIGGER IF EXISTS trg_predictions_no_update")
    for table in ("login_attempts", "sessions", "subscriptions", "users", "plan_entitlements", "plans",
                  "job_runs", "predictions", "prediction_runs", "matches", "teams", "competitions"):
        op.drop_table(table)
