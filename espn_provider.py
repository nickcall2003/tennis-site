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
    """
    Full-roster player props from the game summary boxscore. One call per game
    (covers every listed player), so it stays light on a small instance.
    Each player's season averages drive the projection.
    """
    from props import project_prop, default_line
    try:
        data = _get(SUMMARY[sport], {"event": game_id})
    except Exception:
        return {"game_id": game_id, "props": []}

    # The boxscore.players block lists each team's athletes with stat arrays.
    # For pregame, ESPN populates season AVERAGES; we read the labeled columns.
    box = data.get("boxscore", {}) or {}
    players_block = box.get("players", []) or []
    out = []
    seen = set()
    for team in players_block:
        tabbr = (team.get("team", {}) or {}).get("abbreviation", "")
        for grp in team.get("statistics", []) or []:
            gname = (grp.get("name") or grp.get("type") or "").lower()
            labels = [l.lower() for l in (grp.get("labels") or [])]
            names = [n.lower() for n in (grp.get("names") or [])]
            cols = names or labels
            for ath in grp.get("athletes", []) or []:
                person = ath.get("athlete", {}) or {}
                pname = person.get("displayName")
                stats = ath.get("stats") or []
                if not pname or not stats:
                    continue
                for stat_key, label in _PROP_STATS[sport]:
                    # NFL: only read a yard type from its matching stat group
                    if sport == "nfl":
                        want_group = _NFL_GROUP.get(stat_key, "")
                        if want_group and want_group not in gname:
                            continue
                    col = _find_col(cols, stat_key, sport)
                    if col is None or col >= len(stats):
                        continue
                    try:
                        rate = float(str(stats[col]).replace(",", ""))
                    except (ValueError, TypeError):
                        continue
                    if rate <= 0:
                        continue
                    dedup = (pname, stat_key)
                    if dedup in seen:
                        continue
                    seen.add(dedup)
                    line = default_line(stat_key, rate)
                    proj = project_prop(stat_key, rate, line)
                    if not proj:
                        continue
                    proj["player"] = pname
                    proj["team"] = tabbr
                    proj["label"] = label
                    out.append(proj)
    out.sort(key=lambda p: -p["edge"])
    return {"game_id": game_id, "props": out}


# Which boxscore column holds each prop stat. ESPN labels vary by sport/group;
# we match the column header against these candidates (lowercased).
_COL_CANDIDATES = {
    "points": ["pts"], "rebounds": ["reb"], "assists": ["ast"],
    "passingYards": ["yds"], "rushingYards": ["yds"], "receivingYards": ["yds"],
}
# For NFL, the stat group name disambiguates which "yds" we want.
_NFL_GROUP = {"passingYards": "passing", "rushingYards": "rushing",
              "receivingYards": "receiving"}


def _find_col(cols, stat_key, sport):
    for cand in _COL_CANDIDATES.get(stat_key, []):
        if cand in cols:
            return cols.index(cand)
    return None


# ---- prop game logs (NBA / NFL history charts) --------------------------

GAMELOG = {
    "nba": "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{pid}/gamelog",
    "nfl": "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{pid}/gamelog",
}
# Map our stat key -> the label ESPN uses in gamelog stat arrays (varies; we
# match case-insensitively against the labels list).
_LOG_LABEL = {
    "points": "pts", "rebounds": "reb", "assists": "ast",
    "passing_yards": "yds", "rushing_yards": "yds", "receiving_yards": "yds",
}


def _athlete_id_by_name(sport, name):
    """Find an ESPN athlete id from a display name via the search endpoint."""
    try:
        data = _get("https://site.web.api.espn.com/apis/search/v2", {"query": name, "limit": 5})
        for grp in data.get("results", []):
            for item in grp.get("contents", []):
                if (item.get("type") == "player" and
                        sport.upper() in (item.get("subtitle", "") or "").upper()):
                    uid = item.get("uid", "")
                    # uid like "s:40~l:46~a:3917376" -> athlete id after a:
                    if "a:" in uid:
                        return uid.split("a:")[-1]
    except Exception:
        pass
    return None


def get_prop_history(sport, date, game_id, player_name, stat, line):
    """Last-10 game log for a player+stat with hit/miss vs the line."""
    pid = _athlete_id_by_name(sport, player_name)
    if not pid:
        return {"error": "player not found", "history": []}
    try:
        data = _get(GAMELOG[sport].format(pid=pid))
    except Exception:
        return {"error": "no log", "history": []}
    # ESPN gamelog: seasonTypes -> categories -> events; labels define columns
    labels = [l.lower() for l in (data.get("labels") or [])]
    names = [n.lower() for n in (data.get("names") or [])]
    col = None
    want = _LOG_LABEL.get(stat, "")
    for i, lab in enumerate(names or labels):
        if lab == want:
            col = i
            break
    games = []
    events = data.get("events") or {}
    seasontypes = data.get("seasonTypes") or []
    rows = []
    for stp in seasontypes:
        for cat in stp.get("categories", []):
            rows.extend(cat.get("events", []))
    for ev in rows[-10:]:
        stats = ev.get("stats", [])
        if col is None or col >= len(stats):
            continue
        try:
            val = float(str(stats[col]).replace(",", ""))
        except (ValueError, TypeError):
            continue
        meta = events.get(ev.get("eventId"), {}) if isinstance(events, dict) else {}
        opp = ""
        games.append({"date": "", "opp": opp, "value": val})
    hits = sum(1 for x in games if x["value"] > line)
    return {"player": player_name, "label": stat, "line": line,
            "games": games, "hits": hits, "total": len(games)}
