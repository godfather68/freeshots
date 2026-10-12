"""Data-freshness summary for the header: 'Updated 2 h ago · results to 5 Oct', with a warning level."""

from __future__ import annotations

from datetime import date, datetime

STALE_AFTER_H = 48


def ago(delta_s: float) -> str:
    s = max(delta_s, 0)
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{round(s / 60)} min ago"
    if s < 48 * 3600:
        return f"{round(s / 3600)} h ago"
    return f"{int(s // 86400)} days ago"


def short_date(d: date | str | None) -> str | None:
    if d is None or d == "":
        return None
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d[:10])
        except ValueError:
            return None
    if isinstance(d, datetime):
        d = d.date()
    return f"{d.day} {d:%b}"


def freshness(meta: dict | None, status: dict | None, now: datetime) -> dict | None:
    """{text, level ('ok'|'stale'|'failed'), detail}. None when there is no board to describe.
    `now` is local wall-clock time (board built_at is stored in local time)."""
    meta = meta or {}
    built = None
    if meta.get("built_at"):
        try:
            built = datetime.fromisoformat(meta["built_at"])
        except ValueError:
            built = None
    if built is None:
        return None
    if built.tzinfo is not None:
        built = built.astimezone().replace(tzinfo=None)
    age_s = (now - built).total_seconds()
    results = short_date((status or {}).get("latest_result") or meta.get("results_up_to"))
    text = f"Updated {ago(age_s)}" + (f" · results to {results}" if results else "")
    level, detail = "ok", ""
    job = (status or {}).get("last_job") or {}
    if job.get("status") in ("failed", "partial"):
        level = "failed"
        word = "failed" if job["status"] == "failed" else "only partly worked"
        detail = f"The last automatic update {word}, so these numbers may be out of date."
    elif age_s >= STALE_AFTER_H * 3600:
        level = "stale"
        detail = "These predictions are more than two days old. Matches may have been played or odds may have appeared since."
    return {"text": text, "level": level, "detail": detail}
