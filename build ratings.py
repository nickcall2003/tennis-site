"""
build_ratings.py
----------------
One-off / scheduled OFFLINE trainer for the tennis model.

Pulls Jeff Sackmann's ATP + WTA tour-and-challenger match CSVs, trains the
surface-aware Elo, and exports a small ratings.json that the live server loads
via PredictionEngine.load_ratings() -- so the server never has to touch pandas
or the CSVs at runtime (keeps it lean on a small instance).

WORKFLOW
  1. Run this where you have internet + pandas (your laptop, or a Railway
     one-off shell):   pip install pandas  &&  python build_ratings.py
  2. COMMIT the generated ratings.json to the repo.
  3. Make sure RATINGS_FILE is unset or = "ratings.json" (the default) and
     redeploy. The logs should switch from
        "no ratings.json found; running lean (ranking-only)"
     to
        "loaded N precomputed ratings (low-memory mode)".

Re-run periodically (weekly/monthly) to keep ratings current and re-commit.

Usage:
    python build_ratings.py                  # 2015..this year, tour + challenger
    python build_ratings.py --start 2008     # deeper history (longer careers)
    python build_ratings.py --no-challengers # tour level only (smaller file)
    python build_ratings.py --out ratings.json
"""
from __future__ import annotations

import argparse
import datetime as dt

from predictions import PredictionEngine


def main():
    ap = argparse.ArgumentParser(
        description="Train tennis surface-Elo from Sackmann CSVs and export ratings.json")
    ap.add_argument("--start", type=int, default=2015,
                    help="first season year to train on (default 2015)")
    ap.add_argument("--end", type=int, default=dt.date.today().year,
                    help="last season year, inclusive (default: this year)")
    ap.add_argument("--out", default="ratings.json", help="output JSON path")
    ap.add_argument("--no-challengers", action="store_true",
                    help="tour level only (fewer players, smaller file)")
    args = ap.parse_args()

    if args.end < args.start:
        ap.error("--end must be >= --start")

    engine = PredictionEngine()
    years = range(args.start, args.end + 1)
    scope = "tour + challenger" if not args.no_challengers else "tour only"
    print(f"[build] training surface-Elo on {args.start}..{args.end} ({scope}) ...")
    print("[build] pulling Sackmann CSVs from GitHub (this can take a minute) ...")

    n = engine.train_from_sackmann(years, include_challengers=not args.no_challengers)
    count = engine.export_ratings(args.out)

    print(f"[build] trained on {n:,} matches; exported {count:,} players -> {args.out}")
    if not count:
        print("[build] WARNING: 0 players exported. Check internet access to GitHub "
              "and that pandas is installed (pip install pandas).")
    else:
        print("[build] done. Commit ratings.json, then redeploy so the engine loads it.")


if __name__ == "__main__":
    main()
