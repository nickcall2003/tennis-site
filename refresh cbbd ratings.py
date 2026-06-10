"""
refresh_cbbd_ratings.py
-----------------------
Pulls adjusted efficiency ratings (AdjO / AdjD / AdjNet) from CollegeBasketball
Data (CBBD) and writes ncaab_ratings.json for the NCAAB model. Same pattern as
refresh_cfbd_sp.py: run on a schedule (weekly in season), commit the JSON.

Auth: CBBD uses a Bearer token -> set CBBD_API_KEY.
Season: CBB ends in early April, so by spring the current year's season is the
latest completed one. Override with CBBD_SEASON if needed. The ratings endpoint
path can be overridden with CBBD_RATINGS_URL if CBBD changes it.
"""
from __future__ import annotations
import os
import json
import datetime as dt
import unicodedata

CBBD_KEY = os.environ.get("CBBD_API_KEY", "").strip()
OUT = os.environ.get("NCAAB_RATINGS_PATH", "ncaab_ratings.json")
URL = os.environ.get("CBBD_RATINGS_URL",
                     "https://api.collegebasketballdata.com/ratings/adjusted")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def _season_year() -> int:
    today = dt.date.today()
    # season spans Nov..Apr; label by the ending year. After April the current
    # year's season is complete; before November fall back to the prior year.
    return today.year if today.month >= 4 else today.year - 1


def fetch(season=None):
    import httpx
    season = int(season or os.environ.get("CBBD_SEASON") or _season_year())
    r = httpx.get(URL, params={"season": season},
                  headers={"Authorization": f"Bearer {CBBD_KEY}",
                           "accept": "application/json"},
                  timeout=30.0)
    r.raise_for_status()
    return season, (r.json() or [])


def build(season=None) -> dict:
    if not CBBD_KEY:
        raise SystemExit("Set CBBD_API_KEY to refresh adjusted ratings.")
    season, rows = fetch(season)
    teams = {}
    offs = []
    for row in rows:
        name = row.get("team")
        off = row.get("offensiveRating")
        deff = row.get("defensiveRating")
        net = row.get("netRating")
        if not name or off is None or deff is None:
            continue
        if net is None:
            net = off - deff
        teams[_norm(name)] = {"name": name, "off": off, "def": deff, "net": net}
        offs.append(off)
    avg_eff = round(sum(offs) / len(offs), 2) if offs else 104.0
    data = {"season": season, "updated": dt.datetime.utcnow().isoformat() + "Z",
            "avg_eff": avg_eff, "tempo": 68.0, "teams": teams}
    with open(OUT, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[cbbd] wrote {len(teams)} teams for {season} -> {OUT} (avg_eff={avg_eff})")
    return data


if __name__ == "__main__":
    build()
