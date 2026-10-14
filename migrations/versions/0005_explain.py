"""Explanation stored with each board prediction ("why" the expected total is what it is).

predictions.explain (JSON, NULL): the additive decomposition of the expected total computed by
shots.models.explain from the SAME fitted model and the SAME feature row as the prediction, at the
moment the board is built. It is stored with the prediction, not recomputed at export, because the
exporter has neither models nor features, and because a later recomputation would use newer results
and possibly a refitted model, i.e. it would explain a different prediction than the stored one.

- NULL for every existing row and for picks / backfill rows (only board runs carry explanations).
- Set once at insert by the board loader, never updated: the append-only UPDATE trigger covers it.
- No view selects it; outcomes, the official record and every metric are unchanged.
- It contains no odds values: market factors are worded as categories (shots.models.explain).

Added with ALTER TABLE ADD COLUMN (no table copy): triggers and views of 0001-0004 stay in place and
no existing prediction row is rewritten.

Revision ID: 0005_explain
Revises: 0004_experimental
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_explain"
down_revision = "0004_experimental"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("predictions", sa.Column("explain", sa.JSON(), nullable=True))


def downgrade() -> None:
    # SQLite >= 3.35 drops a column in place (no table copy; no view or trigger refers to it).
    op.execute("ALTER TABLE predictions DROP COLUMN explain")
