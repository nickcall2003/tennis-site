"""
refresh_cfbd_sp.py
------------------
Pulls SP+ team ratings from CollegeFootballData (CFBD) and writes a small
file-backed stat file (ncaaf_sp.json) that the NCAAF model reads. Same shape
as refresh_nhl_stats.py / the NCAABB stat refreshers: run it on a schedule
(weekly in season is plenty -- SP+ updates after each week), commit the JSON.

Auth: CFBD uses a Bearer token. Set CFBD_API_KEY (CFBD_KEY also accepted).
Season: Aug-Jan. Before a new season's SP+ is published (e.g. 2026 preseason
in early August), CFBD returns an EMPTY list for that year -- so we automatically
fall back to the most recent season that actually has data, and self-heal to the
new season the moment CFBD posts it.
"""
from __future__ import annotations
import os
import json
import datetime as dt
import unicodedata

# Accept either name so a mismatch in the deploy env can't silently break auth.
CFBD_KEY = (os.environ.get("CFBD_API_KEY") or os.environ.get("CFBD_KEY") or "").strip()
OUT = os.environ.get("NCAAF_SP_PATH", "ncaaf_sp.json")
URL = "https://api.collegefootballdata.com/ratings/sp"
# How many seasons back to look if the requested one has no ratings yet.
FALLBACK_YEARS = int(os.environ.get("CFBD_FALLBACK_YEARS", "3"))


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def _season_year() -> int:
    today = dt.date.today()
    return today.year if today.month >= 8 else today.year - 1


def fetch(year=None):
    """Fetch SP+ rows for a season. Returns (year, rows). Raises on an HTTP error
    (a 401/403 here means the key is missing/invalid or its tier lacks SP+)."""
    import httpx
    year = int(year or os.environ.get("CFBD_YEAR") or _season_year())
    r = httpx.get(URL, params={"year": year},
                  headers={"Authorization": f"Bearer {CFBD_KEY}",
                           "accept": "application/json"},
                  timeout=30.0)
    r.raise_for_status()
    return year, (r.json() or [])


def _parse(rows):
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
    return teams, national


def build(year=None) -> dict:
    """Build ncaaf_sp.json from CFBD SP+ ratings. If the target season has no
    ratings yet, fall back to the most recent season that DOES -- so the refresh
    never returns empty just because the new season isn't posted. Self-heals:
    once CFBD publishes the new season, the next run uses it automatically."""
    if not CFBD_KEY:
        raise SystemExit("Set CFBD_API_KEY (or CFBD_KEY) to refresh SP+ ratings.")
    start = int(year or os.environ.get("CFBD_YEAR") or _season_year())

    tried, rows, used = [], [], start
    for cand in range(start, start - max(1, FALLBACK_YEARS) - 1, -1):
        try:
            used, rows = fetch(cand)
        except Exception as e:
            # HTTP-level failure (auth/tier/network) -- distinct from "empty data".
            raise SystemExit(
                f"CFBD request failed for {cand}: {type(e).__name__}: {e}. "
                f"The key is set but CFBD rejected the request -- verify the key is "
                f"valid and its tier includes SP+ ratings.")
        tried.append((cand, len(rows)))
        if rows:
            break

    if not rows:
        raise SystemExit(
            f"CFBD returned no SP+ rows for any season in {tried}. If the newest "
            f"season simply isn't published yet, this self-resolves once CFBD posts "
            f"it; otherwise verify the key's tier includes SP+.")

    teams, national = _parse(rows)
    requested = start
    data = {"year": used, "requested_year": requested, "fallback": used != requested,
            "updated": dt.datetime.utcnow().isoformat() + "Z",
            "national": national, "teams": teams}
    with open(OUT, "w") as f:
        json.dump(data, f, indent=2)
    note = "" if used == requested else f" (fell back from {requested} -- not published yet)"
    print(f"[cfbd] wrote {len(teams)} teams for {used}{note} -> {OUT} "
          f"(national off={national['off']}, def={national['def']})")
    return data


if __name__ == "__main__":
    build()
