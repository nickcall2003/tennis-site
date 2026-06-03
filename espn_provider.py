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


_ATHLETE_STATS = {
    "nba": "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team}/athletes/statistics",
    "nfl": "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team}/athletes/statistics",
}
# ESPN's per-athlete statistics group stats by category with display names.
# We match the season-average stat we want by its ESPN stat "name".
_AVG_STAT_NAME = {
    "points": ["avgPoints"], "rebounds": ["avgRebounds", "avgTotalRebounds"],
    "assists": ["avgAssists"],
    "passingYards": ["passingYardsPerGame", "avgPassingYards"],
    "rushingYards": ["rushingYardsPerGame", "avgRushingYards"],
    "receivingYards": ["receivingYardsPerGame", "avgReceivingYards"],
}

_props_cache = {}   # (sport, game_id) -> (ts, props)


def _team_athlete_avgs(sport, team_id):
    """Return {athlete_name: {stat_key: season_avg}} for a team."""
    out = {}
    if not team_id:
        return out
    try:
        data = _get(_ATHLETE_STATS[sport].format(team=team_id), {"region": "us"})
    except Exception:
        return out
    # structure: athletes[] -> {athlete:{displayName}, categories[]:{name, stats[]:{name,value}}}
    athletes = data.get("athletes") or []
    wanted = {name: key for key, names in _AVG_STAT_NAME.items() for name in names}
    for entry in athletes:
        ath = entry.get("athlete", {}) or {}
        nm = ath.get("displayName")
        if not nm:
            continue
        vals = {}
        for cat in entry.get("categories", []) or []:
            for st in cat.get("stats", []) or []:
                sname = st.get("name", "")
                if sname in wanted:
                    try:
                        vals[wanted[sname]] = float(st.get("value"))
                    except (ValueError, TypeError):
                        pass
        if vals:
            out[nm] = vals
    return out


