"""python -m shots.export --out <dir> [--meta changed|always] [--today YYYY-MM-DD]

Writes meta.json, board.json, results.json and track.json (docs/static-api.md) from the database
(data/shots.db; SHOTS_DB overrides). Files whose content did not change are left untouched;
meta.json (the only file with timestamps) is rewritten only when something changed, unless
--meta always. Exit code 2 when there is nothing to export.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date

from shots.export.build import ExportError
from shots.export.write import export


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m shots.export", description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, help="output directory (created if missing)")
    ap.add_argument("--meta", choices=("changed", "always"), default="changed",
                    help="rewrite meta.json only when something changed (default) or on every run")
    ap.add_argument("--today", type=date.fromisoformat, default=None,
                    help="first day of the board (default: today's UTC date)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        print(json.dumps(export(args.out, meta=args.meta, today=args.today), indent=2))
    except ExportError as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
