"""
highlightly.py — Highlightly API (highlightly.net) for NCAA college baseball
team batting/pitching stats. OPTIONAL enrichment, key-ready.

Why this exists: ESPN's free college feed gives records but NOT team ERA/OBP/
SLG. Highlightly's NCAA baseball coverage does, which lets college baseball use
the SAME run-expectancy engine as MLB (expected runs from offense vs opposing
pitching) instead of a strength-only Elo. When no key is set, everything falls
back to the existing ESPN+RPI strength model — nothing breaks.

Auth: set HIGHLIGHTLY_API_KEY in the environment. The key is sent as the
x-rapidapi-key header (works for both the RapidAPI host and Highlightly's own).
Host is configurable via HIGHLIGHTLY_HOST (defaults to the RapidAPI baseball host).

Free tier is ~100 requests/day, so we cache team stats hard (12h) and read the
x-ratelimit-requests-remaining header to pause when low.
"""
from __future__ import annotations

import os
import time
import datetime as dt

API_KEY = os.environ.get("HIGHLIGHTLY_API_KEY", "").strip()
HOST = os.environ.get("HIGHLIGHTLY_HOST", "baseball-highlights-api.p.rapidapi.com").strip()
BASE = f"https://{HOST}"

_team_cache = {}        # team_name_norm -> (ts, stats dict)
_id_cache = {}          # team_name_norm -> team_id
_TTL = 12 * 3600
_DAILY_MAX = int(os.environ.get("HIGHLIGHTLY_DAILY_MAX", "60"))   # under 100 cap
_spend = {"day": None, "count": 0}
_remaining = {"value": None}


def enabled():
    return bool(API_KEY)


def _norm(name):
    return "".join(c for c in (name or "").lower() if c.isalnum())


def _quota_ok():
    today = dt.date.today().isoformat()
    if _spend["day"] != today:
        _spend["day"] = today
        _spend["count"] = 0
    if _spend["count"] >= _DAILY_MAX:
        return False
    try:
        if _remaining["value"] is not None and int(_remaining["value"]) <= 5:
            return False
    except (ValueError, TypeError):
        pass
    return True


def _get(path, params=None):
    import httpx
    headers = {"x-rapidapi-key": API_KEY, "x-rapidapi-host": HOST}
    r = httpx.get(BASE + path, params=params or {}, headers=headers, timeout=15)
    _spend["count"] = _spend.get("count", 0) + 1
    rem = r.headers.get("x-ratelimit-requests-remaining")
    if rem is not None:
        _remaining["value"] = rem
    r.raise_for_status()
    return r.json()


def _find_team_id(name):
    key = _norm(name)
    if key in _id_cache:
        return _id_cache[key]
    if not _quota_ok():
        return None
    try:
        data = _get("/teams", {"league": "NCAA", "name": name, "limit": 5})
    except Exception as e:
        print(f"[highlightly] team lookup failed for {name}: {e}")
        return None
    teams = data if isinstance(data, list) else data.get("data", [])
    for t in teams:
        tn = t.get("displayName") or t.get("name") or ""
        if _norm(tn) == key or key in _norm(tn) or _norm(tn) in key:
            tid = t.get("id")
            _id_cache[key] = tid
            return tid
    return None


def get_team_stats(name, season=None):
    """
    Return {rpg, era, obp, slg, ops, whip} for an NCAA team, or {} if
    unavailable / no key / out of quota. Cached 12h.
    """
    if not API_KEY:
        return {}
    key = _norm(name)
    c = _team_cache.get(key)
    if c and time.time() - c[0] < _TTL:
        return c[1]
    if not _quota_ok():
        return c[1] if c else {}
    tid = _find_team_id(name)
    if not tid:
        return {}
    season = season or dt.date.today().year
    from_date = f"{season}-02-01"
    try:
        data = _get(f"/teams/statistics/{tid}", {"fromDate": from_date})
    except Exception as e:
        print(f"[highlightly] stats failed for {name}: {e}")
        return c[1] if c else {}
    stats = _parse_team_stats(data)
    if stats:
        _team_cache[key] = (time.time(), stats)
    return stats


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_team_stats(data):
    """Extract the fields the run-expectancy model needs from Highlightly's
    team statistics payload. Defensive: returns {} if shape is unexpected."""
    if not isinstance(data, dict):
        if isinstance(data, list) and data:
            data = data[0]
        else:
            return {}
    bat = data.get("batting") or data.get("hitting") or {}
    pit = data.get("pitching") or {}
    games = _num(data.get("games") or data.get("gamesPlayed")) or 0
    runs = _num(bat.get("runs") or bat.get("R"))
    out = {}
    if runs and games:
        out["rpg"] = runs / games
    out["obp"] = _num(bat.get("onBasePercentage") or bat.get("OBP"))
    out["slg"] = _num(bat.get("sluggingPercentage") or bat.get("SLG"))
    out["ops"] = _num(bat.get("onBasePlusSlugging") or bat.get("OPS"))
    out["era"] = _num(pit.get("earnedRunAverage") or pit.get("ERA"))
    # WHIP if components present
    bb = _num(pit.get("walksAllowed") or pit.get("BB"))
    h = _num(pit.get("hitsAllowed") or pit.get("H"))
    ip = _num(pit.get("inningsPitched") or pit.get("IP"))
    if bb is not None and h is not None and ip:
        out["whip"] = (bb + h) / ip
    return {k: v for k, v in out.items() if v is not None}


def quota():
    return {"remaining": _remaining["value"],
            "spend_today": _spend.get("count", 0), "cap": _DAILY_MAX}
