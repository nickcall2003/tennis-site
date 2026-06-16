"""
build_surface_records.py
------------------------
OFFLINE generator for per-player surface win/loss records (career + by year),
exported as surface_records.json for the live server to load. Mirrors the
build_ratings.py workflow: run where you have internet, COMMIT the JSON, redeploy.

Source: Jeff Sackmann's ATP + WTA match CSVs (the same data the model trains on).
Every match row carries `surface`, `winner_name`, `loser_name`, and `tourney_date`,
which is all we need. Uses ONLY the Python standard library (urllib + csv) so it
runs anywhere without pandas.

WORKFLOW
  1. python build_surface_records.py                 # 2015..this year, ATP+WTA
     python build_surface_records.py --start 2005    # deeper career history
     python build_surface_records.py --min-matches 10  # smaller file
  2. COMMIT surface_records.json to the repo.
  3. Redeploy. The server loads it via main.py (SURFACE_RECORDS).

Output shape (keyed by normalized player name):
  {
    "carlos alcaraz": {
      "name": "Carlos Alcaraz",
      "surfaces": {
        "Hard":  {"career": [W, L], "by_year": {"2026": [W, L], ...}},
        "Clay":  {...}, "Grass": {...}, "Carpet": {...}
      }
    }, ...
  }
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import unicodedata
import urllib.request
import urllib.error

ATP_URL = "https://cdn.jsdelivr.net/gh/JeffSackmann/tennis_atp@master/atp_matches_{year}.csv"
WTA_URL = "https://cdn.jsdelivr.net/gh/JeffSackmann/tennis_wta@master/wta_matches_{year}.csv"

SURFACES = {"Hard", "Clay", "Grass", "Carpet"}


def norm_name(name: str) -> str:
    """Accent-insensitive, lowercase, whitespace-collapsed key. Must match the
    normalization main.py uses to look players up."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def _mirror_urls(url: str):
    """Given any Sackmann CSV URL, return candidate mirrors in priority order.
    jsDelivr is a CDN mirror of GitHub that datacenter IPs (Railway) reach
    reliably even when raw.githubusercontent.com 404s or blocks; GitHub raw is
    kept as a fallback. Works regardless of which constant the caller passed."""
    fname = url.rsplit("/", 1)[-1]                  # e.g. atp_matches_2024.csv
    repo = "tennis_atp" if fname.startswith("atp") else "tennis_wta"
    return [
        f"https://cdn.jsdelivr.net/gh/JeffSackmann/{repo}@master/{fname}",
        f"https://raw.githubusercontent.com/JeffSackmann/{repo}/refs/heads/master/{fname}",
        f"https://raw.githubusercontent.com/JeffSackmann/{repo}/master/{fname}",
    ]


def _fetch_csv(url: str):
    last = None
    for u in _mirror_urls(url):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "linelogic-surface/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read().decode("utf-8", "replace")
            return list(csv.DictReader(io.StringIO(text)))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:        # this file/mirror missing; try next mirror
                continue
            last = e                 # other HTTP errors: still try remaining mirrors
        except Exception as e:
            last = e
    print(f"[surf]   skip {url.rsplit('/', 1)[-1]}: {last}")
    return []


def _bump(store, name, surface, year, won):
    rec = store.setdefault(norm_name(name), {"name": name, "surfaces": {}})
    surf = rec["surfaces"].setdefault(surface, {"career": [0, 0], "by_year": {}})
    yr = surf["by_year"].setdefault(year, [0, 0])
    idx = 0 if won else 1
    surf["career"][idx] += 1
    yr[idx] += 1


def aggregate(rows, store):
    """Fold a list of Sackmann match dicts into the running record store."""
    added = 0
    for row in rows:
        surface = (row.get("surface") or "").strip().title()
        if surface not in SURFACES:
            continue
        td = (row.get("tourney_date") or "").strip()
        year = td[:4]
        if len(year) != 4 or not year.isdigit():
            continue
        w = (row.get("winner_name") or "").strip()
        l = (row.get("loser_name") or "").strip()
        if not w or not l:
            continue
        _bump(store, w, surface, year, True)
        _bump(store, l, surface, year, False)
        added += 1
    return added


def _career_total(rec):
    return sum(s["career"][0] + s["career"][1] for s in rec["surfaces"].values())


def main():
    ap = argparse.ArgumentParser(description="Build per-player surface W/L records from Sackmann CSVs")
    ap.add_argument("--start", type=int, default=2015, help="first season (default 2015)")
    ap.add_argument("--end", type=int, default=dt.date.today().year, help="last season inclusive")
    ap.add_argument("--out", default="surface_records.json", help="output path")
    ap.add_argument("--min-matches", type=int, default=0,
                    help="drop players with fewer than N total matches (smaller file)")
    ap.add_argument("--no-wta", action="store_true", help="ATP only")
    args = ap.parse_args()
    if args.end < args.start:
        ap.error("--end must be >= --start")

    store: dict = {}
    total = 0
    print(f"[surf] aggregating {args.start}..{args.end} "
          f"({'ATP only' if args.no_wta else 'ATP + WTA'}) from Sackmann CSVs ...")
    for year in range(args.start, args.end + 1):
        urls = [ATP_URL.format(year=year)]
        if not args.no_wta:
            urls.append(WTA_URL.format(year=year))
        for url in urls:
            n = aggregate(_fetch_csv(url), store)
            total += n
        print(f"[surf]   {year}: running total {total:,} matches, {len(store):,} players")

    if args.min_matches > 0:
        before = len(store)
        store = {k: v for k, v in store.items() if _career_total(v) >= args.min_matches}
        print(f"[surf] min-matches {args.min_matches}: kept {len(store):,}/{before:,} players")

    with open(args.out, "w") as f:
        json.dump(store, f, separators=(",", ":"))
    print(f"[surf] trained on {total:,} matches; exported {len(store):,} players -> {args.out}")
    if not store:
        print("[surf] WARNING: 0 players. Check internet access to GitHub.")
    else:
        print("[surf] done. Commit surface_records.json, then redeploy.")


if __name__ == "__main__":
    main()
