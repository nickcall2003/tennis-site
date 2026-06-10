"""
refresh_cfbd_sp.py
------------------
Pulls SP+ team ratings from CollegeFootballData (CFBD) and writes a small
file-backed stat file (ncaaf_sp.json) that the NCAAF model reads. Same shape
as refresh_nhl_stats.py / the NCAABB stat refreshers: run it on a schedule
(weekly in season is plenty -- SP+ updates after each week), commit the JSON.

Auth: CFBD uses a Bearer token. Set CFBD_API_KEY in the environment.
Season: Aug-Jan, so before August we use the previous (completed) season.
"""
from __future__ import annotations
import os
import json
import datetime as dt
import unicodedata

CFBD_KEY = os.environ.get("CFBD_API_KEY", "").strip()
OUT = os.environ.get("NCAAF_SP_PATH", "ncaaf_sp.json")
URL = "https://api.collegefootballdata.com/ratings/sp"


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def _season_year() -> int:
    today = dt.date.today()
    return today.year if today.month >= 8 else today.year - 1


def fetch(year=None):
    import httpx
    year = int(year or os.environ.get("CFBD_YEAR") or _season_year())
    r = httpx.get(URL, params={"year": year},
                  headers={"Authorization": f"Bearer {CFBD_KEY}",
                           "accept": "application/json"},
                  timeout=30.0)
    r.raise_for_status()
    return year, (r.json() or [])


def build(year=None) -> dict:
    if not CFBD_KEY:
        raise SystemExit("Set CFBD_API_KEY to refresh SP+ ratings.")
    year, rows = fetch(year)
    teams = {}
    national = {"off": None, "def": None}
    for row in rows:
        name = row.get("team")
        off = (row.get("offense") or {}).get("rating")
        deff = (row.get("defense") or {}).get("rating")
        if name == "nationalAverages":
            national = {"off": off, "def": deff}
            continue
        if not name or row.get("rating") is None:
            continue
        teams[_norm(name)] = {"name": name, "sp": row.get("rating"),
                              "off": off, "def": deff}
    data = {"year": year, "updated": dt.datetime.utcnow().isoformat() + "Z",
            "national": national, "teams": teams}
    with open(OUT, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[cfbd] wrote {len(teams)} teams for {year} -> {OUT} "
          f"(national off={national['off']}, def={national['def']})")
    return data


if __name__ == "__main__":
    build()
