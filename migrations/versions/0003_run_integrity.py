"""Close the append-only bypasses found by the 2026-10-09 DB verification, and keep replays and
late inserts out of the official live record.

2a  `INSERT OR REPLACE` (and an UPSERT) on predictions deleted-and-reinserted a locked row: SQLite does
    not fire DELETE triggers for REPLACE's implicit delete. A BEFORE INSERT trigger now aborts any insert
    whose id or (run_id, match_id) already exists, before the conflict resolution runs.
2b  prediction_runs had no triggers: deleting a run orphaned its predictions (with foreign_keys off) and
    editing built_at / params.scan_ts changed which pick is official. Now: a run that has predictions
    cannot be deleted; id, kind, source_key, built_at, params, models, content_hash and created_at can
    never change (confirmed_at, meta, n_predictions and status stay writable: import_board bumps them).
2c  pre_match was derived from the run's claimed time only. Now:
    - predictions.created_at must be the DB clock at insert (within 5 minutes of `now`);
    - a row may claim pre_match = 1 only if, at insert, its match was not played and both the insert
      time and the run's built_at were before the match's latest possible lock (kickoff_utc, else the
      match date + 36 h; the loader applies the precise lock moment, this is the DB-side bound);
    - the views re-check the insert time: v_prediction_outcomes.pre_match also requires
      created_at < that bound (catches rows inserted before this migration);
    - v_official_picks counts **live** scans only (params.source = 'live'); replays (`replay_*` files,
      params.source = 'replay', made after the fact) are queryable separately in v_replay_picks.

Revision ID: 0003_run_integrity
Revises: 0002_pre_match
Create Date: 2026-10-09
"""

from __future__ import annotations

from alembic import op

revision = "0003_run_integrity"
down_revision = "0002_pre_match"
branch_labels = None
depends_on = None

# Latest possible lock moment of a match known to the DB (the loader uses a tighter one with the row's
# HH:MM hint): kickoff_utc, else 36 h after 00:00 of the match date (covers late local kick-offs in
# any time zone).
LOCK_BOUND = "julianday(COALESCE(m.kickoff_utc, datetime(m.date, '+36 hours')))"
CREATED_TOLERANCE_DAYS = "(5.0 / 1440)"  # 5 minutes

TRIGGERS = {
    "trg_predictions_no_replace": """
        CREATE TRIGGER trg_predictions_no_replace BEFORE INSERT ON predictions
        WHEN EXISTS (SELECT 1 FROM predictions
                     WHERE id = NEW.id OR (run_id = NEW.run_id AND match_id = NEW.match_id))
        BEGIN SELECT RAISE(ABORT, 'predictions are append-only: replacing a prediction is not allowed'); END""",
    "trg_predictions_insert_time": f"""
        CREATE TRIGGER trg_predictions_insert_time BEFORE INSERT ON predictions
        WHEN NEW.created_at IS NULL
          OR abs(julianday(NEW.created_at) - julianday('now')) > {CREATED_TOLERANCE_DAYS}
        BEGIN SELECT RAISE(ABORT, 'predictions.created_at must be the insert time (DB clock)'); END""",
    "trg_predictions_pre_match_claim": f"""
        CREATE TRIGGER trg_predictions_pre_match_claim BEFORE INSERT ON predictions
        WHEN NEW.pre_match
         AND COALESCE((SELECT m.status = 'played'
                              OR julianday(NEW.created_at) >= {LOCK_BOUND}
                              OR julianday(r.built_at) >= {LOCK_BOUND}
                       FROM matches m, prediction_runs r
                       WHERE m.match_id = NEW.match_id AND r.id = NEW.run_id), 1)
        BEGIN SELECT RAISE(ABORT, 'pre_match = 1 for a prediction made or inserted after its match locked'); END""",
    "trg_prediction_runs_no_delete": """
        CREATE TRIGGER trg_prediction_runs_no_delete BEFORE DELETE ON prediction_runs
        WHEN EXISTS (SELECT 1 FROM predictions WHERE run_id = OLD.id)
        BEGIN SELECT RAISE(ABORT, 'prediction runs with predictions cannot be deleted'); END""",
    "trg_prediction_runs_immutable": """
        CREATE TRIGGER trg_prediction_runs_immutable BEFORE UPDATE ON prediction_runs
        WHEN NEW.id IS NOT OLD.id OR NEW.kind IS NOT OLD.kind OR NEW.source_key IS NOT OLD.source_key
          OR NEW.built_at IS NOT OLD.built_at OR NEW.params IS NOT OLD.params OR NEW.models IS NOT OLD.models
          OR NEW.content_hash IS NOT OLD.content_hash OR NEW.created_at IS NOT OLD.created_at
        BEGIN SELECT RAISE(ABORT, 'prediction runs are append-only: identity and timing columns cannot change'); END""",
}

VIEW_ORDER = ("v_prediction_outcomes", "v_prediction_points", "v_official_picks", "v_replay_picks")


def _picks_view(source: str, pre_match_only: bool) -> str:
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
    # Official live record: live scans only, pre-match (made AND inserted before the lock moment).
    "v_official_picks": _picks_view("live", pre_match_only=True),
    # Replays: labelled after-the-fact re-runs of past rounds; never part of the official record.
    "v_replay_picks": _picks_view("replay", pre_match_only=False),
}


def _old_views() -> dict:
    """0002 definitions (restored on downgrade)."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("_m0002", Path(__file__).with_name("0002_pre_match.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.NEW_VIEWS


def _drop_views() -> None:
    for name in reversed(VIEW_ORDER):
        op.execute(f"DROP VIEW IF EXISTS {name}")


def upgrade() -> None:
    _drop_views()
    for name in VIEW_ORDER:
        op.execute(f"CREATE VIEW {name} AS {NEW_VIEWS[name]}")
    for name, sql in TRIGGERS.items():
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
        op.execute(sql)


def downgrade() -> None:
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    _drop_views()
    old = _old_views()
    for name in ("v_prediction_outcomes", "v_prediction_points", "v_official_picks"):
        op.execute(f"CREATE VIEW {name} AS {old[name]}")
