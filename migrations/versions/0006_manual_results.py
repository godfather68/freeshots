"""Manual results for competitions without an automatic results source (UEFA Champions League).

Experimental (cross-border) fixtures had no settlement source. A human now enters their final shot
counts in a committed CSV (results_manual/<competition>_<season>.csv, shots.ingest.manual_results);
the update loads it with shots.db.manual. The result goes where every result goes, the `matches` row
(status 'played', source 'manual'), so outcomes stay derived views and a correction propagates. What
the CSV adds that `matches` cannot hold lives in two new tables:

- manual_results: one row per manually settled match: the values as entered, the provider URL
  (`source_url`), when they were entered (`entered_at`, from the CSV), optional notes, the file, and the
  match's `source` / `status` before it was settled (restored if the row is removed from the CSV).
  Results are not append-only: a correction updates this row and the match.
- manual_result_edits: the audit trail of manual results. One row per insert / update / delete
  (`changes` = {column: [old, new]}), append-only (no UPDATE, no DELETE: triggers).

Guards (triggers, so they hold for any client):
- matches: a row may only carry source 'manual' when its competition has no football-data division code
  (domestic leagues all have one), and a football-data match can never become 'manual'.
- manual_results: only for a match whose competition has no division code and whose source is not
  football-data.

No view changes: v_official_picks, v_replay_picks and v_experimental_picks are exactly those of 0004.
Experimental predictions settle against the manual result through v_prediction_outcomes (status
'settled') and are counted in v_experimental_picks, never in the official live record.

Revision ID: 0006_manual_results
Revises: 0005_explain
Create Date: 2026-10-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_manual_results"
down_revision = "0005_explain"
branch_labels = None
depends_on = None

_HAS_CODE = "(SELECT code FROM competitions WHERE id = NEW.competition_id) IS NOT NULL"

TRIGGERS = {
    "trg_matches_manual_scope_insert": f"""
        CREATE TRIGGER trg_matches_manual_scope_insert BEFORE INSERT ON matches
        WHEN NEW.source = 'manual' AND {_HAS_CODE}
        BEGIN SELECT RAISE(ABORT, 'manual results are only allowed for competitions without an automatic source'); END""",
    "trg_matches_manual_scope_update": f"""
        CREATE TRIGGER trg_matches_manual_scope_update BEFORE UPDATE OF source, competition_id ON matches
        WHEN NEW.source = 'manual' AND (OLD.source = 'football-data' OR {_HAS_CODE})
        BEGIN SELECT RAISE(ABORT, 'manual results are only allowed for competitions without an automatic source'); END""",
    "trg_manual_results_scope": """
        CREATE TRIGGER trg_manual_results_scope BEFORE INSERT ON manual_results
        WHEN COALESCE((SELECT c.code IS NOT NULL OR m.source = 'football-data'
                       FROM matches m JOIN competitions c ON c.id = m.competition_id
                       WHERE m.match_id = NEW.match_id), 1)
        BEGIN SELECT RAISE(ABORT, 'manual results are only allowed for competitions without an automatic source'); END""",
    "trg_manual_result_edits_no_update": """
        CREATE TRIGGER trg_manual_result_edits_no_update BEFORE UPDATE ON manual_result_edits
        BEGIN SELECT RAISE(ABORT, 'the manual results audit is append-only'); END""",
    "trg_manual_result_edits_no_delete": """
        CREATE TRIGGER trg_manual_result_edits_no_delete BEFORE DELETE ON manual_result_edits
        BEGIN SELECT RAISE(ABORT, 'the manual results audit is append-only'); END""",
}


def upgrade() -> None:
    op.create_table(
        "manual_results",
        sa.Column("match_id", sa.String(32), sa.ForeignKey("matches.match_id"), primary_key=True),
        sa.Column("file", sa.String(120), nullable=False),
        *[sa.Column(c, sa.Integer(), nullable=False) for c in ("home_shots", "away_shots", "home_goals", "away_goals")],
        sa.Column("home_sot", sa.Integer()),
        sa.Column("away_sot", sa.Integer()),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("entered_at", sa.DateTime(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("prev_source", sa.String(32), nullable=False),
        sa.Column("prev_status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("home_shots >= 0 AND away_shots >= 0 AND home_goals >= 0 AND away_goals >= 0",
                           name="ck_manual_results_non_negative"),
        sa.CheckConstraint("(home_sot IS NULL OR (home_sot >= 0 AND home_sot <= home_shots)) AND "
                           "(away_sot IS NULL OR (away_sot >= 0 AND away_sot <= away_shots))",
                           name="ck_manual_results_sot"),
    )
    op.create_table(
        "manual_result_edits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("match_id", sa.String(32), sa.ForeignKey("matches.match_id"), nullable=False),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("file", sa.String(120), nullable=False),
        sa.Column("entered_at", sa.DateTime()),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("action IN ('insert', 'update', 'delete')", name="ck_manual_result_edits_action"),
    )
    op.create_index("ix_manual_result_edits_match", "manual_result_edits", ["match_id", "recorded_at"])
    for sql in TRIGGERS.values():
        op.execute(sql)


def downgrade() -> None:
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_index("ix_manual_result_edits_match", table_name="manual_result_edits")
    op.drop_table("manual_result_edits")
    op.drop_table("manual_results")
