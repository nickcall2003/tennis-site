"""
hoops_advanced.py — advanced NBA/WNBA stats (defense-vs-position, usage rate,
defensive rating, pace) from the official stats.nba.com / stats.wnba.com endpoints.

WHY THIS FILE IS DEFENSIVE:
stats.nba.com is known to block/throttle datacenter IPs (the same failure mode
yfinance hit on Railway). So EVERY function here returns None on any failure and
NEVER raises. If the host blocks us, the app simply shows no advanced context —
it must never break a page, and it must never invent a number.

Call `status()` to see whether the source is actually reachable from the server.

Endpoints used (all public, no key):
  leaguedashteamstats            -> team DEF RTG, PACE
  leaguedashplayerstats          -> player USG%, MIN
  leaguedashptdefend / matchups  -> (NBA only) defensive detail

Set HOOPS_ADV=0 to disable entirely.
"""
import os
import time

_ENABLED = os.environ.get("HOOPS_ADV", "1").strip().lower() not in ("0", "false", "no")
_TTL = 6 * 3600
_cache = {}
_health = {"ok": None, "checked": 0, "error": None}

_HOST = {
    "nba": "https://stats.nba.com/stats",
    "wnba": "https://stats.wnba.com/stats",
}
_LEAGUE_ID = {"nba": "00", "wnba": "10"}

# stats.nba.com requires browser-ish headers or it hangs/403s.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Connection": "keep-alive",
}


def _season(sport):
    """NBA seasons span years ('2025-26'); WNBA is a single year ('2026')."""
    import datetime as dt
    now = dt.date.today()
    if sport == "wnba":
        return str(now.year)
    yr = now.year if now.month >= 10 else now.year - 1
    return f"{yr}-{str(yr + 1)[-2:]}"


# ---- uploaded snapshot (the working path: fetched on a home IP, uploaded here) ----
STORE = os.environ.get("HOOPS_ADV_PATH", "/data/hoops_adv.json")
_disk = {"t": 0, "data": None}


def save_uploaded(payload):
    """Persist an uploaded snapshot from fetch_hoops.py."""
    import json
    if not isinstance(payload, dict) or not payload:
        return {"error": "empty payload"}
    os.makedirs(os.path.dirname(STORE) or ".", exist_ok=True)
    with open(STORE, "w") as f:
        json.dump(payload, f)
    _disk["t"] = 0            # force reload
    summary = {}
    for sp, blob in payload.items():
        if isinstance(blob, dict):
            summary[sp] = {"teams": len(blob.get("teams") or {}),
                           "players": len(blob.get("players") or {}),
                           "dvp_teams": len(blob.get("dvp") or {}),
                           "fetched": blob.get("fetched")}
    return {"ok": True, "saved": STORE, "summary": summary}


def _load_disk():
    """The uploaded snapshot, cached in memory for 5 minutes."""
    import json
    if _disk["data"] is not None and time.time() - _disk["t"] < 300:
        return _disk["data"]
    try:
        with open(STORE) as f:
            _disk["data"] = json.load(f)
    except Exception:
        _disk["data"] = {}
    _disk["t"] = time.time()
    return _disk["data"]


def uploaded_status():
    d = _load_disk()
    out = {"has_file": bool(d), "path": STORE, "sports": {}}
    for sp, blob in (d or {}).items():
        if isinstance(blob, dict):
            out["sports"][sp] = {"fetched": blob.get("fetched"),
                                 "teams": len(blob.get("teams") or {}),
                                 "players": len(blob.get("players") or {}),
                                 "dvp_teams": len(blob.get("dvp") or {})}
    return out


def _blob(sport):
    d = _load_disk()
    b = (d or {}).get(sport)
    return b if isinstance(b, dict) else None


def defense_vs_position(sport, team_name, position):
    """What a team allows to a position: {pts, reb, ast, fg3m} \u2014 the real
    'opponent allows X to small forwards' stat. From the uploaded snapshot."""
    b = _blob(sport)
    if not b or not team_name or not position:
        return None
    dvp = b.get("dvp") or {}
    n = str(team_name).upper()
    row = None
    for k, v in dvp.items():
        if k == n or n in k or k in n:
            row = v
            break
    if not row:
        return None
    pos = str(position).strip().title()
    # map fine-grained positions onto the three buckets we store
    bucket = ("Guard" if pos.endswith("Guard") or pos in ("G", "Pg", "Sg")
              else "Center" if pos.startswith("Cent") or pos == "C"
              else "Forward")
    return row.get(bucket) or row.get(pos)


