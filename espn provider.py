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
    "wnba": "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard",
}
SUMMARY = {
    "nba": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary",
    "nfl": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary",
    "ncaaf": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary",
    "ncaab": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/summary",
    "wncaab": "https://site.api.espn.com/apis/site/v2/sports/basketball/womens-college-basketball/summary",
    "wnba": "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary",
}

_cache = {}          # (sport,date) -> (ts, [games])
_DAY_TTL = 6 * 3600
_LIVE_TTL = 8


def _get(url, params=None):
    import httpx
    r = httpx.get(url, params=params or {}, timeout=20.0)
    r.raise_for_status()
    return r.json()


# ---- Head-to-head season series --------------------------------------------
# High-repeat sports (NBA) show THIS season's series; low-repeat sports (NFL and
# college, where two teams rarely meet twice in a year) show the last 3 seasons.
# Derived from each team's ESPN schedule, cached per team+season. A per-build
# FETCH BUDGET caps new network calls so a huge college board can never stall the
# slate — games past the budget simply get H2H on a later build as caches warm.
_TEAM_SCHED: dict = {}     # (sport, team_id, season) -> [{"opp":id,"won":bool}, ...]
_H2H_BUDGET = [0]          # remaining NEW schedule fetches allowed this build


def _h2h_lookback(sport):
    return 1 if sport in ("nba", "wnba") else 3


def _team_schedule(sport, team_id, season):
    key = (sport, str(team_id), season)
    if key in _TEAM_SCHED:
        return _TEAM_SCHED[key]
    if _H2H_BUDGET[0] <= 0:
        return []                       # out of budget; not cached, retried next build
    _H2H_BUDGET[0] -= 1
    out = []
    try:
        base = SCOREBOARD[sport].rsplit("/scoreboard", 1)[0]
        params = {} if season is None else {"season": season}
        data = _get(f"{base}/teams/{team_id}/schedule", params)
        for ev in data.get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            cs = comp.get("competitors", [])
            me = next((c for c in cs if str((c.get("team") or {}).get("id")) == str(team_id)), None)
            opp = next((c for c in cs if str((c.get("team") or {}).get("id")) != str(team_id)), None)
            if not me or not opp:
                continue
            if not (((comp.get("status") or {}).get("type") or {}).get("completed")):
                continue
            out.append({"opp": str((opp.get("team") or {}).get("id")),
                        "won": me.get("winner") is True})
    except Exception:
        out = []
    _TEAM_SCHED[key] = out
    return out


def _season_h2h(sport, home_id, away_id):
    if not home_id or not away_id:
        return None
    try:
        n = _h2h_lookback(sport)
        yr = dt.date.today().year
        years = [None] if n == 1 else [yr - 2, yr - 1, yr]   # None = current season
        games = []
        for s in years:
            for g in _team_schedule(sport, home_id, s):
                if g["opp"] == str(away_id):
                    games.append(g)
        if not games:
            return None
        w = sum(1 for x in games if x["won"])
        return {"w": w, "l": len(games) - w, "record": f"{w}-{len(games)-w}",
                "games": len(games), "seasons": n}
    except Exception:
        return None


def _score_of(comp_side):
    s = comp_side.get("score")
    if isinstance(s, dict):
        s = s.get("value", s.get("displayValue"))
    try:
        return int(float(s))
    except Exception:
        return None


