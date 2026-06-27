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
    "ncaaf": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard",
    "ncaab": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard",
    "wncaab": "https://site.api.espn.com/apis/site/v2/sports/basketball/womens-college-basketball/scoreboard",
}
SUMMARY = {
    "nba": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary",
    "nfl": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary",
    "ncaaf": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary",
    "ncaab": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/summary",
    "wncaab": "https://site.api.espn.com/apis/site/v2/sports/basketball/womens-college-basketball/summary",
}

_cache = {}          # (sport,date) -> (ts, [games])
_DAY_TTL = 6 * 3600
_LIVE_TTL = 8


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
    name = (st.get("name") or "").upper()
    completed = bool(st.get("completed"))
    # Postponed / canceled / forfeited games are NOT results. ESPN parks them in
    # state 'post' with completed=false, which the old code read as 'finished' and
    # then (0-0 scores) graded as a loss. Treat them as their own status so they
    # never settle a pick.
    if any(k in name for k in ("POSTPON", "CANCEL", "FORFEIT", "ABANDON")):
        return "postponed"
    if state == "post":
        return "finished" if (completed or name == "STATUS_FINAL") else "postponed"
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
        "location": t.get("location", ""), "short": t.get("shortDisplayName", ""),
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
    is_current = date >= dt.date.today()
    ttl = _LIVE_TTL if force_live else (45 if is_current else _DAY_TTL)
    if c and time.time() - c[0] < ttl:
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
        try:
            # When we've built a team-strength Elo from ESPN results, feed it to
            # team_model so the base prediction reflects real strength, not just
            # win% seeding. Absent the file, hr/ar are None -> unchanged behavior.
            hr = ar = None
            try:
                import espn_elo
                hr = espn_elo.lookup(sport, h.get("team_id"))
                ar = espn_elo.lookup(sport, a.get("team_id"))
            except Exception:
                pass
            pred = predict(sport, h["win_pct"], a["win_pct"], hr, ar)
        except Exception:
            # model doesn't know this sport yet -> neutral win%-based fallback
            wph = h["win_pct"] if h["win_pct"] is not None else 0.5
            wpa = a["win_pct"] if a["win_pct"] is not None else 0.5
            ph = max(0.05, min(0.95, 0.5 + (wph - wpa) * 0.5 + 0.03))
            pred = {"prob_home": round(ph, 4), "exp_margin": None,
                    "confidence": "low", "avg_total": None}
        if sport == "ncaaf":
            try:
                import ncaaf_provider
                _sp = ncaaf_provider.predict(
                    h.get("location") or h["name"], a.get("location") or a["name"])
                if _sp:
                    pred = _sp
            except Exception:
                pass
        if sport == "ncaab":
            try:
                import ncaab_provider
                _adj = ncaab_provider.predict(
                    h.get("location") or h["name"], a.get("location") or a["name"])
                if _adj:
                    pred = _adj
            except Exception:
                pass
        if sport == "nba":
            try:
                import nba_provider
                _eff = nba_provider.predict(h["name"], a["name"])  # full team names
                if _eff:
                    pred = _eff
            except Exception:
                pass
        if sport == "nfl":
            try:
                import nfl_provider
                _epa = nfl_provider.predict(h.get("abbr"), a.get("abbr"))  # abbreviations
                if _epa:
                    pred = _epa
            except Exception:
                pass
        # --- injuries: weight each OUT/limited player by value, sum per team,
        # and nudge the final probability (applies on top of whichever model won)
        try:
            import injuries as _inj
            if _inj.enabled(sport):
                ih = _inj.for_team(sport, h.get("team_id"))
                ia = _inj.for_team(sport, a.get("team_id"))
                net = (ia.get("penalty", 0.0) - ih.get("penalty", 0.0))  # + favors home
                if net and pred.get("prob_home") is not None:
                    import math as _m
                    p = min(0.999, max(0.001, pred["prob_home"]))
                    e = -400.0 * _m.log10(1.0 / p - 1.0)
                    pred["prob_home"] = round(1.0 / (1.0 + 10 ** (-((e + net) / 400.0))), 4)
                    pred["injuries"] = {"home": ih.get("players", []), "away": ia.get("players", []),
                                        "home_pts": ih.get("penalty", 0.0),
                                        "away_pts": ia.get("penalty", 0.0)}
        except Exception:
            pass
        # --- rest / schedule spot: nudge toward the more-rested team
        try:
            import schedule as _sch
            if _sch.enabled(sport):
                newp, rinfo = _sch.adjust(sport, pred.get("prob_home"),
                                          h.get("team_id"), a.get("team_id"), date)
                if rinfo:
                    pred["prob_home"] = newp
                    pred["rest"] = rinfo
        except Exception:
            pass
        status = _status(comp)
        venue = (comp.get("venue", {}) or {}).get("fullName", "")
        # prominence: combined win% (better teams = bigger game)
        prominence = (h["win_pct"] or 0.5) + (a["win_pct"] or 0.5)
        st = ((comp.get("status") or {}).get("type") or {})
        status_obj = (comp.get("status") or {})
        # football situation block (NFL/NCAAF only have this; everything else -> None).
        # All defensive .get so off-season / non-live responses yield an empty panel.
        _sit = comp.get("situation") or {}
        _home_id = str(home.get("id") or (home.get("team") or {}).get("id") or "")
        _away_id = str(away.get("id") or (away.get("team") or {}).get("id") or "")
        _poss_id = str(_sit.get("possession") or "")
        _poss = ("home" if (_poss_id and _poss_id == _home_id)
                 else "away" if (_poss_id and _poss_id == _away_id) else None)
        situation = ({
            "down": _sit.get("down"),
            "distance": _sit.get("distance"),
            "yard_line": _sit.get("yardLine"),
            "possession": _poss,
            "is_red_zone": bool(_sit.get("isRedZone")),
            "home_timeouts": _sit.get("homeTimeouts"),
            "away_timeouts": _sit.get("awayTimeouts"),
            "down_distance_short": _sit.get("shortDownDistanceText"),
            "down_distance_text": _sit.get("downDistanceText"),
            "possession_text": _sit.get("possessionText"),
        } if _sit else None)
        games.append({
            "id": ev.get("id"), "sport": sport, "status": status,
            "event_time": _ct_time(ev.get("date", "")),
            "home": h, "away": a,
            "prob_home": pred["prob_home"], "exp_margin": pred["exp_margin"],
            "confidence": pred["confidence"], "avg_total": pred["avg_total"],
            "home_rating": pred.get("home_rating"), "away_rating": pred.get("away_rating"),
            "factors": pred.get("factors"), "situation": situation,
            "injuries": pred.get("injuries"),
            "rest": pred.get("rest"),
            "venue": venue, "prominence": prominence,
            "score": {"home": h["score"], "away": a["score"],
                      "detail": st.get("shortDetail", ""),
                      "clock": status_obj.get("displayClock"),
                      "period": status_obj.get("period")},
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
    if sport not in _PROP_STATS:
        return {"game_id": game_id, "props": []}
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
    "threes": "3pt", "three pointers": "3pt", "three pointers made": "3pt",
    "3-pt made": "3pt", "3pt made": "3pt", "3 pt made": "3pt",
    "threepointersmade": "3pt", "three pointers": "3pt",
    "steals": "stl", "blocks": "blk", "turnovers": "to",
    # NFL (category-scoped below); accept camelCase, underscore, and spaced labels
    "passingyards": "yds", "rushingyards": "yds", "receivingyards": "yds",
    "passing yards": "yds", "rushing yards": "yds", "receiving yards": "yds",
    "passing_yards": "yds", "rushing_yards": "yds", "receiving_yards": "yds",
    "passingYards": "yds", "rushingYards": "yds", "receivingYards": "yds",
    "passing touchdowns": "td", "rushing touchdowns": "td", "receiving touchdowns": "td",
    "passing tds": "td", "rushing tds": "td", "receiving tds": "td",
    "interceptions": "int", "completions": "cmp",
    "receptions": "rec", "rushing attempts": "car", "carries": "car",
}
# For NFL gamelogs, the right column lives in a category matching this word.
_LOG_NFL_CAT = {
    "passingyards": "passing", "rushingyards": "rushing", "receivingyards": "receiving",
    "passing yards": "passing", "rushing yards": "rushing", "receiving yards": "receiving",
    "passing_yards": "passing", "rushing_yards": "rushing", "receiving_yards": "receiving",
    "passingYards": "passing", "rushingYards": "rushing", "receivingYards": "receiving",
    "passing touchdowns": "passing", "rushing touchdowns": "rushing",
    "receiving touchdowns": "receiving", "passing tds": "passing",
    "rushing tds": "rushing", "receiving tds": "receiving",
    "interceptions": "passing", "completions": "passing",
    "receptions": "receiving", "rushing attempts": "rushing", "carries": "rushing",
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
    if sport not in GAMELOG:
        return {"error": "no log", "history": []}
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
    want = _LOG_LABEL.get((stat or "").lower(), "")
    for i, lab in enumerate(names or labels):
        if lab == want:
            col = i
            break
    games = []
    events = data.get("events") or {}
    seasontypes = data.get("seasonTypes") or []
    rows = []
    nfl_cat = _LOG_NFL_CAT.get((stat or "").lower())
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
    "ncaaf": ("football", "college-football"),
    "ncaab": ("basketball", "mens-college-basketball"),
    "wncaab": ("basketball", "womens-college-basketball"),
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


# ---- live box score (per-game player stats) -------------------------------
_box_cache = {}   # (sport, game_id) -> (ts, parsed)

def _parse_espn_boxscore(data):
    """Uniform box score from an ESPN summary payload: a list of teams, each
    with stat groups (column labels + athlete rows). Works across NBA / NFL /
    college / college-baseball because ESPN supplies its own column labels per
    group. Fully defensive: missing pieces just yield fewer rows/groups."""
    bs = (data or {}).get("boxscore") or {}
    teams_out = []
    for tp in bs.get("players") or []:
        team = tp.get("team") or {}
        tname = (team.get("displayName") or team.get("shortDisplayName")
                 or team.get("abbreviation") or "Team")
        groups = []
        for grp in tp.get("statistics") or []:
            cols = grp.get("labels") or grp.get("names") or []
            rows = []
            for ath in grp.get("athletes") or []:
                a = ath.get("athlete") or {}
                rows.append({
                    "name": a.get("displayName") or a.get("shortName") or "\u2014",
                    "pos": ((a.get("position") or {}) or {}).get("abbreviation") or "",
                    "stats": ath.get("stats") or [],
                })
            if rows:
                title = (grp.get("text") or grp.get("name") or "").strip()
                groups.append({"title": title.title() if title else "Stats",
                               "columns": cols, "rows": rows})
        if groups:
            teams_out.append({"name": tname,
                              "abbr": team.get("abbreviation") or "",
                              "groups": groups})
    return {"teams": teams_out}

def get_boxscore(sport, date, game_id):
    """Live player box score for one ESPN game (cached ~15s)."""
    import time as _t
    url = SUMMARY.get(sport)
    if not url:
        return {"teams": []}
    ck = (sport, str(game_id))
    c = _box_cache.get(ck)
    if c and _t.time() - c[0] < 15:
        return c[1]
    try:
        data = _get(url, {"event": str(game_id)})
    except Exception as e:
        print(f"[{sport}] boxscore fetch failed: {e}")
        return {"teams": []}
    out = _parse_espn_boxscore(data)
    _box_cache[ck] = (_t.time(), out)
    return out