def get_props(sport: str, date: dt.date, game_id: str):
    """
    Full-roster player props using each team's real season averages.
    Two calls per game (one per team's athlete statistics), cached.
    """
    import time as _t
    from props import project_prop, default_line
    ck = (sport, str(game_id))
    c = _props_cache.get(ck)
    if c and _t.time() - c[0] < _DAY_TTL:
        return {"game_id": game_id, "props": c[1]}

    g = get_game(sport, date, game_id)
    if not g:
        return {"game_id": game_id, "props": []}
    out = []
    for side in ("home", "away"):
        team_id = g[side].get("team_id")
        tabbr = g[side].get("abbr", "")
        avgs = _team_athlete_avgs(sport, team_id)
        for pname, vals in avgs.items():
            for stat_key, label in _PROP_STATS[sport]:
                rate = vals.get(stat_key)
                if not rate or rate <= 0:
                    continue
                # skip implausible/no-volume lines (e.g. a center with 0.3 assists)
                line = default_line(stat_key, rate)
                if line is None or line <= 0:
                    continue
                proj = project_prop(stat_key, rate, line)
                if not proj:
                    continue
                proj["player"] = pname
                proj["team"] = tabbr
                proj["label"] = label
                out.append(proj)
    out.sort(key=lambda p: -p["edge"])
    _props_cache[ck] = (_t.time(), out)
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
    # accept both the camelCase prop keys and underscore variants
    "passingYards": "yds", "rushingYards": "yds", "receivingYards": "yds",
    "passing_yards": "yds", "rushing_yards": "yds", "receiving_yards": "yds",
}
# For NFL gamelogs, the right "yds" lives in a category matching this word.
_LOG_NFL_CAT = {
    "passingYards": "passing", "rushingYards": "rushing", "receivingYards": "receiving",
    "passing_yards": "passing", "rushing_yards": "rushing", "receiving_yards": "receiving",
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
    nfl_cat = _LOG_NFL_CAT.get(stat)
    for stp in seasontypes:
        for cat in stp.get("categories", []):
            # For NFL yards, only read events from the matching category
            if nfl_cat and nfl_cat not in (cat.get("name", "") or "").lower():
                continue
            rows.extend(cat.get("events", []))
    for ev in rows[-10:]:
        stats = ev.get("stats", [])
        if col is None or col >= len(stats):
            continue
        try:
            val = float(str(stats[col]).replace(",", ""))
        except (ValueError, TypeError):
            continue
        ev_meta = events.get(ev.get("eventId"), {}) if isinstance(events, dict) else {}
        opp = ""
        try:
            opp = (ev_meta.get("opponent", {}) or {}).get("abbreviation", "") or ""
        except Exception:
            opp = ""
        games.append({"date": "", "opp": opp, "value": val})
    hits = sum(1 for x in games if x["value"] > line)
    return {"player": player_name, "label": stat, "line": line,
            "games": games, "hits": hits, "total": len(games)}


# ---- news & injuries (for the News/Injury tab) -------------------------
# Two real, free ESPN feeds:
#   News:     site.api.espn.com/apis/site/v2/sports/{sport}/{league}/news
#   Injuries: sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/teams/{id}/injuries
# Trades / free agency / transfer portal are NOT available as a structured free
# feed; ESPN's news headlines naturally surface many of those stories, which is
# the honest closest version.

_SPORT_LEAGUE = {
    "nfl": ("football", "nfl"), "nba": ("basketball", "nba"),
    "mlb": ("baseball", "mlb"),
}
_news_cache = {}      # league -> (ts, items)
_inj_cache = {}       # league -> (ts, items)
_NEWS_TTL = 1800      # 30 min
_INJ_TTL = 1800


def get_news(league: str, limit: int = 25):
    import time as _t
    if league not in _SPORT_LEAGUE:
        return []
    c = _news_cache.get(league)
    if c and _t.time() - c[0] < _NEWS_TTL:
        return c[1]
    sport, lg = _SPORT_LEAGUE[league]
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{lg}/news"
    try:
        data = _get(url, {"limit": limit})
    except Exception:
        return []
    items = []
    for art in data.get("articles", []) or []:
        cats = art.get("categories", []) or []
        # try to surface a player/team the story is about
        tag = ""
        for c2 in cats:
            if c2.get("type") == "athlete" and c2.get("description"):
                tag = c2["description"]
                break
        items.append({
            "headline": art.get("headline", ""),
            "description": art.get("description", ""),
            "published": (art.get("published", "") or "")[:10],
            "tag": tag,
            "type": (art.get("type", "") or "Story").title(),
        })
    _news_cache[league] = (_t.time(), items)
    return items


def get_injuries(league: str, date=None):
    """Injury report for teams playing on `date` (relevant + light). Grouped by team."""
    import time as _t
    import datetime as _dt
    if league not in _SPORT_LEAGUE:
        return []
    date = date or _dt.date.today()
    ckey = (league, date.isoformat())
    c = _inj_cache.get(ckey)
    if c and _t.time() - c[0] < _INJ_TTL:
        return c[1]
    sport, lg = _SPORT_LEAGUE[league]
    # only the teams in today's games (far fewer calls than all 30)
    games = get_games(league, date)
    team_ids = []
    for g in games:
        for side in ("home", "away"):
            tid = g[side].get("team_id")
            if tid and tid not in team_ids:
                team_ids.append(tid)
    out = []
    for tid in team_ids:
        try:
            inj = _get(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{lg}/teams/{tid}",
                       {"enable": "injuries"})
            team_obj = inj.get("team", {}) or {}
            tname = team_obj.get("displayName", "")
            injuries = team_obj.get("injuries", []) or []
        except Exception:
            continue
        players = []
        for it in injuries:
            ath = it.get("athlete", {}) or {}
            players.append({
                "player": ath.get("displayName", "Player"),
                "position": (ath.get("position", {}) or {}).get("abbreviation", ""),
                "status": it.get("status", "") or (it.get("type", {}) or {}).get("description", ""),
                "detail": (it.get("details", {}) or {}).get("type", "")
                          or it.get("shortComment", "") or it.get("longComment", ""),
            })
        if players:
            out.append({"team": tname, "players": players})
    _inj_cache[ckey] = (_t.time(), out)
    return out
