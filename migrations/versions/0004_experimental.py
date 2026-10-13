"""Experimental predictions (cross-border competitions: the UEFA Champions League).

Champions League fixtures are scored by an experimental model built from each club's domestic form.
There is no source of Champions League shot counts, so these predictions can never be settled, and
their calibration on that competition is unknown. They must be stored (append-only, like every
prediction) but must never count in the official live record.

- predictions.experimental (boolean, NOT NULL, default 0): set once at insert by the loaders, never
  updated (the append-only UPDATE trigger covers it). Existing rows are 0: no experimental
  prediction existed before this revision.
- predictions.data_quality ('full' | 'partial' | 'none', NULL = full): how many of the two clubs had
  domestic shot data behind the prediction.
- v_prediction_outcomes gains the column `experimental`.
- v_official_picks and v_replay_picks exclude experimental predictions: the official record, its
  counts (settled / pending / void) and every metric derived from it are unchanged by them.
- v_experimental_picks: the same shape for live experimental picks, kept apart ("unsettleable"
  today: their status stays 'pending' until a results source exists).

Columns are added with ALTER TABLE ADD COLUMN (no table copy), so the triggers of 0001-0003 stay
in place and no existing prediction row is rewritten.

Revision ID: 0004_experimental
Revises: 0003_run_integrity
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_experimental"
down_revision = "0003_run_integrity"
branch_labels = None
depends_on = None

LOCK_BOUND = "julianday(COALESCE(m.kickoff_utc, datetime(m.date, '+36 hours')))"  # as in 0003

VIEW_ORDER = ("v_prediction_outcomes", "v_prediction_points", "v_official_picks", "v_replay_picks",
              "v_experimental_picks")


def _picks_view(source: str, pre_match_only: bool, experimental: int) -> str:
    """Per (match, N): the last scan of `source` taken on or before the match date."""
    pre = "AND o.pre_match = 1" if pre_match_only else ""
    return f"""
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
          WHERE pt.kind = 'picks' AND json_extract(r.params, '$.source') = '{source}' {pre}
            AND o.experimental = {experimental}
            AND substr(json_extract(r.params, '$.scan_ts'), 1, 10) <= o.date
        ) WHERE rn = 1"""


NEW_VIEWS = {
    "v_prediction_outcomes": f"""
        SELECT p.id AS prediction_id, p.run_id, r.kind, p.match_id, m.date, c.name AS competition,
               CASE WHEN m.status != 'played' THEN 'pending'
                    WHEN m.home_shots IS NULL OR m.away_shots IS NULL THEN 'void'
                    ELSE 'settled' END AS status,
               m.home_shots + m.away_shots AS actual_total, m.home_shots AS actual_home, m.away_shots AS actual_away,
               CASE WHEN p.pre_match
                     AND (m.kickoff_utc IS NULL OR julianday(r.built_at) < julianday(m.kickoff_utc))
                     AND julianday(p.created_at) < {LOCK_BOUND}
                    THEN 1 ELSE 0 END AS pre_match,
               p.experimental AS experimental
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
    # Official live record: live scans, pre-match, never experimental.
    "v_official_picks": _picks_view("live", pre_match_only=True, experimental=0),
    "v_replay_picks": _picks_view("replay", pre_match_only=False, experimental=0),
    # Experimental live picks (no settlement source): kept apart from the official record.
    "v_experimental_picks": _picks_view("live", pre_match_only=True, experimental=1),
}


def _old_views() -> dict:
    """0003 definitions (restored on downgrade)."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("_m0003", Path(__file__).with_name("0003_run_integrity.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.NEW_VIEWS


def _drop_views() -> None:
    for name in reversed(VIEW_ORDER):
        op.execute(f"DROP VIEW IF EXISTS {name}")


def upgrade() -> None:
    _drop_views()  # the views select from predictions: drop before altering it
    op.add_column("predictions", sa.Column("experimental", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("predictions", sa.Column("data_quality", sa.String(length=8), nullable=True))
    for name in VIEW_ORDER:
        op.execute(f"CREATE VIEW {name} AS {NEW_VIEWS[name]}")


def downgrade() -> None:
    _drop_views()
    # SQLite >= 3.35 drops a column in place (no table copy, so the triggers stay as they are).
    op.execute("ALTER TABLE predictions DROP COLUMN data_quality")
    op.execute("ALTER TABLE predictions DROP COLUMN experimental")
    old = _old_views()
    for name in ("v_prediction_outcomes", "v_prediction_points", "v_official_picks", "v_replay_picks"):
        op.execute(f"CREATE VIEW {name} AS {old[name]}")
