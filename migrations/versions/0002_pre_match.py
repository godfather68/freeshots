"""Pre-match integrity: predictions.pre_match + views that count only pre-match predictions.

A board or picks scan built late on a match day can contain matches that have already kicked off.
`predictions.pre_match` records, once, at insert, whether the run was built before the match's
kick-off (kickoff_utc when known, else a conservative estimate; see shots.db.load.lock_moment).
The outcome view re-checks the flag against the current kickoff_utc (a kick-off learned or moved
later can only make it stricter), and v_official_picks counts only pre-match predictions.

Existing rows are back-filled from the data in the DB (kickoff_utc, else 00:00 UTC of the match date).
That back-fill is the only write ever made to existing predictions: it fills a new derived column
inside this migration, with the append-only UPDATE trigger dropped and re-created around it.

Revision ID: 0002_pre_match
Revises: 0001_initial
Create Date: 2026-10-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_pre_match"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

NO_UPDATE_TRIGGER = """CREATE TRIGGER trg_predictions_no_update BEFORE UPDATE ON predictions
       BEGIN SELECT RAISE(ABORT, 'predictions are append-only: updates are not allowed'); END"""

# julianday() compares instants whatever the stored precision ("... 19:00:00" vs "... 19:00:00.000000").
BACKFILL = """
    UPDATE predictions SET pre_match = COALESCE((
        SELECT julianday(r.built_at) < julianday(COALESCE(m.kickoff_utc, m.date))
        FROM prediction_runs r, matches m
        WHERE r.id = predictions.run_id AND m.match_id = predictions.match_id), 0)"""

VIEW_ORDER = ("v_prediction_outcomes", "v_prediction_points", "v_official_picks")  # dependency order

NEW_VIEWS = {
    "v_prediction_outcomes": """
        SELECT p.id AS prediction_id, p.run_id, r.kind, p.match_id, m.date, c.name AS competition,
               CASE WHEN m.status != 'played' THEN 'pending'
                    WHEN m.home_shots IS NULL OR m.away_shots IS NULL THEN 'void'
                    ELSE 'settled' END AS status,
               m.home_shots + m.away_shots AS actual_total, m.home_shots AS actual_home, m.away_shots AS actual_away,
               CASE WHEN p.pre_match AND (m.kickoff_utc IS NULL OR julianday(r.built_at) < julianday(m.kickoff_utc))
                    THEN 1 ELSE 0 END AS pre_match
        FROM predictions p
        JOIN prediction_runs r ON r.id = p.run_id
        JOIN matches m ON m.match_id = p.match_id
        JOIN competitions c ON c.id = m.competition_id""",
    "v_prediction_points": """
        SELECT p.id AS prediction_id, p.run_id, r.kind, p.match_id, CAST(j.key AS INTEGER) AS n, j.value AS p
        FROM predictions p
        JOIN prediction_runs r ON r.id = p.run_id
        JOIN json_each(p.points) j
        WHERE p.points IS NOT NULL""",
    # Official live record: per (match, N) the last PRE-MATCH picks scan taken on or before the match date.
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
          WHERE pt.kind = 'picks' AND o.pre_match = 1
            AND substr(json_extract(r.params, '$.scan_ts'), 1, 10) <= o.date
        ) WHERE rn = 1""",
}

# 0001 definitions, restored on downgrade.
OLD_VIEWS = {
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
    "v_prediction_points": NEW_VIEWS["v_prediction_points"],
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


def _replace_views(views: dict) -> None:
    for name in reversed(VIEW_ORDER):
        op.execute(f"DROP VIEW IF EXISTS {name}")
    for name in VIEW_ORDER:
        op.execute(f"CREATE VIEW {name} AS {views[name]}")


def upgrade() -> None:
    for name in reversed(VIEW_ORDER):  # views select from predictions: drop before altering it
        op.execute(f"DROP VIEW IF EXISTS {name}")
    op.add_column("predictions", sa.Column("pre_match", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("DROP TRIGGER IF EXISTS trg_predictions_no_update")
    op.execute(BACKFILL)
    op.execute(NO_UPDATE_TRIGGER)
    _replace_views(NEW_VIEWS)


def downgrade() -> None:
    for name in reversed(VIEW_ORDER):
        op.execute(f"DROP VIEW IF EXISTS {name}")
    # SQLite >= 3.35 drops a column in place (no table copy, so the triggers stay as they are).
    op.execute("ALTER TABLE predictions DROP COLUMN pre_match")
    _replace_views(OLD_VIEWS)
