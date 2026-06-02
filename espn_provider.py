"""
espn_provider.py
----------------
Adapter for ESPN's free hidden API (no key). Powers NBA and NFL.

Scoreboard gives us, per game: both teams (name, abbr, logo, season W-L record),
live score, status, start time, and venue. From the records we derive win% ->
the team_model prediction. Cached per date; live scores refresh on a short TTL.

ESPN endpoints (site.api.espn.com):
  NBA: /apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD
  NFL: /apis/site/v2/sports/football/nfl/scoreboard?dates=YYYYMMDD
"""

from __future__ import annotations

import datetime as dt
import time

from team_model import predict

SCOREBOARD = {
    "nba": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "nfl": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
}
SUMMARY = {
    "nba": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary",
    "nfl": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary",
}

_cache = {}          # (sport,date) -> (ts, [games])
_DAY_TTL = 6 * 3600
_LIVE_TTL = 25


def _get(url, params=None):
    import httpx
    r = httpx.get(url, params=params or {}, timeout=20.0)
    r.raise_for_status()
    return r.json()


def _record_winpct(team):
    """Pull season win% from a competitor's records array."""
    for rec in team.get("records", []) or []:
        summ = rec.get("summary", "")
        if "-" in summ:
            try:
                parts = [int(x) for x in summ.split("-")[:2]]
                w, l = parts[0], parts[1]
                if w + l > 0:
                    return w / (w + l), summ
            except (ValueError, IndexError):
                continue
    return None, ""


def _status(comp):
    st = ((comp.get("status") or {}).get("type") or {})
    state = st.get("state", "")
    if state == "post":
        return "finished"
    if state == "in":
        return "live"
    return "scheduled"


def _ct_time(iso):
    try:
        utc = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        ct = utc - dt.timedelta(hours=5)
        h = ct.hour % 12 or 12
        return f"{h}:{ct.minute:02d} {'AM' if ct.hour < 12 else 'PM'} CT"
    except Exception:
        return ""


def _side(competitor):
    t = competitor.get("team", {}) or {}
    wp, rec = _record_winpct(competitor)
    logo = t.get("logo")
    if not logo:
        logos = t.get("logos") or []
        logo = logos[0]["href"] if logos else None
    return {
        "team_id": t.get("id"), "name": t.get("displayName", "Team"),
        "abbr": t.get("abbreviation", ""), "logo": logo,
        "record": rec, "win_pct": wp,
        "score": _to_int(competitor.get("score")),
    }


def _to_int(v):
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def get_games(sport: str, date: dt.date, force_live=False):
    key = (sport, date.isoformat())
    c = _cache.get(key)
    if c and not force_live and time.time() - c[0] < _DAY_TTL:
        return c[1]
    try:
        data = _get(SCOREBOARD[sport], {"dates": date.strftime("%Y%m%d")})
    except Exception:
        return []
    games = []
    for ev in data.get("events", []):
        comps = ev.get("competitions", [])
        if not comps:
            continue
        comp = comps[0]
        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        h, a = _side(home), _side(away)
        pred = predict(sport, h["win_pct"], a["win_pct"])
        status = _status(comp)
        venue = (comp.get("venue", {}) or {}).get("fullName", "")
        # prominence: combined win% (better teams = bigger game)
        prominence = (h["win_pct"] or 0.5) + (a["win_pct"] or 0.5)
        st = ((comp.get("status") or {}).get("type") or {})
        games.append({
            "id": ev.get("id"), "sport": sport, "status": status,
            "event_time": _ct_time(ev.get("date", "")),
            "home": h, "away": a,
            "prob_home": pred["prob_home"], "exp_margin": pred["exp_margin"],
            "confidence": pred["confidence"], "avg_total": pred["avg_total"],
            "venue": venue, "prominence": prominence,
            "score": {"home": h["score"], "away": a["score"],
                      "detail": st.get("shortDetail", "")},
            "winner": ("home" if (status == "finished" and (h["score"] or 0) > (a["score"] or 0))
                       else "away" if status == "finished" else None),
        })
    _cache[key] = (time.time(), games)
    return games


def get_game(sport: str, date: dt.date, game_id: str):
    key = (sport, date.isoformat())
    c = _cache.get(key)
    games = c[1] if (c and time.time() - c[0] < _LIVE_TTL) else get_games(sport, date, force_live=True)
    for g in games:
        if str(g["id"]) == str(game_id):
            return g
    return None


# ---- player props (NBA / NFL) ------------------------------------------
# ESPN's scoreboard includes a "leaders" block per team with each team's top
# players and their season averages. We turn those into prop projections.
# This is a v1: it covers the headline players ESPN surfaces, not full rosters.

_PROP_STATS = {
    "nba": [("points", "Points"), ("rebounds", "Rebounds"), ("assists", "Assists")],
    "nfl": [("passingYards", "Pass Yds"), ("rushingYards", "Rush Yds"),
            ("receivingYards", "Rec Yds")],
}
_STAT_KEY = {  # ESPN leader category name -> our prop stat key
    "points": "points", "rebounds": "rebounds", "assists": "assists",
    "passingYards": "passing_yards", "rushingYards": "rushing_yards",
    "receivingYards": "receiving_yards",
}


def _leaders_from_event(sport, ev):
    """Extract per-player season averages from an event's leaders block."""
    out = []
    comps = ev.get("competitions", [])
    if not comps:
        return out
    for comp in comps:
        for team in comp.get("competitors", []):
            tname = (team.get("team", {}) or {}).get("abbreviation", "")
            for cat in team.get("leaders", []) or []:
                catname = cat.get("name", "")
                stat_key = _STAT_KEY.get(catname)
                if not stat_key:
                    continue
                for ldr in cat.get("leaders", []) or []:
                    ath = ldr.get("athlete", {}) or {}
                    # ESPN gives the season average in 'value' for season-leader cats
                    val = ldr.get("value")
                    if val is None:
                        continue
                    out.append({"player": ath.get("displayName", "Player"),
                                "team": tname, "stat": stat_key, "rate": float(val)})
    return out


def get_props(sport: str, date: dt.date, game_id: str):
    from props import project_prop, default_line
    g = get_game(sport, date, game_id)
    if not g:
        return {"error": "not found"}
    # re-fetch the raw event to read its leaders block
    try:
        data = _get(SCOREBOARD[sport], {"dates": date.strftime("%Y%m%d")})
    except Exception:
        return {"game_id": game_id, "props": []}
    ev = next((e for e in data.get("events", []) if str(e.get("id")) == str(game_id)), None)
    if not ev:
        return {"game_id": game_id, "props": []}
    leaders = _leaders_from_event(sport, ev)
    out = []
    for L in leaders:
        if L["rate"] <= 0:
            continue
        line = default_line(L["stat"], L["rate"])
        proj = project_prop(L["stat"], L["rate"], line)
        if not proj:
            continue
        proj["player"] = L["player"]
        proj["team"] = L["team"]
        proj["label"] = dict(_PROP_STATS[sport]).get(
            next((k for k, v in _STAT_KEY.items() if v == L["stat"]), ""), L["stat"])
        out.append(proj)
    # strongest edges first
    out.sort(key=lambda p: -p["edge"])
    return {"game_id": game_id, "props": out}
