"""
balldontlie.py — universal multi-sport client (api.balldontlie.io).

One key, many sports. This is a generic, extensible client: a per-sport path
prefix map + a cached getter + teams/standings helpers and an ESPN-name matcher.
Per-sport advanced-stat endpoints (season averages, advanced stats) can be added
as we confirm each sport's exact response shape.

Key: set BALLDONTLIE_KEY in the environment. Auth is the Authorization header.
"""
import os
import time

BASE = "https://api.balldontlie.io"
_KEY = os.environ.get("BALLDONTLIE_KEY", "")

# our sport key -> balldontlie path prefix
PREFIX = {
    "nba": "nba/v1", "nfl": "nfl/v1", "mlb": "mlb/v1", "nhl": "nhl/v1",
    "ncaab": "ncaab/v1", "ncaaf": "ncaaf/v1", "wncaab": "ncaaw/v1", "wnba": "wnba/v1",
    # soccer leagues (our soccer 'league' key -> prefix)
    "epl": "epl/v2", "laliga": "laliga/v1", "seriea": "seriea/v1",
    "bundesliga": "bundesliga/v1", "ligue1": "ligue1/v1", "mls": "mls/v1", "ucl": "ucl/v1",
}

_cache = {}
_TTL = 6 * 3600


def available():
    return bool(_KEY)


def _get(prefix, resource, params=None):
    if not _KEY or not prefix:
        return None
    url = f"{BASE}/{prefix}/{resource}"
    ck = url + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    try:
        import httpx
        r = httpx.get(url, params=params or {},
                      headers={"Authorization": _KEY}, timeout=15.0)
        r.raise_for_status()
        j = r.json()
    except Exception:
        return hit[1] if hit else None
    _cache[ck] = (time.time(), j)
    return j


def get(sport, resource, params=None):
    """Generic getter keyed by our sport name (or soccer league key)."""
    return _get(PREFIX.get(sport), resource, params)


def teams(sport):
    j = get(sport, "teams")
    return (j or {}).get("data") or []


def _teams_index(sport):
    import name_match
    rows = teams(sport)
    # balldontlie team objects usually carry name / full_name / abbreviation + id
    items = [{"name": (t.get("full_name") or t.get("name") or t.get("abbreviation")),
              "id": t.get("id")} for t in rows if isinstance(t, dict)]
    return name_match.build_index(items)


def resolve_team_id(sport, espn_name):
    import name_match
    return name_match.match(espn_name, _teams_index(sport))


def standings(sport, season=None):
    """Raw standings payload for a sport (shape varies by sport)."""
    p = {"season": season} if season else {}
    return get(sport, "standings", p)