def team_profile(sport, team_id, name=None):
    """Honest team profile from the team's ESPN schedule + Elo rating. Only real,
    computed fields (record, recent form, home/away splits, points for/against,
    current streak, power rating) — no fabricated pace/ATS/chemistry."""
    if sport not in SCOREBOARD:
        return {"error": "unsupported sport"}
    out = {"sport": sport, "team_id": str(team_id), "name": name}
    try:
        base = SCOREBOARD[sport].rsplit("/scoreboard", 1)[0]
        data = _get(f"{base}/teams/{team_id}/schedule", {})
    except Exception:
        return out
    games = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        cs = comp.get("competitors", [])
        me = next((c for c in cs if str((c.get("team") or {}).get("id")) == str(team_id)), None)
        opp = next((c for c in cs if str((c.get("team") or {}).get("id")) != str(team_id)), None)
        if not me or not opp:
            continue
        if not (((comp.get("status") or {}).get("type") or {}).get("completed")):
            continue
        tm = me.get("team") or {}
        if not out.get("name"):
            out["name"] = tm.get("displayName") or tm.get("shortDisplayName")
        out.setdefault("abbr", tm.get("abbreviation"))
        out.setdefault("logo", tm.get("logo") or tm.get("logos", [{}])[0].get("href") if tm.get("logos") else tm.get("logo"))
        games.append({"won": me.get("winner") is True,
                      "home": me.get("homeAway") == "home",
                      "opp": (opp.get("team") or {}).get("abbreviation") or (opp.get("team") or {}).get("displayName"),
                      "ms": _score_of(me), "os": _score_of(opp),
                      "date": (ev.get("date", "") or "")[:10]})
    games.sort(key=lambda x: x["date"])
    n = len(games)
    if not n:
        return out
    w = sum(1 for g in games if g["won"])
    hw = sum(1 for g in games if g["home"] and g["won"]); hg = sum(1 for g in games if g["home"])
    aw = sum(1 for g in games if not g["home"] and g["won"]); ag = sum(1 for g in games if not g["home"])
    last10 = games[-10:]
    pf = [g["ms"] for g in games if g["ms"] is not None]
    pa = [g["os"] for g in games if g["os"] is not None]
    # current streak from most recent backwards
    streak = 0; last_res = games[-1]["won"]
    for g in reversed(games):
        if g["won"] == last_res:
            streak += 1
        else:
            break
    try:
        import espn_elo
        rating = espn_elo.lookup(sport, team_id)
    except Exception:
        rating = None
    out.update({
        "rating": round(rating) if isinstance(rating, (int, float)) else None,
        "record": f"{w}-{n-w}", "w": w, "l": n - w, "games": n,
        "home_record": f"{hw}-{hg-hw}", "away_record": f"{aw}-{ag-aw}",
        "last10": f"{sum(1 for g in last10 if g['won'])}-{len(last10)-sum(1 for g in last10 if g['won'])}",
        "form": "".join("W" if g["won"] else "L" for g in last10),
        "streak": (("W" if last_res else "L") + str(streak)) if streak else None,
        "ppg": round(sum(pf) / len(pf), 1) if pf else None,
        "opp_ppg": round(sum(pa) / len(pa), 1) if pa else None,
        "recent": [{"opp": g["opp"], "won": g["won"], "home": g["home"],
                    "score": (f"{g['ms']}-{g['os']}" if g["ms"] is not None else None),
                    "date": g["date"]} for g in reversed(games[-8:])],
    })
    return out


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
    _H2H_BUDGET[0] = 24        # cap NEW schedule fetches per build so big boards can't stall
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
            "h2h": _season_h2h(sport, h.get("team_id"), a.get("team_id")),
            "prob_home": pred["prob_home"], "exp_margin": pred["exp_margin"],
            "confidence": pred["confidence"], "avg_total": pred["avg_total"],
            "home_rating": pred.get("home_rating"), "away_rating": pred.get("away_rating"),
            "home_rating_base": pred.get("home_rating_base"), "home_edge_pts": pred.get("home_edge_pts"),
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
    "wnba": "https://site.web.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team}/athletes/statistics",
    "nfl": "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team}/athletes/statistics",
}
# ESPN's per-athlete statistics group stats by category with display names.
# We match the season-average stat we want by its ESPN stat "name".
_AVG_STAT_NAME = {
    "points": ["avgPoints"], "rebounds": ["avgRebounds", "avgTotalRebounds"],
    "assists": ["avgAssists"],
    "threes": ["avgThreePointFieldGoalsMade", "avg3PointFieldGoalsMade", "avgThreePointersMade"],
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

    # --- opponent-defense + pace context (real ESPN season data) --------------
    # Sharpen each projection: a player facing a soft defense / fast pace gets a
    # bump; facing a stingy defense / slow pace gets trimmed. Bounded so a single
    # factor can't run away, and only scoring props take the defensive factor
    # (we don't have per-stat defensive splits, so rebounds/assists lean on pace).
    _LEAGUE_PPG = {"nba": 114.0, "wnba": 82.0, "ncaab": 72.0, "wncaab": 65.0}.get(sport, 100.0)

    def _clamp(x, lo, hi):
        return lo if x < lo else (hi if x > hi else x)

    def _ctx(tid):
        try:
            tp = team_profile(sport, tid) or {}
            return tp.get("opp_ppg"), tp.get("ppg")   # (points allowed, points scored)
        except Exception:
            return None, None

    _def = {}
    _off = {}
    for _s in ("home", "away"):
        _def[_s], _off[_s] = _ctx(g[_s].get("team_id"))

    out = []
    for side in ("home", "away"):
        team_id = g[side].get("team_id")
        tabbr = g[side].get("abbr", "")
        opp = "away" if side == "home" else "home"
        opp_abbr = g[opp].get("abbr", "")
        opp_def = _def.get(opp)                 # points the OPPONENT allows
        team_off, opp_off = _off.get(side), _off.get(opp)
        def_factor = _clamp((opp_def / _LEAGUE_PPG) if opp_def else 1.0, 0.88, 1.12)
        pace_factor = _clamp((((team_off or _LEAGUE_PPG) + (opp_off or _LEAGUE_PPG))
                              / (2 * _LEAGUE_PPG)) if (team_off or opp_off) else 1.0, 0.90, 1.10)
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
                scoring = stat_key in ("points", "threes")
                adj_rate = rate * pace_factor * (def_factor if scoring else 1.0)
                proj = project_prop(stat_key, adj_rate, line)
                if not proj:
                    continue
                proj["player"] = pname
                proj["team"] = tabbr
                proj["label"] = label
                proj["season_avg"] = round(rate, 1)
                proj["factors"] = {
                    "opponent": opp_abbr,
                    "opp_allows_ppg": round(opp_def, 1) if opp_def else None,
                    "def_adj_pct": round((def_factor - 1) * 100, 1) if scoring else 0.0,
                    "pace_adj_pct": round((pace_factor - 1) * 100, 1),
                }
                _da = proj["factors"]["def_adj_pct"]
                _pa = proj["factors"]["pace_adj_pct"]
                bits = [f"season avg {round(rate,1)}"]
                if opp_def:
                    bits.append(f"vs {opp_abbr} D (allows {round(opp_def,1)} ppg{', ' + ('%+.0f' % _da) + '%' if scoring and _da else ''})")
                if abs(_pa) >= 0.5:
                    bits.append(f"pace {'%+.0f' % _pa}%")
                proj["context"] = " \u00b7 ".join(bits)
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
    "wnba": "https://site.web.api.espn.com/apis/common/v3/sports/basketball/wnba/athletes/{pid}/gamelog",
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
    """Find an ESPN athlete id from a display name via the search endpoint.
    WNBA subtitles frequently omit the league, so we also match on ESPN's league
    code in the uid and, for WNBA, fall back to the top player hit (names unique)."""
    try:
        data = _get("https://site.web.api.espn.com/apis/search/v2", {"query": name, "limit": 8})
        league_code = {"nba": "46", "wnba": "59", "nfl": "28",
                       "ncaab": "41", "wncaab": "54"}.get(sport)
        want = sport.upper()
        fallback = None
        for grp in data.get("results", []):
            for item in grp.get("contents", []):
                if item.get("type") != "player":
                    continue
                uid = item.get("uid", "") or ""
                if "a:" not in uid:
                    continue
                aid = uid.split("a:")[-1]
                sub = (item.get("subtitle", "") or "").upper()
                if want in sub or (league_code and ("l:" + league_code + "~") in uid):
                    return aid
                if fallback is None:
                    fallback = aid
        if sport == "wnba" and fallback:   # subtitle often lacks "WNBA" — take top player hit
            return fallback
    except Exception:
        pass
    return None


_roster_id_cache = {}   # (sport, team_id) -> {name_lower: athlete_id}


def _team_roster_ids(sport, team_id):
    """{display_name_lower: athlete_id} from the team's statistics endpoint \u2014 a
    reliable ID source that doesn't depend on the flaky global search."""
    key = (sport, str(team_id))
    if key in _roster_id_cache:
        return _roster_id_cache[key]
    ids = {}
    try:
        url = _ATHLETE_STATS.get(sport)
        if url and team_id:
            data = _get(url.format(team=team_id), {"region": "us"})
            for entry in (data.get("athletes") or []):
                ath = entry.get("athlete", {}) or {}
                nm = ath.get("displayName")
                aid = ath.get("id")
                if nm and aid:
                    ids[nm.lower()] = str(aid)
    except Exception:
        pass
    _roster_id_cache[key] = ids
    return ids


def _game_roster_ids(sport, date, game_id):
    ids = {}
    try:
        g = get_game(sport, date, game_id)
        if g:
            for side in ("home", "away"):
                ids.update(_team_roster_ids(sport, g[side].get("team_id")))
    except Exception:
        pass
    return ids


def get_prop_history(sport, date, game_id, player_name, stat, line):
    """Last-10 game log for a player+stat with hit/miss vs the line."""
    if sport not in GAMELOG:
        return {"error": "no log", "history": [], "games": []}
    # resolve the athlete id from the game roster first (reliable), then search
    nm = (player_name or "").lower()
    pid = _game_roster_ids(sport, date, game_id).get(nm) or _athlete_id_by_name(sport, player_name)
    if not pid:
        return {"error": "player not found", "history": [], "games": []}
    try:
        data = _get(GAMELOG[sport].format(pid=pid))
    except Exception:
        return {"error": "no log", "history": []}
    # ESPN gamelog: seasonTypes -> categories -> events; labels/names define columns.
    # WNBA populates `names` with full words ("points") while `labels` has the
    # abbreviations ("PTS") that _LOG_LABEL targets \u2014 so check BOTH at each index.
    labels = [l.lower() for l in (data.get("labels") or [])]
    names = [n.lower() for n in (data.get("names") or [])]
    col = None
    want = _LOG_LABEL.get((stat or "").lower(), "")
    if want:
        for i in range(max(len(labels), len(names))):
            lab = labels[i] if i < len(labels) else ""
            nm = names[i] if i < len(names) else ""
            if lab == want or nm == want:
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


_officials_cache = {}
def get_officials(sport, game_id):
    """Referee crew from the ESPN summary (gameInfo.officials). Best-effort:
    ESPN exposes officials inconsistently and often only near or after game
    time. Names only — no tendency data, so the model never weights them."""
    import time as _t
    url = SUMMARY.get(sport)
    if not url:
        return {"sport": sport, "officials": []}
    ck = (sport, str(game_id))
    c = _officials_cache.get(ck)
    if c and _t.time() - c[0] < 1800:
        return c[1]
    out = {"sport": sport, "officials": []}
    try:
        data = _get(url, {"event": str(game_id)})
        gi = data.get("gameInfo") or {}
        for o in (gi.get("officials") or []):
            name = o.get("displayName") or o.get("fullName")
            pos = ((o.get("position") or {}).get("displayName")
                   or (o.get("position") or {}).get("name") or "")
            if name:
                out["officials"].append({"name": name, "position": pos})
    except Exception:
        pass
    _officials_cache[ck] = (_t.time(), out)
    return out
