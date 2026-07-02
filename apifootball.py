"""
apifootball.py — api-football (v3.football.api-sports.io) client.

Rich soccer team stats: goals for/against + averages, home/away W-D-L, clean
sheets, failed-to-score, biggest win/loss, formations, recent form. Tuned to the
real /teams/statistics response shape.

Honest limits:
  * The free tier is 100 requests/day, so this tracks a daily budget and stops
    hitting the network when it's spent (serving cache or None instead).
  * xG is NOT in /teams/statistics; it lives per-fixture in /fixtures/statistics.
    Season xG would need per-fixture aggregation, which the budget can't afford
    daily — so we expose the rich season stats here and leave xG for later.

Key: set APIFOOTBALL_KEY in the environment (Railway). Never hardcode it.
"""
import os
import time
import datetime as dt

BASE = "https://v3.football.api-sports.io"
_KEY = os.environ.get("APIFOOTBALL_KEY", "")
_DAILY = int(os.environ.get("APIFOOTBALL_DAILY", "100"))

_cache = {}                 # url -> (ts, json)
_TTL = 12 * 3600            # season stats change slowly
_budget = {"day": None, "used": 0}


def available():
    return bool(_KEY)


def _spend():
    today = dt.date.today().isoformat()
    if _budget["day"] != today:
        _budget["day"] = today
        _budget["used"] = 0
    if _budget["used"] >= _DAILY:
        return False
    _budget["used"] += 1
    return True


def _get(path, params):
    if not _KEY:
        return None
    url = BASE + path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    if not _spend():
        return hit[1] if hit else None       # out of daily budget
    try:
        import httpx
        r = httpx.get(BASE + path, params=params,
                      headers={"x-apisports-key": _KEY}, timeout=15.0)
        r.raise_for_status()
        j = r.json()
    except Exception:
        return hit[1] if hit else None
    _cache[url] = (time.time(), j)
    return j


def _teams_index(league, season):
    """{normalized_team_name: api-football team id} for a league+season (cached)."""
    j = _get("/teams", {"league": league, "season": season})
    if not j or not j.get("response"):
        return {}
    import name_match
    return name_match.build_index(
        [{"name": row.get("team", {}).get("name"), "id": row.get("team", {}).get("id")}
         for row in j["response"]])


def resolve_team_id(espn_name, league, season):
    import name_match
    return name_match.match(espn_name, _teams_index(league, season))


def _rec(wins, draws, loses):
    return f"{wins}-{draws}-{loses}"


def team_statistics(league, season, team_id):
    """Parse /teams/statistics into a clean, display-ready dict of real stats."""
    j = _get("/teams/statistics", {"league": league, "season": season, "team": team_id})
    if not j or not isinstance(j.get("response"), dict):
        return None
    r = j["response"]
    fx = r.get("fixtures", {}) or {}
    g = r.get("goals", {}) or {}
    gf = (g.get("for", {}) or {}); ga = (g.get("against", {}) or {})
    big = r.get("biggest", {}) or {}
    played = (fx.get("played", {}) or {})
    wins = (fx.get("wins", {}) or {}); draws = (fx.get("draws", {}) or {}); loses = (fx.get("loses", {}) or {})

    def _num(x):
        try:
            return float(x)
        except Exception:
            return None

    forms = [f"{l.get('formation')} ({l.get('played')})" for l in (r.get("lineups") or [])[:3]]
    return {
        "source": "api-football",
        "team": (r.get("team", {}) or {}).get("name"),
        "logo": (r.get("team", {}) or {}).get("logo"),
        "league": (r.get("league", {}) or {}).get("name"),
        "season": (r.get("league", {}) or {}).get("season"),
        "form": (r.get("form") or "")[-10:],
        "played": played.get("total"),
        "record": _rec(wins.get("total"), draws.get("total"), loses.get("total")),
        "home_record": _rec(wins.get("home"), draws.get("home"), loses.get("home")),
        "away_record": _rec(wins.get("away"), draws.get("away"), loses.get("away")),
        "gf": (gf.get("total", {}) or {}).get("total"),
        "ga": (ga.get("total", {}) or {}).get("total"),
        "gf_avg": _num((gf.get("average", {}) or {}).get("total")),
        "ga_avg": _num((ga.get("average", {}) or {}).get("total")),
        "clean_sheets": (r.get("clean_sheet", {}) or {}).get("total"),
        "failed_to_score": (r.get("failed_to_score", {}) or {}).get("total"),
        "biggest_win": (big.get("wins", {}) or {}).get("home") or (big.get("wins", {}) or {}).get("away"),
        "biggest_loss": (big.get("loses", {}) or {}).get("home") or (big.get("loses", {}) or {}).get("away"),
        "formations": forms,
        "budget_used": _budget["used"], "budget_max": _DAILY,
    }


