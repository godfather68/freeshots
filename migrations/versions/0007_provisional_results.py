"""Provisional results for football-data competitions (hand-entered until football-data publishes).

football-data.co.uk can stop publishing for days; the predicted domestic matches then cannot settle. A human
may enter their final counts from a public match page in a committed CSV (results_provisional/<league id>_
<season>.csv, shots.ingest.provisional_results); the update loads it with shots.db.provisional. As with manual
results (0006) the values go to the `matches` row (status 'played', source 'provisional'), so outcomes stay
derived views and the predictions settle, the official record included. When football-data publishes the
match, the regular results load overwrites the row (source 'football-data'): the result is then 'confirmed'
and football-data's numbers count, with no step of ours in between.

- provisional_results: one row per provisionally entered match: the values as entered, the provider URL and
  its label (`provider`, e.g. 'espn'), `entered_at`, notes, the file, and the match's source / status before
  it was settled (restored if the CSV row is removed while the result is still provisional). Corrections
  update it. It is kept after football-data confirms the match: it is the provider's side of the agreement
  audit.
- provisional_result_edits: append-only audit (no UPDATE, no DELETE: triggers). action insert / update /
  delete (the CSV changed; changes = {column: [old, new]} of the changed columns) or confirm (written by a
  trigger at the moment football-data replaces a provisional result; changes = {column: [provisional,
  football-data]} of every result column, equal or not).
- v_provisional_agreement: per provisional row, its status (provisional | confirmed | unsettled), the
  provider's values, football-data's (once confirmed) and the differences (football-data minus provider).

Guards (triggers, so they hold for any client):
- matches: source 'provisional' only on a competition WITH a football-data division code; never on insert
  (it only settles a fixture we already have); a played football-data match never becomes provisional.
- provisional_results: only for a match of a competition with a division code.
- 0006's manual-results triggers are unchanged: 'manual' stays reserved for competitions without a code.

No change to existing views.

Revision ID: 0007_provisional_results
Revises: 0006_manual_results
Create Date: 2026-10-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_provisional_results"
down_revision = "0006_manual_results"
branch_labels = None
depends_on = None

_NO_CODE = "(SELECT code FROM competitions WHERE id = NEW.competition_id) IS NULL"
_VALUES = ("home_shots", "away_shots", "home_sot", "away_sot", "home_goals", "away_goals")
_PAIRS = ", ".join(f"'{c}', json_array(OLD.{c}, NEW.{c})" for c in _VALUES)
_MSG = "provisional results are only allowed for unconfirmed matches of football-data competitions"

TRIGGERS = {
    "trg_matches_provisional_insert": """
        CREATE TRIGGER trg_matches_provisional_insert BEFORE INSERT ON matches
        WHEN NEW.source = 'provisional'
        BEGIN SELECT RAISE(ABORT, 'a provisional result only settles an existing fixture'); END""",
    "trg_matches_provisional_scope_update": f"""
        CREATE TRIGGER trg_matches_provisional_scope_update BEFORE UPDATE OF source, competition_id ON matches
        WHEN NEW.source = 'provisional' AND ({_NO_CODE} OR (OLD.source = 'football-data' AND OLD.status = 'played'))
        BEGIN SELECT RAISE(ABORT, '{_MSG}'); END""",
    "trg_provisional_results_scope": f"""
        CREATE TRIGGER trg_provisional_results_scope BEFORE INSERT ON provisional_results
        WHEN COALESCE((SELECT c.code IS NULL FROM matches m JOIN competitions c ON c.id = m.competition_id
                       WHERE m.match_id = NEW.match_id), 1)
        BEGIN SELECT RAISE(ABORT, '{_MSG}'); END""",
    "trg_matches_provisional_confirmed": f"""
        CREATE TRIGGER trg_matches_provisional_confirmed AFTER UPDATE OF source ON matches
        WHEN OLD.source = 'provisional' AND NEW.source = 'football-data'
        BEGIN
            INSERT INTO provisional_result_edits (match_id, action, changes, file, entered_at, recorded_at)
            VALUES (NEW.match_id, 'confirm', json_object({_PAIRS}),
                    COALESCE((SELECT file FROM provisional_results WHERE match_id = NEW.match_id), ''),
                    NULL, datetime('now'));
        END""",
    "trg_provisional_result_edits_no_update": """
        CREATE TRIGGER trg_provisional_result_edits_no_update BEFORE UPDATE ON provisional_result_edits
        BEGIN SELECT RAISE(ABORT, 'the provisional results audit is append-only'); END""",
    "trg_provisional_result_edits_no_delete": """
        CREATE TRIGGER trg_provisional_result_edits_no_delete BEFORE DELETE ON provisional_result_edits
        BEGIN SELECT RAISE(ABORT, 'the provisional results audit is append-only'); END""",
}

_CONFIRMED = "(m.source = 'football-data' AND m.status = 'played')"
VIEW = f"""
    SELECT pr.match_id, c.name AS competition, m.date, m.home_team, m.away_team, pr.provider, pr.source_url,
           pr.entered_at, pr.file,
           CASE WHEN m.source = 'provisional' THEN 'provisional' WHEN {_CONFIRMED} THEN 'confirmed'
                ELSE 'unsettled' END AS result_status,
           pr.home_shots AS prov_home_shots, pr.away_shots AS prov_away_shots,
           pr.home_shots + pr.away_shots AS prov_total, pr.home_sot AS prov_home_sot, pr.away_sot AS prov_away_sot,
           pr.home_goals AS prov_home_goals, pr.away_goals AS prov_away_goals,
           CASE WHEN {_CONFIRMED} THEN m.home_shots END AS fd_home_shots,
           CASE WHEN {_CONFIRMED} THEN m.away_shots END AS fd_away_shots,
           CASE WHEN {_CONFIRMED} THEN m.home_shots + m.away_shots END AS fd_total,
           CASE WHEN {_CONFIRMED} THEN m.home_sot END AS fd_home_sot,
           CASE WHEN {_CONFIRMED} THEN m.away_sot END AS fd_away_sot,
           CASE WHEN {_CONFIRMED} THEN m.home_goals END AS fd_home_goals,
           CASE WHEN {_CONFIRMED} THEN m.away_goals END AS fd_away_goals,
           CASE WHEN {_CONFIRMED} THEN m.home_shots - pr.home_shots END AS diff_home_shots,
           CASE WHEN {_CONFIRMED} THEN m.away_shots - pr.away_shots END AS diff_away_shots,
           CASE WHEN {_CONFIRMED} THEN (m.home_shots + m.away_shots) - (pr.home_shots + pr.away_shots) END AS diff_total,
           CASE WHEN {_CONFIRMED} THEN m.home_goals = pr.home_goals AND m.away_goals = pr.away_goals END AS goals_agree
    FROM provisional_results pr
    JOIN matches m ON m.match_id = pr.match_id
    JOIN competitions c ON c.id = m.competition_id"""


def upgrade() -> None:
    op.create_table(
        "provisional_results",
        sa.Column("match_id", sa.String(32), sa.ForeignKey("matches.match_id"), primary_key=True),
        sa.Column("file", sa.String(120), nullable=False),
        *[sa.Column(c, sa.Integer(), nullable=False) for c in ("home_shots", "away_shots", "home_goals", "away_goals")],
        sa.Column("home_sot", sa.Integer()),
        sa.Column("away_sot", sa.Integer()),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("entered_at", sa.DateTime(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("prev_source", sa.String(32), nullable=False),
        sa.Column("prev_status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("home_shots >= 0 AND away_shots >= 0 AND home_goals >= 0 AND away_goals >= 0",
                           name="ck_provisional_results_non_negative"),
        sa.CheckConstraint("(home_sot IS NULL OR (home_sot >= 0 AND home_sot <= home_shots)) AND "
                           "(away_sot IS NULL OR (away_sot >= 0 AND away_sot <= away_shots))",
                           name="ck_provisional_results_sot"),
    )
    op.create_table(
        "provisional_result_edits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("match_id", sa.String(32), sa.ForeignKey("matches.match_id"), nullable=False),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("file", sa.String(120), nullable=False),
        sa.Column("entered_at", sa.DateTime()),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("action IN ('insert', 'update', 'delete', 'confirm')",
                           name="ck_provisional_result_edits_action"),
    )
    op.create_index("ix_provisional_result_edits_match", "provisional_result_edits", ["match_id", "recorded_at"])
    for sql in TRIGGERS.values():
        op.execute(sql)
    op.execute(f"CREATE VIEW v_provisional_agreement AS {VIEW}")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_provisional_agreement")
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    # matches still settled provisionally go back to what they were (0006 does not know the source)
    nulls = ", ".join(f"{c} = NULL" for c in _VALUES)
    op.execute(f"""
        UPDATE matches SET {nulls},
            source = (SELECT prev_source FROM provisional_results pr WHERE pr.match_id = matches.match_id),
            status = (SELECT prev_status FROM provisional_results pr WHERE pr.match_id = matches.match_id)
        WHERE source = 'provisional' AND match_id IN (SELECT match_id FROM provisional_results)""")
    op.drop_index("ix_provisional_result_edits_match", table_name="provisional_result_edits")
    op.drop_table("provisional_result_edits")
    op.drop_table("provisional_results")
