"""
ncaaf_stats.py — College football advanced stats from CollegeFootballData (CFBD).

WHY CFBD
It's the best free college football data available: SP+ ratings, EPA/PPA per play,
success rate, explosiveness, havoc rate, finishing drives, and returning
production. That's the college equivalent of what nflverse gives us for the NFL.

SETUP (one time)
  1. Get a free key at https://collegefootballdata.com/key (arrives by email)
  2. Add it in Railway as:  CFBD_KEY

Same rules as the other data modules:
  * Every function returns None on failure and NEVER raises.
  * Cached — these are season-level numbers that move weekly at most.
  * `status()` reports which endpoints answered, row counts, and real field names.

Kill switch: NCAAF_STATS=0
"""
import datetime as dt
import os
import time

_ENABLED = os.environ.get("NCAAF_STATS", "1").strip().lower() not in ("0", "false", "no")
_KEY = os.environ.get("CFBD_KEY", "").strip()
_TTL = int(os.environ.get("NCAAF_STATS_TTL", str(24 * 3600)))
# CFBD free tier = 1,000 calls/month. These are season-level numbers that
# move at most weekly, so a daily refresh is plenty and keeps us well under.

BASE = "https://api.collegefootballdata.com"

_cache = {}
_health = {}


def season_year(when=None):
    """College football seasons are named for the August they start in."""
    d = when or dt.date.today()
    return d.year if d.month >= 8 else d.year - 1


def has_key():
    return bool(_KEY)


def key_info():
    """Describe the key WITHOUT exposing it: length, masked ends, and whether it
    picked up stray quotes or whitespace (the usual cause of a 401)."""
    raw = os.environ.get("CFBD_KEY")
    if raw is None:
        return {"set": False, "note": "CFBD_KEY is not set in the environment"}
    stripped = raw.strip()
    return {
        "set": True,
        "length": len(stripped),
        "masked": (stripped[:3] + "..." + stripped[-3:]) if len(stripped) > 8 else "??",
        "had_surrounding_whitespace": raw != raw.strip(),
        "has_quotes": stripped.startswith(("\"", "'")) or stripped.endswith(("\"", "'")),
        "starts_with_bearer": stripped.lower().startswith("bearer"),
        "note": ("Paste ONLY the token into CFBD_KEY \u2014 no quotes, no 'Bearer ' "
                 "prefix. After changing it in Railway the service must redeploy "
                 "for the new value to load."),
    }


def ping():
    """Cheapest possible authenticated call, to isolate auth from endpoint shape."""
    d = _get("ping_conferences", "/conferences", nocache=True)
    return {"ok": isinstance(d, list) and len(d) > 0,
            "rows": len(d) if isinstance(d, list) else None,
            "health": _health.get("ping_conferences")}


def _get(key, path, params=None, timeout=25.0, nocache=False):
    """GET a CFBD endpoint -> parsed JSON, or None on ANY failure."""
    if not _ENABLED:
        return None
    if not _KEY:
        _health[key] = {"ok": False, "error": "no CFBD_KEY set"}
        return None
    now = time.time()
    hit = _cache.get(key)
    if hit and not nocache and now - hit[0] < _TTL:
        _health[key] = {"ok": True, "source": "cache",
                        "rows": len(hit[1]) if isinstance(hit[1], list) else None}
        return hit[1]
    try:
        import httpx
        r = httpx.get(f"{BASE}{path}", params=params or {},
                      headers={"Authorization": f"Bearer {_KEY}",
                               "Accept": "application/json"},
                      timeout=timeout, follow_redirects=True)
        if r.status_code == 401:
            _health[key] = {"ok": False, "error": "401 — key rejected"}
            return None
        if r.status_code != 200:
            _health[key] = {"ok": False, "error": f"HTTP {r.status_code}"}
            return None
        data = r.json()
        _health[key] = {"ok": True, "source": "network",
                        "rows": len(data) if isinstance(data, list) else None,
                        "fields": (list(data[0].keys())[:20]
                                   if isinstance(data, list) and data
                                   and isinstance(data[0], dict) else None)}
        _cache[key] = (now, data)
        return data
    except Exception as e:
        _health[key] = {"ok": False, "error": str(e)[:120]}
        return None


def _team_key(v):
    return str(v or "").strip().lower()


# ----------------------------- team ratings -----------------------------
def sp_ratings(year=None):
    """SP+ ratings — the best public all-in-one team rating.
    -> {team_lower: {overall, offense, defense, special_teams}}"""
    year = year or season_year()
    rows = _get(f"sp_{year}", "/ratings/sp", {"year": year})
    if not isinstance(rows, list) or not rows:
        return None
    out = {}
    for r in rows:
        t = _team_key(r.get("team"))
        if not t:
            continue
        off, dfn = r.get("offense") or {}, r.get("defense") or {}
        out[t] = {
            "overall": r.get("rating"),
            "offense": off.get("rating") if isinstance(off, dict) else off,
            "defense": dfn.get("rating") if isinstance(dfn, dict) else dfn,
            "special_teams": ((r.get("specialTeams") or {}).get("rating")
                              if isinstance(r.get("specialTeams"), dict) else None),
            "conference": r.get("conference"),
        }
    return out or None


