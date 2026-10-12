"""Deterministic serialisation and change-aware writing of the export files.

Text form: UTF-8, LF line endings, keys in contract order, one fixture / result per line (so the git
history of the published data shows one changed line per changed prediction). A file is rewritten
only when its bytes change. meta.json carries the only timestamps (built_at, last_job.finished_at):
with meta="changed" it is rewritten only when another file changed or its own non-volatile content
changed, so an unchanged database produces no diff at all.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

from shots.db.repo import Repository
from shots.export.build import ExportError, build_all

log = logging.getLogger("shots.export")

FILES = ("board", "results", "track", "meta")  # meta last: it is decided by the others
ROW_LISTS = {"board": "fixtures", "results": "results"}  # top-level lists written one item per line


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def render(name: str, doc: dict) -> str:
    """The exact file text of one document."""
    key = ROW_LISTS.get(name)
    if key is None:
        return json.dumps(doc, ensure_ascii=False, indent=1, allow_nan=False) + "\n"
    head = {k: v for k, v in doc.items() if k != key}
    lines = ["{"] + [f" {_dumps(k)}: {_dumps(v)}," for k, v in head.items()]
    items = doc[key]
    if not items:
        lines.append(f" {_dumps(key)}: []")
    else:
        lines.append(f" {_dumps(key)}: [")
        lines += [f"  {_dumps(item)}{',' if i < len(items) - 1 else ''}" for i, item in enumerate(items)]
        lines.append(" ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def stable_meta(meta: dict) -> dict:
    """meta without its volatile fields (export time, job end time)."""
    out = copy.deepcopy(meta)
    out.pop("built_at", None)
    if isinstance(out.get("last_job"), dict):
        out["last_job"].pop("finished_at", None)
    return out


def _write_if_changed(path: Path, text: str) -> bool:
    data = text.encode("utf-8")
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)  # bytes: no CRLF translation on Windows
    os.replace(tmp, path)  # atomic: a reader never sees a half-written file
    return True


def export(out_dir: str | Path, db=None, meta: str = "changed", built_at: datetime | None = None,
           today: date | None = None) -> dict:
    """Write meta.json, board.json, results.json and track.json into out_dir.
    meta: "changed" (default) rewrites meta.json only when something else changed; "always" refreshes
    its built_at on every run. -> {"dir", "written": [names], "unchanged": [names], "fixtures", "results"}.
    Raises ExportError (and writes nothing) when the database has no board or holds an invalid curve."""
    if meta not in ("changed", "always"):
        raise ValueError("meta must be 'changed' or 'always'")
    repo = db if isinstance(db, Repository) else Repository(db)
    owns = not isinstance(db, Repository)
    try:
        docs = build_all(repo, built_at, today)
    finally:
        if owns:
            repo.engine.dispose()
    texts = {name: render(name, docs[name]) for name in FILES}  # everything rendered before any write
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = [name for name in FILES[:-1] if _write_if_changed(out / f"{name}.json", texts[name])]
    meta_path = out / "meta.json"
    rewrite = meta == "always" or bool(written)
    if not rewrite:
        try:
            rewrite = stable_meta(json.loads(meta_path.read_text(encoding="utf-8"))) != stable_meta(docs["meta"])
        except (OSError, ValueError):
            rewrite = True
    if rewrite and _write_if_changed(meta_path, texts["meta"]):
        written.append("meta")
    summary = {"dir": str(out), "written": written, "unchanged": [n for n in FILES if n not in written],
               "fixtures": len(docs["board"]["fixtures"]), "results": len(docs["results"]["results"])}
    log.info("export -> %s: wrote %s, unchanged %s", out, written or "nothing", summary["unchanged"] or "nothing")
    return summary


__all__ = ["FILES", "ExportError", "export", "render", "stable_meta"]
