"""Static JSON export for the Stage 1 site (GitHub Pages): meta.json, board.json, results.json,
track.json, exactly as specified in docs/static-api.md (JSON Schema: docs/static-api.schema.json).

    python -m shots.export --out <dir>             write the four files (only those whose content changed)
    python -m shots.export.verify --dir <dir>      independent check of an export against the database

Everything is read through shots.db.repo.Repository with full-access entitlements. Only our own
predictions, fixture identity and final shot counts are published: never odds or raw source rows.
"""

from shots.export.build import SCHEMA_VERSION, ExportError, build_all, league_id
from shots.export.write import FILES, export, render

__all__ = ["SCHEMA_VERSION", "ExportError", "FILES", "build_all", "export", "league_id", "render"]
