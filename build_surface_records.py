"""
build_surface_records.py — per-player surface W/L records -> surface_records.json
Reads Jeff Sackmann ATP + WTA match CSVs, either from local cloned repos
(--from-dir, the reliable path in CI) or over HTTP. Standard library only.
"""
from __future__ import annotations
import argparse, csv, datetime as dt, glob, io, json, os, unicodedata
import urllib.request, urllib.error

ATP_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_{year}.csv"
WTA_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_matches_{year}.csv"
SURFACES = {"Hard", "Clay", "Grass", "Carpet"}


def norm_name(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def _fetch_csv(url: str):
    fname = url.rsplit("/", 1)[-1]
    repo = "tennis_atp" if fname.startswith("atp") else "tennis_wta"
    mirrors = [
        f"https://raw.githubusercontent.com/JeffSackmann/{repo}/master/{fname}",
        f"https://cdn.jsdelivr.net/gh/JeffSackmann/{repo}@master/{fname}",
    ]
    last = None
    for u in mirrors:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "linelogic-surface/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read().decode("utf-8", "replace")
            return list(csv.DictReader(io.StringIO(text)))
        except Exception as e:
            last = f"{u} -> {e}"
    print(f"[surf]   skip {fname}: {last}")
    return []


def _read_local_csv(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return list(csv.DictReader(f))
    except Exception as e:
        print(f"[surf]   skip {os.path.basename(path)}: {e}")
        return []


def _bump(store, name, surface, year, won):
    rec = store.setdefault(norm_name(name), {"name": name, "surfaces": {}})
    surf = rec["surfaces"].setdefault(surface, {"career": [0, 0], "by_year": {}})
    yr = surf["by_year"].setdefault(year, [0, 0])
    surf["career"][0 if won else 1] += 1
    yr[0 if won else 1] += 1


def aggregate(rows, store):
    added = 0
    for row in rows:
        surface = (row.get("surface") or "").strip().title()
        if surface not in SURFACES:
            continue
        year = (row.get("tourney_date") or "").strip()[:4]
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


def aggregate_from_dirs(dirs, start, end, no_wta, store):
    total = 0
    prefixes = ["atp_matches_"] + ([] if no_wta else ["wta_matches_"])
    for d in dirs:
        for pre in prefixes:
            for path in sorted(glob.glob(os.path.join(d, pre + "*.csv"))):
                base = os.path.basename(path)
                yr = base.replace(pre, "").replace(".csv", "")
                if not (yr.isdigit() and start <= int(yr) <= end):
                    continue
                n = aggregate(_read_local_csv(path), store)
                total += n
                print(f"[surf]   {base}: +{n:,} matches (total {total:,}, {len(store):,} players)")
    return total


def _career_total(rec):
    return sum(s["career"][0] + s["career"][1] for s in rec["surfaces"].values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=2015)
    ap.add_argument("--end", type=int, default=dt.date.today().year)
    ap.add_argument("--out", default="surface_records.json")
    ap.add_argument("--min-matches", type=int, default=0)
    ap.add_argument("--no-wta", action="store_true")
    ap.add_argument("--from-dir", action="append", default=[], metavar="DIR",
                    help="read CSVs from local cloned repos (repeatable)")
    args = ap.parse_args()
    if args.end < args.start:
        ap.error("--end must be >= --start")

    store, total = {}, 0
    if args.from_dir:
        print(f"[surf] aggregating {args.start}..{args.end} from local dirs: {', '.join(args.from_dir)}")
        total = aggregate_from_dirs(args.from_dir, args.start, args.end, args.no_wta, store)
    else:
        print(f"[surf] aggregating {args.start}..{args.end} "
              f"({'ATP only' if args.no_wta else 'ATP + WTA'}) over HTTP ...")
        for year in range(args.start, args.end + 1):
            urls = [ATP_URL.format(year=year)] + ([] if args.no_wta else [WTA_URL.format(year=year)])
            for url in urls:
                total += aggregate(_fetch_csv(url), store)
            print(f"[surf]   {year}: running total {total:,} matches, {len(store):,} players")

    if args.min_matches > 0:
        store = {k: v for k, v in store.items() if _career_total(v) >= args.min_matches}

    with open(args.out, "w") as f:
        json.dump(store, f, separators=(",", ":"))
    print(f"[surf] trained on {total:,} matches; exported {len(store):,} players -> {args.out}")
    if not store:
        print("[surf] WARNING: 0 players.")


if __name__ == "__main__":
    main()
