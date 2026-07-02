"""
thesportsdb.py — TheSportsDB client for team badges, jerseys, stadiums, images.

Enrichment only (logos/photos), so a miss is harmless. Key from THESPORTSDB_KEY
(the public "123" test key is the default). Results cached hard since badges and
stadiums almost never change.
"""
import os
import time

_KEY = os.environ.get("THESPORTSDB_KEY", "123")
BASE = "https://www.thesportsdb.com/api/v1/json"

_cache = {}
_TTL = 7 * 24 * 3600
_neg = set()                 # names we've already failed to find (skip refetch)


def _get(path, params):
    url = f"{BASE}/{_KEY}/{path}"
    ck = url + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    try:
        import httpx
        r = httpx.get(url, params=params, timeout=12.0)
        r.raise_for_status()
        j = r.json()
    except Exception:
        return hit[1] if hit else None
    _cache[ck] = (time.time(), j)
    return j


def team_media(name):
    """Badge, jersey, stadium and description for a team name. None on miss."""
    if not name or name in _neg:
        return None
    j = _get("searchteams.php", {"t": name})
    teams = (j or {}).get("teams") or []
    if not teams:
        _neg.add(name)
        return None
    t = teams[0]
    out = {
        "source": "thesportsdb",
        "team": t.get("strTeam"),
        "badge": t.get("strBadge") or t.get("strTeamBadge"),
        "logo": t.get("strLogo") or t.get("strTeamLogo"),
        "jersey": t.get("strEquipment") or t.get("strTeamJersey"),
        "stadium": t.get("strStadium"),
        "stadium_thumb": t.get("strStadiumThumb"),
        "stadium_capacity": t.get("intStadiumCapacity"),
        "formed": t.get("intFormedYear"),
    }
    return {k: v for k, v in out.items() if v}


def badge(name):
    """Just the badge URL for a team, or None."""
    m = team_media(name)
    return m.get("badge") if m else None
