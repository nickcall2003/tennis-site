"""
nhl_stats.py — NHL data from the official NHL APIs (free, no key, datacenter-OK).

TWO OFFICIAL HOSTS
  api-web.nhle.com   -> player landing pages, game logs, rosters, standings
  api.nhle.com/stats -> the "REST" statistics tables (skater/goalie/team summary,
                        plus advanced tables: on-ice rates, faceoffs, penalties)

Built to the same rules as the MLB/Statcast modules:
  * Every function returns None on failure and NEVER raises.
  * Cached so we hit the API once per window, not once per page view.
  * `status()` reports exactly which endpoints answered and what keys came back,
    so a wrong parameter shows up as a broken endpoint instead of silent zeros.

Kill switch: NHL_STATS=0
"""
import datetime as dt
import os
import time

_ENABLED = os.environ.get("NHL_STATS", "1").strip().lower() not in ("0", "false", "no")
_TTL = int(os.environ.get("NHL_STATS_TTL", "3600"))

WEB = "https://api-web.nhle.com/v1"
REST = "https://api.nhle.com/stats/rest/en"
SEARCH = "https://search.d3.nhle.com/api/v1/search/player"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "application/json",
}

_cache = {}
_health = {}


def season_id(when=None):
    """NHL seasons are '20252026'. Runs Oct -> Jun, so before September we're
    still describing the season that just finished."""
    d = when or dt.date.today()
    start = d.year if d.month >= 9 else d.year - 1
    return f"{start}{start + 1}"


def _get(key, url, params=None, timeout=15.0, nocache=False):
    """GET JSON -> dict/list, or None on ANY failure."""
    if not _ENABLED:
        return None
    now = time.time()
    hit = _cache.get(key)
    if hit and not nocache and now - hit[0] < _TTL:
        _health[key] = {"ok": True, "source": "cache"}
        return hit[1]
    try:
        import httpx
        r = httpx.get(url, params=params or {}, headers=_HEADERS,
                      timeout=timeout, follow_redirects=True)
        if r.status_code != 200:
            _health[key] = {"ok": False, "error": f"HTTP {r.status_code}"}
            return None
        data = r.json()
        _health[key] = {"ok": True, "source": "network",
                        "keys": (list(data.keys())[:12] if isinstance(data, dict)
                                 else f"list[{len(data)}]")}
        _cache[key] = (now, data)
        return data
    except Exception as e:
        _health[key] = {"ok": False, "error": str(e)[:120]}
        return None


# ----------------------------- players -----------------------------
def search_player(name):
    """Name -> [{id, name, team, position}] using the NHL's own search service."""
    if not name:
        return []
    d = _get(f"search_{name.lower()}", SEARCH,
             {"culture": "en-us", "limit": 8, "q": name})
    out = []
    for p in (d or []):
        pid = p.get("playerId") or p.get("id")
        if not pid:
            continue
        nm = (f"{p.get('firstName','')} {p.get('lastName','')}".strip()
              or p.get("name"))
        out.append({"id": pid, "name": nm,
                    "team": p.get("teamAbbrev") or p.get("teamId"),
                    "position": p.get("positionCode")})
    return out


def _fmt(v):
    return v.get("default") if isinstance(v, dict) else v


def player_profile(pid):
    """Full NHL player profile: bio, current season, career totals, season-by-season,
    and recent games. Skaters and goalies return their own stat sets."""
    if not pid:
        return None
    d = _get(f"landing_{pid}", f"{WEB}/player/{pid}/landing")
    if not d:
        return None
    pos = d.get("position")
    is_goalie = str(pos).upper() == "G"
    prof = {
        "id": pid,
        "name": f"{_fmt(d.get('firstName')) or ''} {_fmt(d.get('lastName')) or ''}".strip(),
        "team": _fmt(d.get("fullTeamName")) or d.get("currentTeamAbbrev"),
        "position": pos,
        "number": d.get("sweaterNumber"),
        "shoots": d.get("shootsCatches"),
        "height": d.get("heightInInches"),
        "weight": d.get("weightInPounds"),
        "birth_date": d.get("birthDate"),
        "birth_city": _fmt(d.get("birthCity")),
        "birth_country": d.get("birthCountry"),
        "draft": d.get("draftDetails"),
        "headshot": d.get("headshot"),
        "is_goalie": is_goalie,
        "_sections": [],
    }
    feat = d.get("featuredStats") or {}
    reg = (feat.get("regularSeason") or {})
    if reg.get("subSeason"):
        prof["season"] = reg["subSeason"]
        prof["_sections"].append("season")
    if reg.get("career"):
        prof["career"] = reg["career"]
        prof["_sections"].append("career")
    ct = d.get("careerTotals") or {}
    if ct.get("regularSeason"):
        prof["career_totals"] = ct["regularSeason"]
        if ct.get("playoffs"):
            prof["playoff_totals"] = ct["playoffs"]
        prof["_sections"].append("career_totals")
    seasons = d.get("seasonTotals") or []
    if seasons:
        nhl = [s for s in seasons if s.get("leagueAbbrev") == "NHL"
               and s.get("gameTypeId") == 2]
        if nhl:
            prof["by_season"] = nhl[-10:]
            prof["_sections"].append("by_season")
    last5 = d.get("last5Games") or []
    if last5:
        prof["last_5"] = last5
        prof["_sections"].append("last_5")
    return prof


