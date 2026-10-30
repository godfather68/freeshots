"""Model milestones for meta.model_history (docs/static-api.md, "Model history").

The database records which model scored each prediction, but not when a model was adopted on a route or when a
display rule changed, so the milestones live in a small committed file next to the exporter
(`model_history.json`), versioned with the code that switched the model (src/shots/config.py). The export
publishes the entries dated on or before the export day: deterministic for the same code and day.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

HISTORY_PATH = Path(__file__).with_name("model_history.json")
ROUTES = ("all", "odds", "noodds", "cross_border")
FIELDS = ("date", "route", "model", "label", "detail")


def load_history(path: Path | None = None) -> list[dict]:
    """All committed milestones, validated and sorted by (date, route, model). Raises ValueError when malformed."""
    doc = json.loads(Path(path or HISTORY_PATH).read_text(encoding="utf-8"))
    rows = doc.get("milestones") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise ValueError("model_history.json: 'milestones' must be a list")
    out = []
    for i, m in enumerate(rows):
        if not isinstance(m, dict) or set(m) != set(FIELDS):
            raise ValueError(f"model_history.json: milestone {i} must have exactly the fields {FIELDS}")
        try:
            date.fromisoformat(m["date"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"model_history.json: milestone {i} has a bad date {m.get('date')!r}") from exc
        if m["route"] not in ROUTES:
            raise ValueError(f"model_history.json: milestone {i} route {m['route']!r} not in {ROUTES}")
        for k in ("model", "label", "detail"):
            if not isinstance(m[k], str) or not m[k].strip():
                raise ValueError(f"model_history.json: milestone {i} {k} must be a non-empty string")
        out.append({k: m[k] for k in FIELDS})
    return sorted(out, key=lambda m: (m["date"], m["route"], m["model"]))


def model_history(until: date, path: Path | None = None) -> list[dict]:
    """The milestones dated on or before `until` (the export day), oldest first."""
    return [m for m in load_history(path) if m["date"] <= until.isoformat()]