def _get(sport, endpoint, params, timeout=6.0):
    """Fetch a stats.nba.com endpoint -> list[dict], or None on ANY failure."""
    if not _ENABLED or sport not in _HOST:
        return None
    key = (sport, endpoint, tuple(sorted(params.items())))
    c = _cache.get(key)
    if c and time.time() - c[0] < _TTL:
        return c[1]
    try:
        import httpx
        r = httpx.get(f"{_HOST[sport]}/{endpoint}", params=params,
                      headers=_HEADERS, timeout=timeout)
        if r.status_code != 200:
            _health.update({"ok": False, "checked": time.time(),
                            "error": f"HTTP {r.status_code}"})
            return None
        data = r.json()
        rs = (data.get("resultSets") or data.get("resultSet") or [])
        if isinstance(rs, dict):
            rs = [rs]
        if not rs:
            return None
        head = rs[0].get("headers") or []
        rows = rs[0].get("rowSet") or []
        out = [dict(zip(head, row)) for row in rows]
        _cache[key] = (time.time(), out)
        _health.update({"ok": True, "checked": time.time(), "error": None})
        return out
    except Exception as e:
        _health.update({"ok": False, "checked": time.time(), "error": str(e)[:120]})
        return None


def status(sport="wnba"):
    """Is the advanced-stats source actually reachable from this server? Fast, and
    always returns — a blocked host must never hang the request."""
    rows = _get(sport, "leaguedashteamstats", {
        "MeasureType": "Advanced", "PerMode": "PerGame", "Season": _season(sport),
        "SeasonType": "Regular Season", "LeagueID": _LEAGUE_ID[sport],
        "PaceAdjust": "N", "PlusMinus": "N", "Rank": "N", "Month": "0",
        "OpponentTeamID": "0", "Period": "0", "LastNGames": "0",
        "TeamID": "0", "GameSegment": "", "DateFrom": "", "DateTo": "",
        "Conference": "", "Division": "", "GameScope": "", "PlayerExperience": "",
        "PlayerPosition": "", "SeasonSegment": "", "ShotClockRange": "",
        "StarterBench": "", "VsConference": "", "VsDivision": "", "Outcome": "",
        "Location": "",
    })
    return {"enabled": _ENABLED, "reachable": bool(rows),
            "teams": len(rows) if rows else 0, "season": _season(sport),
            "health": dict(_health)}


def team_advanced(sport):
    """{team_name: {def_rtg, off_rtg, pace}} or None.
    Uploaded snapshot FIRST (works from a home IP), then a live call (usually
    blocked from datacenters)."""
    b = _blob(sport)
    if b and b.get("teams"):
        return b["teams"]
    rows = _get(sport, "leaguedashteamstats", {
        "MeasureType": "Advanced", "PerMode": "PerGame", "Season": _season(sport),
        "SeasonType": "Regular Season", "LeagueID": _LEAGUE_ID[sport],
        "PaceAdjust": "N", "PlusMinus": "N", "Rank": "N", "Month": "0",
        "OpponentTeamID": "0", "Period": "0", "LastNGames": "0",
        "TeamID": "0", "GameSegment": "", "DateFrom": "", "DateTo": "",
        "Conference": "", "Division": "", "GameScope": "", "PlayerExperience": "",
        "PlayerPosition": "", "SeasonSegment": "", "ShotClockRange": "",
        "StarterBench": "", "VsConference": "", "VsDivision": "", "Outcome": "",
        "Location": "",
    })
    if not rows:
        return None
    out = {}
    for r in rows:
        abbr = r.get("TEAM_NAME") or r.get("TEAM_ABBREVIATION")
        if not abbr:
            continue
        out[str(abbr).upper()] = {
            "def_rtg": r.get("DEF_RATING"),
            "off_rtg": r.get("OFF_RATING"),
            "pace": r.get("PACE"),
        }
    return out or None


def player_usage(sport):
    """{player_name_lower: {usg_pct, min, ts_pct}} or None. Uploaded snapshot first."""
    b = _blob(sport)
    if b and b.get("players"):
        return b["players"]
    rows = _get(sport, "leaguedashplayerstats", {
        "MeasureType": "Advanced", "PerMode": "PerGame", "Season": _season(sport),
        "SeasonType": "Regular Season", "LeagueID": _LEAGUE_ID[sport],
        "PaceAdjust": "N", "PlusMinus": "N", "Rank": "N", "Month": "0",
        "OpponentTeamID": "0", "Period": "0", "LastNGames": "0",
        "TeamID": "0", "GameSegment": "", "DateFrom": "", "DateTo": "",
        "Conference": "", "Division": "", "DraftPick": "", "DraftYear": "",
        "GameScope": "", "Height": "", "PlayerExperience": "", "PlayerPosition": "",
        "SeasonSegment": "", "ShotClockRange": "", "StarterBench": "",
        "TeamAbbreviation": "", "TwoWay": "", "VsConference": "", "VsDivision": "",
        "Weight": "", "Outcome": "", "Location": "", "College": "", "Country": "",
    })
    if not rows:
        return None
    out = {}
    for r in rows:
        nm = (r.get("PLAYER_NAME") or "").strip().lower()
        if not nm:
            continue
        out[nm] = {"usg_pct": r.get("USG_PCT"), "min": r.get("MIN"),
                   "ts_pct": r.get("TS_PCT")}
    return out or None