# ----------------------------- league tables -----------------------------
def _rest_table(name, table, season=None, extra=None, limit=1000):
    season = season or season_id()
    params = {"isAggregate": "false", "isGame": "false", "limit": limit,
              "start": 0, "sort": "[]",
              "cayenneExp": f"seasonId={season} and gameTypeId=2"}
    if extra:
        params.update(extra)
    d = _get(f"{name}_{season}", f"{REST}/{table}", params)
    if not isinstance(d, dict):
        return None
    rows = d.get("data")
    return rows if isinstance(rows, list) and rows else None


def team_summary(season=None):
    """Team-level season summary: goals for/against, PP%, PK%, shots, etc."""
    rows = _rest_table("team_summary", "team/summary", season)
    if not rows:
        return None
    out = {}
    for r in rows:
        nm = (r.get("teamFullName") or "").upper()
        if nm:
            out[nm] = r
    return out or None


def skater_summary(season=None):
    """Every skater's season line (G, A, P, TOI, shots, +/-, etc.)."""
    rows = _rest_table("skater_summary", "skater/summary", season)
    if not rows:
        return None
    out = {}
    for r in rows:
        nm = (r.get("skaterFullName") or "").strip().lower()
        if nm:
            out[nm] = r
    return out or None


def goalie_summary(season=None):
    """Every goalie's season line (SV%, GAA, wins, shutouts)."""
    rows = _rest_table("goalie_summary", "goalie/summary", season)
    if not rows:
        return None
    out = {}
    for r in rows:
        nm = (r.get("goalieFullName") or "").strip().lower()
        if nm:
            out[nm] = r
    return out or None


def skater_advanced(season=None):
    """On-ice rates: Corsi/Fenwick-style shot share, zone starts, on-ice goals."""
    rows = _rest_table("skater_onice", "skater/summaryshooting", season)
    if not rows:
        rows = _rest_table("skater_onice2", "skater/percentages", season)
    if not rows:
        return None
    out = {}
    for r in rows:
        nm = (r.get("skaterFullName") or "").strip().lower()
        if nm:
            out[nm] = r
    return out or None


def status(season=None):
    """Which NHL endpoints answer from THIS server, with the keys they returned."""
    season = season or season_id()
    res = {}
    probes = {
        "team_summary": lambda: team_summary(season),
        "skater_summary": lambda: skater_summary(season),
        "goalie_summary": lambda: goalie_summary(season),
        "skater_advanced": lambda: skater_advanced(season),
    }
    for k, fn in probes.items():
        t0 = time.time()
        try:
            d = fn()
        except Exception as e:
            d = None
            _health[k] = {"ok": False, "error": str(e)[:120]}
        res[k] = {"rows": len(d) if d else 0, "secs": round(time.time() - t0, 1)}
        if d:
            first = list(d.values())[0]
            res[k]["sample_fields"] = list(first.keys())[:16] if isinstance(first, dict) else None
    # one player probe end-to-end
    try:
        s = search_player("McDavid")
        res["search"] = {"found": len(s), "first": s[0] if s else None}
        if s:
            p = player_profile(s[0]["id"])
            res["player_profile"] = {"ok": bool(p),
                                     "sections": (p or {}).get("_sections")}
    except Exception as e:
        res["search"] = {"error": str(e)[:120]}
    return {"enabled": _ENABLED, "season": season, "results": res,
            "endpoint_health": _health}
