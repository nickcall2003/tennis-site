"""
build_surface_records.py
------------------------
OFFLINE generator for per-player surface win/loss records (career + by year),
exported as surface_records.json for the live server to load.

Source: Jeff Sackmann's ATP + WTA match CSVs. Every match row carries `surface`,
`winner_name`, `loser_name`, and `tourney_date`. Standard library only.

WORKFLOW
  1. python build_surface_records.py                 # 2015..this year, ATP+WTA
  2. COMMIT surface_records.json to the repo.
  3. Redeploy (Railway loads it via main.py SURFACE_RECORDS).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import unicodedata
import urllib.request
import urllib.error

# Canonical raw GitHub URLs — confirmed reachable from CI runners. jsDelivr is
# kept only as a last-ditch mirror because it 404s for these files on some hosts.
ATP_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_{year}.csv"
WTA_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_matches_{year}.csv"

SURFACES = {"Hard", "Clay", "Grass", "Carpet"}


def norm_name(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def _mirror_urls(url: str):
    """Candidate URLs in priority order. raw.githubusercontent (both the plain
    and refs/heads form) first because it is the most reliable from CI; jsDelivr
    last as a fallback."""
    fname = url.rsplit("/", 1)[-1]                  # e.g. atp_matches_2024.csv
    repo = "tennis_atp" if fname.startswith("atp") else "tennis_wta"
    return [
        f"https://raw.githubusercontent.com/JeffSackmann/{repo}/master/{fname}",
        f"https://raw.githubusercontent.com/JeffSackmann/{repo}/refs/heads/master/{fname}",
        f"https://cdn.jsdelivr.net/gh/JeffSackmann/{repo}@master/{fname}",
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
            last = f"{u} -> HTTP {e.code}"
            continue
        except Exception as e:
            last = f"{u} -> {e}"
            continue
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
    ap.add_argument("--start", type=int, default=2015)
    ap.add_argument("--end", type=int, default=dt.date.today().year)
    ap.add_argument("--out", default="surface_records.json")
    ap.add_argument("--min-matches", type=int, default=0)
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
            total += aggregate(_fetch_csv(url), store)
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


if __name__ == "__main__":
    main()