def team_advanced(year=None):
    """Advanced season stats per team: EPA/play, success rate, explosiveness,
    havoc, finishing drives — split offense vs defense."""
    year = year or season_year()
    rows = _get(f"adv_{year}", "/stats/season/advanced", {"year": year})
    if not isinstance(rows, list) or not rows:
        return None
    out = {}
    for r in rows:
        t = _team_key(r.get("team"))
        if not t:
            continue
        o, d = r.get("offense") or {}, r.get("defense") or {}

        def pick(side, *keys):
            cur = side
            for k in keys:
                if not isinstance(cur, dict):
                    return None
                cur = cur.get(k)
            return cur

        out[t] = {
            "off_ppa": o.get("ppa"), "off_success": o.get("successRate"),
            "off_explosive": o.get("explosiveness"),
            "off_rush_ppa": pick(o, "rushingPlays", "ppa"),
            "off_pass_ppa": pick(o, "passingPlays", "ppa"),
            "def_ppa": d.get("ppa"), "def_success": d.get("successRate"),
            "def_explosive": d.get("explosiveness"),
            "def_havoc": pick(d, "havoc", "total"),
            "def_rush_ppa": pick(d, "rushingPlays", "ppa"),
            "def_pass_ppa": pick(d, "passingPlays", "ppa"),
        }
    return out or None


def ppa_teams(year=None):
    """Predicted Points Added per team (CFBD's EPA equivalent)."""
    year = year or season_year()
    rows = _get(f"ppa_{year}", "/ppa/teams", {"year": year})
    if not isinstance(rows, list) or not rows:
        return None
    out = {}
    for r in rows:
        t = _team_key(r.get("team"))
        if t:
            out[t] = r
    return out or None


def team_profile(team, year=None):
    """Everything we hold on one team, from the real sources only."""
    if not team:
        return None
    k = _team_key(team)
    prof = {"team": team, "season": year or season_year()}
    sp = sp_ratings(year)
    if sp and k in sp:
        prof["sp_plus"] = sp[k]
    adv = team_advanced(year)
    if adv and k in adv:
        prof["advanced"] = adv[k]
    return prof if len(prof) > 2 else None


# ----------------------------- players -----------------------------
def player_season(year=None, team=None, category=None):
    """Season stat lines for players (optionally one team / one category)."""
    year = year or season_year()
    params = {"year": year}
    if team:
        params["team"] = team
    if category:
        params["category"] = category
    key = f"pstats_{year}_{team or 'all'}_{category or 'all'}"
    rows = _get(key, "/stats/player/season", params)
    return rows if isinstance(rows, list) and rows else None


def player_ppa(year=None, team=None):
    """Per-player PPA — who actually creates value, not just volume."""
    year = year or season_year()
    params = {"year": year}
    if team:
        params["team"] = team
    rows = _get(f"pppa_{year}_{team or 'all'}", "/ppa/players/season", params)
    return rows if isinstance(rows, list) and rows else None


def status(year=None):
    """Which CFBD endpoints answer from this server, with counts and fields."""
    year = year or season_year()
    if not _KEY:
        return {"enabled": _ENABLED, "season": year, "key_set": False,
                "key_info": key_info(),
                "note": ("No CFBD_KEY set. Get a free key at "
                         "https://collegefootballdata.com/key and add it in "
                         "Railway as CFBD_KEY.")}
    res = {}
    for label, fn in (("sp_ratings", lambda: sp_ratings(year)),
                      ("team_advanced", lambda: team_advanced(year)),
                      ("ppa_teams", lambda: ppa_teams(year))):
        t0 = time.time()
        try:
            d = fn()
        except Exception as e:
            d = None
            _health[label] = {"ok": False, "error": str(e)[:120]}
        res[label] = {"teams": len(d) if d else 0,
                      "secs": round(time.time() - t0, 1)}
        if d:
            first_k = next(iter(d))
            res[label]["sample"] = {first_k: d[first_k]}
    try:
        pp = player_ppa(year, "Alabama")
        res["player_ppa"] = {"rows": len(pp) if pp else 0,
                             "fields": list(pp[0].keys())[:16] if pp else None}
    except Exception as e:
        res["player_ppa"] = {"error": str(e)[:120]}
    return {"enabled": _ENABLED, "season": year, "key_set": True,
            "key_info": key_info(), "ping": ping(),
            "results": res, "endpoint_health": _health}



# SP+ POLARITY NOTE (important):
#   overall / offense  -> HIGHER is better
#   defense            -> LOWER is better (points allowed per drive-ish scale)
# Any code comparing defense ratings must not treat "bigger" as "stronger".

def matchup(home, away, year=None):
    """Side-by-side SP+ and advanced profile for one game, with the raw gaps.
    Returns only measured values; no prediction is implied here."""
    if not home or not away:
        return None
    hp, ap = team_profile(home, year), team_profile(away, year)
    if not hp or not ap:
        return {"home": hp, "away": ap,
                "note": "one or both teams not found in CFBD for this season"}
    out = {"home": hp, "away": ap, "season": year or season_year(), "edges": {}}
    hs, as_ = hp.get("sp_plus") or {}, ap.get("sp_plus") or {}
    if hs.get("overall") is not None and as_.get("overall") is not None:
        out["edges"]["sp_overall_home_minus_away"] = round(
            hs["overall"] - as_["overall"], 2)
    # offense vs the OTHER team's defense (defense: lower = better)
    if hs.get("offense") is not None and as_.get("defense") is not None:
        out["edges"]["home_off_vs_away_def"] = round(
            hs["offense"] - as_["defense"], 2)
    if as_.get("offense") is not None and hs.get("defense") is not None:
        out["edges"]["away_off_vs_home_def"] = round(
            as_["offense"] - hs["defense"], 2)
    ha, aa = hp.get("advanced") or {}, ap.get("advanced") or {}
    if ha.get("off_ppa") is not None and aa.get("def_ppa") is not None:
        out["edges"]["home_ppa_matchup"] = round(ha["off_ppa"] - aa["def_ppa"], 3)
    if aa.get("off_ppa") is not None and ha.get("def_ppa") is not None:
        out["edges"]["away_ppa_matchup"] = round(aa["off_ppa"] - ha["def_ppa"], 3)
    return out