def team_stats(espn_name, league, season):
    """High-level: resolve an ESPN team name to its api-football id, then fetch
    that team's season statistics. Returns None if we can't confidently match."""
    tid = resolve_team_id(espn_name, league, season)
    if tid is None:
        return {"unmatched": espn_name, "budget_used": _budget["used"], "budget_max": _DAILY}
    return team_statistics(league, season, tid)


# ---- per-fixture xG + match stats (lazy: one match at a time) ----------------
_STAT_KEYS = {
    "expected_goals": "xg", "goals_prevented": "goals_prevented",
    "Ball Possession": "possession", "Total Shots": "shots",
    "Shots on Goal": "shots_on", "Shots insidebox": "shots_inside",
    "Corner Kicks": "corners", "Passes %": "pass_pct", "Total passes": "passes",
    "Goalkeeper Saves": "saves", "Fouls": "fouls", "Yellow Cards": "yellow",
    "Red Cards": "red", "Offsides": "offsides",
}


def fixture_stats(fixture_id):
    """Per-team match stats (xG, shots, possession, passing) keyed by team id."""
    j = _get("/fixtures/statistics", {"fixture": fixture_id})
    if not j or not j.get("response"):
        return None
    out = {}
    for entry in j["response"]:
        tid = (entry.get("team", {}) or {}).get("id")
        d = {"team": (entry.get("team", {}) or {}).get("name"), "team_id": tid}
        for s in entry.get("statistics", []) or []:
            key = _STAT_KEYS.get(s.get("type"))
            if not key:
                continue
            v = s.get("value")
            if key in ("xg", "goals_prevented"):
                try:
                    v = round(float(v), 2)
                except Exception:
                    v = None
            d[key] = v
        out[str(tid)] = d
    return out


def _close(a, b):
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def find_fixture(league, season, date_iso, home_name, away_name):
    """Resolve an ESPN match (date + team names) to the api-football fixture id
    and the two team ids. Caches the day's fixtures so it's one call per league-day."""
    j = _get("/fixtures", {"league": league, "season": season, "date": date_iso})
    if not j or not j.get("response"):
        return None
    import name_match as N
    hn, an = N._norm(home_name), N._norm(away_name)
    for fx in j["response"]:
        t = fx.get("teams", {}) or {}
        fh = N._norm((t.get("home") or {}).get("name"))
        fa = N._norm((t.get("away") or {}).get("name"))
        if _close(hn, fh) and _close(an, fa):
            return ((fx.get("fixture", {}) or {}).get("id"),
                    (t.get("home") or {}).get("id"), (t.get("away") or {}).get("id"))
    return None


def match_xg(league, season, date_iso, home_name, away_name):
    """xG + match stats for a single ESPN match. Two cached calls at most
    (day fixtures, then that fixture's statistics). None/unmatched on any miss."""
    found = find_fixture(league, season, date_iso, home_name, away_name)
    if not found:
        return {"unmatched": f"{away_name} @ {home_name}",
                "budget_used": _budget["used"], "budget_max": _DAILY}
    fid, hid, aid = found
    stats = fixture_stats(fid)
    if not stats:
        return None
    return {"fixture_id": fid, "home": stats.get(str(hid)), "away": stats.get(str(aid)),
            "budget_used": _budget["used"], "budget_max": _DAILY}
