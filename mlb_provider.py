"""
mlb_provider.py
---------------
Adapter for the free, official MLB Stats API (https://statsapi.mlb.com) — no key.

Pulls a day's games with probable pitchers and live scores, enriches each side
with team offense, starter ERA, bullpen ERA, ballpark factor and weather, then
runs mlb_model.predict_game. Results are cached in memory per date (MLB has
~15 games/day, so this is light) and refreshed for live scores.

Everything that enriches a game is wrapped in try/except: if a stat call fails
the model simply falls back to league average for that input, so a game always
gets a prediction.
"""

from __future__ import annotations

import datetime as dt
import time

import mlb_data as MD
from mlb_model import GameFactors, TeamInput, predict_game

BASE = "https://statsapi.mlb.com/api/v1"
SEASON = dt.date.today().year

_team_cache = {}       # team_id -> (ts, {rpg, bullpen_era})
_pitcher_cache = {}    # pitcher_id -> (ts, {era, ip})
_weather_cache = {}    # (lat,lon) -> (ts, factor)
# Home-plate -> center-field compass azimuth. MLB Rule 1.04 puts the batting
# axis (home->2B->CF) East-Northeast for most parks; Wrigley sits further north.
# The wind-direction run nudge is modest, so the ENE default is a safe baseline.
_PARK_CF_AZ = {"CHC": 30}
_DEFAULT_CF_AZ = 65
def _compass(deg):
    d = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return d[int((deg % 360) / 22.5 + 0.5) % 16]
_games_cache = {}      # date -> (ts, [games])
_DAY_TTL = 6 * 3600
_LIVE_TTL = 25         # seconds for the day's score refresh


def _get(url, params=None):
    import httpx
    r = httpx.get(url, params=params or {}, timeout=20.0)
    r.raise_for_status()
    return r.json()


# ---- enrichment ---------------------------------------------------------

def _ip_to_float(ip):
    """MLB innings notation -> float: '5.2' means 5 and 2/3 innings."""
    try:
        txt = str(ip)
        if "." in txt:
            whole, frac = txt.split(".")
            return int(whole) + int(frac) / 3.0
        return float(ip)
    except (ValueError, TypeError):
        return 0.0


def _team_stats(team_id):
    c = _team_cache.get(team_id)
    if c and time.time() - c[0] < _DAY_TTL:
        return c[1]
    out = {"rpg": None, "bullpen_era": None, "recent_rpg": None, "bullpen_fatigue": None}
    try:
        hit = _get(f"{BASE}/teams/{team_id}/stats",
                   {"stats": "season", "group": "hitting", "season": SEASON})
        stat = hit["stats"][0]["splits"][0]["stat"]
        runs, games = float(stat.get("runs", 0)), float(stat.get("gamesPlayed", 0) or 0)
        if games:
            out["rpg"] = runs / games
    except Exception:
        pass
    try:
        pit = _get(f"{BASE}/teams/{team_id}/stats",
                   {"stats": "season", "group": "pitching", "season": SEASON})
        stat = pit["stats"][0]["splits"][0]["stat"]
        out["bullpen_era"] = float(stat.get("era")) if stat.get("era") else None
    except Exception:
        pass
    # recent form: runs/game over the last ~10 games (gameLog)
    try:
        log = _get(f"{BASE}/teams/{team_id}/stats",
                   {"stats": "gameLog", "group": "hitting", "season": SEASON})
        splits = log["stats"][0]["splits"][-10:]
        rs = [float(s["stat"].get("runs", 0)) for s in splits if s.get("stat")]
        if rs:
            out["recent_rpg"] = sum(rs) / len(rs)
    except Exception:
        pass
    # bullpen fatigue: relief innings over the last 3 games vs a normal load.
    # Estimate bullpen IP = total staff IP - ~5.3 IP/start; normal pen load is
    # ~3.3 IP/game, so a recent stretch of short starts/extra innings -> fatigue.
    try:
        plog = _get(f"{BASE}/teams/{team_id}/stats",
                    {"stats": "gameLog", "group": "pitching", "season": SEASON})
        psplits = plog["stats"][0]["splits"][-3:]
        n = len(psplits)
        if n:
            tot_ip = sum(_ip_to_float((sp.get("stat") or {}).get("inningsPitched"))
                         for sp in psplits)
            bp_ip = max(0.0, tot_ip - 5.3 * n)        # relief innings (estimate)
            normal = 3.3 * n                          # a normal pen load
            out["bullpen_fatigue"] = max(0.0, min(1.0, (bp_ip - normal) / (2.0 * n)))
    except Exception:
        pass
    _team_cache[team_id] = (time.time(), out)
    return out


def _pitcher_stats(pid):
    if not pid:
        return {"era": None, "ip": None, "name": None}
    c = _pitcher_cache.get(pid)
    if c and time.time() - c[0] < _DAY_TTL:
        return c[1]
    out = {"era": None, "ip": None, "name": None, "fip": None}
    try:
        data = _get(f"{BASE}/people/{pid}",
                    {"hydrate": f"stats(group=[pitching],type=[season],season={SEASON})"})
        person = data["people"][0]
        out["name"] = person.get("fullName")
        splits = person["stats"][0]["splits"][0]["stat"]
        out["era"] = float(splits.get("era")) if splits.get("era") not in (None, "-.--") else None
        ip = splits.get("inningsPitched")
        out["ip"] = float(ip) if ip else None
        # FIP: ((13*HR + 3*(BB+HBP) - 2*K) / IP) + constant. ERA-scaled but far
        # more predictive of a pitcher's next start than raw ERA.
        try:
            hr = float(splits.get("homeRuns", 0) or 0)
            bb = float(splits.get("baseOnBalls", 0) or 0)
            hbp = float(splits.get("hitByPitch", 0) or 0)
            k = float(splits.get("strikeOuts", 0) or 0)
            if out["ip"] and out["ip"] >= 10:        # enough sample to be meaningful
                out["fip"] = round((13 * hr + 3 * (bb + hbp) - 2 * k) / out["ip"] + 3.15, 2)
        except Exception:
            pass
    except Exception:
        pass
    _pitcher_cache[pid] = (time.time(), out)
    return out


def _weather_factor(lat, lon, dome, abbr=None):
    if dome or lat is None:
        return 1.0, "Indoor / roof"
    key = (round(lat, 2), round(lon, 2))
    c = _weather_cache.get(key)
    if c and time.time() - c[0] < _DAY_TTL:
        return c[1]
    factor, note = 1.0, None
    try:
        data = _get("https://api.open-meteo.com/v1/forecast",
                    {"latitude": lat, "longitude": lon,
                     "current": "temperature_2m,wind_speed_10m,wind_direction_10m",
                     "temperature_unit": "fahrenheit", "wind_speed_unit": "mph"})
        cur = data.get("current", {})
        temp, wind = cur.get("temperature_2m"), cur.get("wind_speed_10m")
        wdir = cur.get("wind_direction_10m")
        # Warm air carries the ball.
        if temp is not None:
            factor *= 1.0 + max(-0.04, min(0.05, (temp - 70) * 0.0015))
        wind_txt = ""
        if wind is not None:
            wind_txt = f", wind {round(wind)} mph"
            if wdir is not None and wind >= 6:
                import math
                cf_az = _PARK_CF_AZ.get(abbr, _DEFAULT_CF_AZ)
                out_from = (cf_az + 180) % 360            # wind FROM here blows OUT to CF
                align = math.cos(math.radians(wdir - out_from))   # +1 straight out, -1 straight in
                factor *= 1.0 + align * min(wind, 20) / 20.0 * 0.06
                blow = "blowing out" if align > 0.5 else ("blowing in" if align < -0.5 else "crosswind")
                wind_txt = f", wind {round(wind)} mph {_compass(wdir)} ({blow})"
            elif wdir is not None:
                wind_txt = f", wind {round(wind)} mph {_compass(wdir)}"
            if wind > 12:                                 # strong wind adds a little scoring variance
                factor *= 1.01
        note = (f"{round(temp)}\u00b0F" if temp is not None else "") + wind_txt
    except Exception:
        pass
    res = (round(factor, 3), note)
    _weather_cache[key] = (time.time(), res)
    return res


# ---- schedule -----------------------------------------------------------

def _status(game):
    s = game.get("status", {}) or {}
    abs = s.get("abstractGameState", "")
    detailed = (s.get("detailedState") or "").lower()
    # Postponed / cancelled / suspended games carry abstractGameState 'Final' but
    # were never played to completion. The old code read that as 'finished', and
    # with 0-0 scores the winner defaulted to 'away' -> a bogus loss on the record.
    if any(k in detailed for k in ("postpon", "cancel", "suspend", "forfeit")):
        return "postponed"
    if abs == "Final":
        return "finished"
    if abs == "Live":
        return "live"
    return "scheduled"


def _ct_time(iso):
    """Game time (UTC ISO) -> Central-time 'H:MM AM/PM'."""
    try:
        utc = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        ct = utc - dt.timedelta(hours=5)   # America/Chicago (DST; close enough for display)
        h = ct.hour % 12 or 12
        return f"{h}:{ct.minute:02d} {'AM' if ct.hour < 12 else 'PM'} CT"
    except Exception:
        return ""


def get_games(date: dt.date, force_live=False):
    key = date.isoformat()
    c = _games_cache.get(key)
    # Today/future games change status (scheduled->live->final) and scores
    # constantly, so cache them briefly. Past dates are final -> cache long.
    is_current = date >= dt.date.today()
    ttl = _LIVE_TTL if force_live else (45 if is_current else _DAY_TTL)
    if c and time.time() - c[0] < ttl:
        return c[1]

    try:
        sched = _get(f"{BASE}/schedule",
                     {"sportId": 1, "date": key, "gameType": "R",
                      "hydrate": "probablePitcher,linescore,team"})
    except Exception:
        return []
    games = []
    dates = sched.get("dates", [])
    raw_games = dates[0]["games"] if dates else []
    for g in raw_games:
        gid = g.get("gamePk")
        home_t = g["teams"]["home"]; away_t = g["teams"]["away"]
        home_id = home_t["team"]["id"]; away_id = away_t["team"]["id"]
        hmeta = MD.team_meta(home_id) or {}; ameta = MD.team_meta(away_id) or {}
        hp = _pitcher_stats((home_t.get("probablePitcher") or {}).get("id"))
        ap = _pitcher_stats((away_t.get("probablePitcher") or {}).get("id"))
        hs = _team_stats(home_id); as_ = _team_stats(away_id)
        wfactor, wnote = _weather_factor(hmeta.get("lat"), hmeta.get("lon"), hmeta.get("dome", False), hmeta.get("abbr"))

        home = TeamInput(name=home_t["team"].get("name", "Home"), abbr=hmeta.get("abbr", ""),
                         team_id=home_id, runs_per_game=hs["rpg"], recent_rpg=hs.get("recent_rpg"),
                         starter_name=hp["name"] or (home_t.get("probablePitcher") or {}).get("fullName"),
                         starter_era=(hp.get("fip") if hp.get("fip") is not None else hp["era"]), starter_ip=hp["ip"], bullpen_era=hs["bullpen_era"],
                         bullpen_fatigue=hs.get("bullpen_fatigue"),
                         logo=MD.logo_url(home_id))
        away = TeamInput(name=away_t["team"].get("name", "Away"), abbr=ameta.get("abbr", ""),
                         team_id=away_id, runs_per_game=as_["rpg"], recent_rpg=as_.get("recent_rpg"),
                         starter_name=ap["name"] or (away_t.get("probablePitcher") or {}).get("fullName"),
                         starter_era=(ap.get("fip") if ap.get("fip") is not None else ap["era"]), starter_ip=ap["ip"], bullpen_era=as_["bullpen_era"],
                         bullpen_fatigue=as_.get("bullpen_fatigue"),
                         logo=MD.logo_url(away_id))

        gf = GameFactors(park_factor=hmeta.get("park", 1.0), weather_factor=wfactor)
        pred = predict_game(home, away, gf)

        ls = g.get("linescore", {}) or {}
        status = _status(g)
        prominence = (home.runs_per_game or 4.4) + (away.runs_per_game or 4.4)

        games.append({
            "id": gid, "sport": "mlb", "status": status,
            "event_time": _ct_time(g.get("gameDate", "")),
            "home": _side(home, home_t, hp), "away": _side(away, away_t, ap),
            "prob_home": pred["prob_home"], "confidence": pred["confidence"],
            "exp_runs_home": pred["exp_runs_home"], "exp_runs_away": pred["exp_runs_away"],
            "factors": pred["factors"], "park_factor": gf.park_factor,
            "weather": wnote, "venue": (g.get("venue", {}) or {}).get("name", ""),
            "prominence": prominence,
            "score": {
                "home": home_t.get("score"), "away": away_t.get("score"),
                "inning": ls.get("currentInningOrdinal", ""),
                "state": ls.get("inningState", ""),
                # live situation (present only during a live game; all defensive
                # so off-season / pre-game responses just yield an empty panel)
                "situation": {
                    "outs": ls.get("outs"),
                    "balls": ls.get("balls"),
                    "strikes": ls.get("strikes"),
                    "on1": bool((ls.get("offense", {}) or {}).get("first")),
                    "on2": bool((ls.get("offense", {}) or {}).get("second")),
                    "on3": bool((ls.get("offense", {}) or {}).get("third")),
                    "batter": ((ls.get("offense", {}) or {}).get("batter") or {}).get("fullName"),
                    "pitcher": ((ls.get("defense", {}) or {}).get("pitcher") or {}).get("fullName"),
                },
            },
            "winner": ("home" if (status == "finished" and (home_t.get("score") or 0) > (away_t.get("score") or 0))
                       else "away" if status == "finished" else None),
        })
    _games_cache[key] = (time.time(), games)
    return games


def _side(t: TeamInput, raw, pitcher):
    rec = raw.get("leagueRecord", {}) or {}
    return {
        "team_id": t.team_id, "name": t.name, "abbr": t.abbr, "logo": t.logo,
        "record": f"{rec.get('wins','')}-{rec.get('losses','')}" if rec.get("wins") is not None else "",
        "runs_per_game": round(t.runs_per_game, 2) if t.runs_per_game else None,
        "bullpen_era": t.bullpen_era,
        "starter": {"name": t.starter_name, "era": t.starter_era, "ip": t.starter_ip},
    }


def _refresh_scores(date: dt.date, games):
    """Update only the live scores on already-built games (one schedule call)."""
    key = date.isoformat()
    try:
        sched = _get(f"{BASE}/schedule",
                     {"sportId": 1, "date": key, "gameType": "R", "hydrate": "linescore"})
    except Exception:
        return games
    by_id = {}
    for d in sched.get("dates", []):
        for g in d.get("games", []):
            by_id[g.get("gamePk")] = g
    for game in games:
        raw = by_id.get(game["id"])
        if not raw:
            continue
        ht, at = raw["teams"]["home"], raw["teams"]["away"]
        ls = raw.get("linescore", {}) or {}
        status = _status(raw)
        game["status"] = status
        game["score"] = {"home": ht.get("score"), "away": at.get("score"),
                         "inning": ls.get("currentInningOrdinal", ""),
                         "state": ls.get("inningState", "")}
        game["winner"] = ("home" if (status == "finished" and (ht.get("score") or 0) > (at.get("score") or 0))
                          else "away" if status == "finished" else None)
    _games_cache[key] = (time.time(), games)
    return games


def _enrich_situation(g):
    """Pull the full base/out/batter/pitcher state from the dedicated linescore
    endpoint (the schedule hydrate often omits offense/defense). Live games only;
    fully defensive so any miss just leaves the prior situation in place."""
    if not g or g.get("status") != "live":
        return g
    try:
        ls = _get(f"https://statsapi.mlb.com/api/v1/game/{g['id']}/linescore")
        off = ls.get("offense", {}) or {}
        defn = ls.get("defense", {}) or {}
        sit = g.setdefault("score", {}).get("situation") or {}
        sit.update({
            "outs": ls.get("outs", sit.get("outs")),
            "balls": ls.get("balls", sit.get("balls")),
            "strikes": ls.get("strikes", sit.get("strikes")),
            "on1": bool(off.get("first")),
            "on2": bool(off.get("second")),
            "on3": bool(off.get("third")),
            "batter": (off.get("batter") or {}).get("fullName"),
            "pitcher": (defn.get("pitcher") or {}).get("fullName"),
        })
        g["score"]["situation"] = sit
    except Exception as e:
        print(f"[mlb] situation enrich failed: {e}")
    return g


def get_game(date: dt.date, game_id: int):
    # Use the cached, fully-enriched day; refresh just the scores if it's stale.
    key = date.isoformat()
    c = _games_cache.get(key)
    if c:
        games = c[1]
        if time.time() - c[0] >= _LIVE_TTL:
            games = _refresh_scores(date, games)
        for g in games:
            if g["id"] == game_id:
                return _enrich_situation(g)
    for g in get_games(date):
        if g["id"] == game_id:
            return _enrich_situation(g)
    return None


# ---- batter vs pitcher (lazy, detail-page only) -------------------------

def _lineup_or_roster(game_id, team_id, side_key):
    """
    Return a list of batter dicts [{id,name,order}] for a side.
    Prefers the posted lineup from the live boxscore; falls back to the
    team's position-player roster if the lineup isn't out yet.
    """
    # 1) try the posted lineup from the game's boxscore
    try:
        box = _get(f"https://statsapi.mlb.com/api/v1/game/{game_id}/boxscore")
        team = box["teams"][side_key]
        order = team.get("battingOrder") or []
        players = team.get("players", {})
        out = []
        for i, pid in enumerate(order):
            p = players.get(f"ID{pid}") or {}
            person = p.get("person", {})
            out.append({"id": person.get("id", pid),
                        "name": person.get("fullName", "Batter"),
                        "order": i + 1})
        if out:
            return out, True   # True = real posted lineup
    except Exception:
        pass
    # 2) fall back to roster position players (no batting order yet)
    try:
        roster = _get(f"{BASE}/teams/{team_id}/roster", {"rosterType": "active"})
        out = []
        for r in roster.get("roster", []):
            pos = (r.get("position", {}) or {}).get("abbreviation", "")
            if pos in ("P",):       # skip pitchers
                continue
            person = r.get("person", {})
            out.append({"id": person.get("id"), "name": person.get("fullName", "Batter"), "order": None})
        return out[:9], False
    except Exception:
        return [], False


def _bvp(batter_id, pitcher_id):
    """Career batter-vs-pitcher line, or None if no history / on error."""
    if not batter_id or not pitcher_id:
        return None
    try:
        data = _get(f"{BASE}/people/{batter_id}",
                    {"hydrate": f"stats(group=[hitting],type=[vsPlayer],opposingPlayerId={pitcher_id})"})
        person = data["people"][0]
        for blk in person.get("stats", []):
            # the career-total split is the most useful single line
            tkey = (blk.get("type", {}) or {}).get("displayName", "")
            splits = blk.get("splits", [])
            if not splits:
                continue
            # prefer the "vsPlayerTotal" aggregate if present, else first split
            st = splits[-1].get("stat", {}) if "Total" in tkey else splits[0].get("stat", {})
            ab = int(st.get("atBats", 0) or 0)
            if ab <= 0:
                continue
            return {
                "ab": ab, "h": int(st.get("hits", 0) or 0),
                "hr": int(st.get("homeRuns", 0) or 0),
                "rbi": int(st.get("rbi", 0) or 0),
                "bb": int(st.get("baseOnBalls", 0) or 0),
                "so": int(st.get("strikeOuts", 0) or 0),
                "avg": st.get("avg", ""),
            }
    except Exception:
        return None
    return None


def get_matchups(date: dt.date, game_id: int):
    """
    For a game, return each team's batters vs the OPPONENT's starting pitcher.
    Lazy: call only when a detail page is opened.
    """
    g = get_game(date, game_id)
    if not g:
        return {"error": "not found"}
    home_sp = (g["home"]["starter"] or {})
    away_sp = (g["away"]["starter"] or {})
    # need pitcher IDs — fetch from the schedule's probable pitcher ids
    home_pid = _starter_id(date, game_id, "home")
    away_pid = _starter_id(date, game_id, "away")

    def build(side_key, team_id, opp_pitcher_id, opp_pitcher_name):
        batters, posted = _lineup_or_roster(game_id, team_id, side_key)
        rows = []
        for b in batters:
            line = _bvp(b["id"], opp_pitcher_id)
            rows.append({"batter": b["name"], "order": b["order"], "line": line})
        return {"pitcher": opp_pitcher_name, "posted": posted, "batters": rows}

    return {
        "home_team": g["home"]["name"], "away_team": g["away"]["name"],
        # home batters face the AWAY starter, and vice-versa
        "home": build("home", g["home"]["team_id"], away_pid, away_sp.get("name")),
        "away": build("away", g["away"]["team_id"], home_pid, home_sp.get("name")),
    }


def _starter_id(date, game_id, side_key):
    """Look up a probable starter's player id from the cached schedule."""
    try:
        sched = _get(f"{BASE}/schedule",
                     {"sportId": 1, "date": date.isoformat(), "gamePk": game_id,
                      "hydrate": "probablePitcher"})
        for d in sched.get("dates", []):
            for g in d.get("games", []):
                if g.get("gamePk") == game_id:
                    pp = g["teams"][side_key].get("probablePitcher") or {}
                    return pp.get("id")
    except Exception:
        pass
    return None


# ---- injuries (roster status) -------------------------------------------

_injury_cache = {}    # team_id -> (ts, [ {name, position, status, note} ])
_INJ_TTL = 3 * 3600

# Roster status codes/descriptions that mean "not available to play".
_OUT_HINTS = ("injured list", "10-day", "15-day", "60-day", "7-day",
              "disabled", "bereavement", "paternity", "restricted",
              "suspended", "il")


def _is_out(status_desc: str) -> bool:
    s = (status_desc or "").lower()
    return any(h in s for h in _OUT_HINTS)


def get_injuries(team_id):
    """
    Return a team's currently-unavailable players from the roster status.
    [{name, position, status, note}]. Cached a few hours.
    """
    if not team_id:
        return []
    c = _injury_cache.get(team_id)
    if c and time.time() - c[0] < _INJ_TTL:
        return c[1]
    out = []
    try:
        data = _get(f"{BASE}/teams/{team_id}/roster",
                    {"rosterType": "fullRoster",
                     "hydrate": "person(injuries)"})
        for r in data.get("roster", []):
            status = (r.get("status", {}) or {}).get("description", "")
            if not _is_out(status):
                continue
            person = r.get("person", {}) or {}
            pos = (r.get("position", {}) or {}).get("abbreviation", "")
            # injury note, if the hydrate provided one
            note = ""
            injuries = person.get("injuries") or []
            if injuries:
                inj = injuries[0]
                note = inj.get("description") or inj.get("comment") or ""
            out.append({"name": person.get("fullName", "Player"),
                        "position": pos, "status": status, "note": note})
    except Exception:
        pass
    # pitchers and regulars first-ish: sort P last so position players show on top
    out.sort(key=lambda x: (x["position"] == "P", x["name"]))
    _injury_cache[team_id] = (time.time(), out)
    return out


def get_game_injuries(date: dt.date, game_id: int):
    g = get_game(date, game_id)
    if not g:
        return {"error": "not found"}
    return {
        "home_team": g["home"]["name"], "away_team": g["away"]["name"],
        "home": get_injuries(g["home"]["team_id"]),
        "away": get_injuries(g["away"]["team_id"]),
    }


# ---- pitcher strikeout props -------------------------------------------

def _pitcher_k_rate(pid):
    """Season strikeouts-per-start for a pitcher (K / games started)."""
    if not pid:
        return None, None
    try:
        data = _get(f"{BASE}/people/{pid}",
                    {"hydrate": f"stats(group=[pitching],type=[season],season={SEASON})"})
        person = data["people"][0]
        st = person["stats"][0]["splits"][0]["stat"]
        ks = float(st.get("strikeOuts", 0) or 0)
        gs = float(st.get("gamesStarted", 0) or 0)
        name = person.get("fullName")
        if gs >= 1:
            return ks / gs, name
        # reliever or no starts: fall back to K per 9 * ~5.5 IP
        k9 = float(st.get("strikeoutsPer9Inn", 0) or 0)
        if k9:
            return k9 / 9 * 5.5, name
    except Exception:
        pass
    return None, None


def get_props(date: dt.date, game_id: int):
    """Pitcher strikeout props + batter hits/HR/RBI props for a game."""
    from props import project_prop, default_line
    g = get_game(date, game_id)
    if not g:
        return {"error": "not found"}
    home_pid = _starter_id(date, game_id, "home")
    away_pid = _starter_id(date, game_id, "away")
    out = []
    for side, pid, opp in (("home", home_pid, g["away"]["name"]),
                           ("away", away_pid, g["home"]["name"])):
        rate, name = _pitcher_k_rate(pid)
        if not rate:
            continue
        line = default_line("strikeouts", rate)
        proj = project_prop("strikeouts", rate, line)
        if proj:
            proj["player"] = name
            proj["team"] = g[side]["name"]
            proj["opponent"] = opp
            proj["label"] = "Strikeouts"
            out.append(proj)
    # batter props (hits / HR / RBI) for both lineups
    try:
        batters = get_batter_props(date, game_id).get("props", [])
        out.extend(batters)
    except Exception as e:
        print(f"[mlb] batter props failed: {e}")
    return {"game_id": game_id, "props": out}


# ---- prop game logs (last N games, for the history chart) ---------------

def _pitcher_game_log(pid, stat="strikeOuts", n=10):
    """Return last n starts as [{date, opp, value}] for a stat."""
    if not pid:
        return []
    try:
        data = _get(f"{BASE}/people/{pid}/stats",
                    {"stats": "gameLog", "group": "pitching", "season": SEASON})
        splits = data["stats"][0]["splits"]
    except Exception:
        return []
    out = []
    for s in splits[-n:]:
        st = s.get("stat", {})
        opp = (s.get("opponent", {}) or {}).get("abbreviation", "")
        date = s.get("date", "")
        val = st.get(stat)
        if val is None:
            continue
        try:
            out.append({"date": date[5:] if date else "", "opp": opp, "value": float(val)})
        except (ValueError, TypeError):
            pass
    return out


def _batter_game_log(pid, stat_field, n=10):
    """Last n games as [{date, opp, value}] for a hitting stat field."""
    if not pid:
        return []
    try:
        data = _get(f"{BASE}/people/{pid}/stats",
                    {"stats": "gameLog", "group": "hitting", "season": SEASON})
        splits = data["stats"][0]["splits"]
    except Exception:
        return []
    out = []
    for s in splits[-n:]:
        st = s.get("stat", {})
        opp = (s.get("opponent", {}) or {}).get("abbreviation", "")
        date = s.get("date", "")
        val = st.get(stat_field)
        if val is None:
            continue
        try:
            out.append({"date": date[5:] if date else "", "opp": opp, "value": float(val)})
        except (ValueError, TypeError):
            pass
    return out


_LOG_SPEC = {
    # SportsGameOdds labels (lowercased) -> (statsapi group, field)
    "batting basesonballs": ("hitting", "baseOnBalls"),
    "batting doubles": ("hitting", "doubles"),
    "batting hits": ("hitting", "hits"),
    "batting homeruns": ("hitting", "homeRuns"),
    "batting rbi": ("hitting", "rbi"),
    "batting stolenbases": ("hitting", "stolenBases"),
    "batting strikeouts": ("hitting", "strikeOuts"),
    "batting totalbases": ("hitting", "totalBases"),
    "batting triples": ("hitting", "triples"),
    "pitching basesonballs": ("pitching", "baseOnBalls"),
    "pitching earnedruns": ("pitching", "earnedRuns"),
    "pitching hits": ("pitching", "hits"),
    "pitching outs": ("pitching", "outs"),
    "pitching strikeouts": ("pitching", "strikeOuts"),
    # legacy / canonical aliases
    "strikeouts": ("pitching", "strikeOuts"),
    "hits": ("hitting", "hits"),
    "home_runs": ("hitting", "homeRuns"),
    "home runs": ("hitting", "homeRuns"),
    "rbis": ("hitting", "rbi"),
    "rbi": ("hitting", "rbi"),
    "total bases": ("hitting", "totalBases"),
    "walks": ("hitting", "baseOnBalls"),
}


def _player_id_in_game(date, game_id, player_name):
    """Resolve a player's id from this game's starters + both lineups by name."""
    g = get_game(date, game_id)
    if not g:
        return None
    target = (player_name or "").strip().lower()
    # starters
    for side in ("home", "away"):
        pid = _starter_id(date, game_id, side)
        if pid:
            r, name = _pitcher_k_rate(pid)
            if name and name.lower() == target:
                return pid
    # lineups / rosters
    for side in ("home", "away"):
        batters, _ = _lineup_or_roster(game_id, g[side]["team_id"], side)
        for b in batters:
            if (b.get("name") or "").lower() == target:
                return b.get("id")
    return None


def get_prop_history(date: dt.date, game_id: int, player=None, stat=None, line=None):
    """Last-10 game log for a specific player+stat, with hit/miss vs the line."""
    spec = _LOG_SPEC.get((stat or "").lower())
    if not spec:
        return {"history": [], "games": []}
    group, field = spec
    pid = _player_id_in_game(date, game_id, player)
    if not pid:
        return {"history": [], "games": []}
    if group == "pitching":
        log = _pitcher_game_log(pid, field)
    else:
        log = _batter_game_log(pid, field)
    if line is None:
        line = 0.5
    hits = sum(1 for x in log if x["value"] > line)
    return {"player": player, "label": stat, "line": line,
            "games": log, "hits": hits, "total": len(log)}


# ---- batter props (hits / HR / RBI) ------------------------------------

_batter_rate_cache = {}   # batter_id -> (ts, rates)

def _batter_rates(batter_id):
    """Per-game hits, HR, RBI for a batter from season stats (cached)."""
    if not batter_id:
        return None
    c = _batter_rate_cache.get(batter_id)
    if c and time.time() - c[0] < _DAY_TTL:
        return c[1]
    try:
        data = _get(f"{BASE}/people/{batter_id}",
                    {"hydrate": f"stats(group=[hitting],type=[season],season={SEASON})"})
        person = data["people"][0]
        st = person["stats"][0]["splits"][0]["stat"]
        g = float(st.get("gamesPlayed", 0) or 0)
        if g < 1:
            _batter_rate_cache[batter_id] = (time.time(), None)
            return None
        rates = {
            "name": person.get("fullName"),
            "hits": float(st.get("hits", 0) or 0) / g,
            "home_runs": float(st.get("homeRuns", 0) or 0) / g,
            "rbis": float(st.get("rbi", 0) or 0) / g,
        }
        _batter_rate_cache[batter_id] = (time.time(), rates)
        return rates
    except Exception:
        return None


def get_batter_props(date: dt.date, game_id: int, max_batters=9):
    """Hits/HR/RBI props for each team's lineup (or projected batters)."""
    from props import project_prop, default_line
    g = get_game(date, game_id)
    if not g:
        return {"error": "not found"}
    labels = {"hits": "Hits", "home_runs": "Home Runs", "rbis": "RBIs"}
    out = []
    for side_key in ("home", "away"):
        team_id = g[side_key]["team_id"]
        batters, _ = _lineup_or_roster(game_id, team_id, side_key)
        for b in batters[:max_batters]:
            rates = _batter_rates(b["id"])
            if not rates:
                continue
            for stat in ("hits", "home_runs", "rbis"):
                rate = rates[stat]
                if rate <= 0:
                    continue
                line = default_line(stat, rate)
                proj = project_prop(stat, rate, line)
                if not proj:
                    continue
                proj["player"] = rates["name"]
                proj["team"] = g[side_key]["name"]
                proj["label"] = labels[stat]
                out.append(proj)
    out.sort(key=lambda p: -p["edge"])
    return {"game_id": game_id, "props": out}


def get_boxscore(date, game_id):
    """Live batting + pitching box score for one MLB game, in the same uniform
    shape the ESPN provider emits {teams:[{name,abbr,groups:[{title,columns,rows}]}]}.
    Defensive: pre-game / no-stats responses just yield empty groups."""
    try:
        box = _get(f"https://statsapi.mlb.com/api/v1/game/{game_id}/boxscore")
    except Exception as e:
        print(f"[mlb] boxscore fetch failed: {e}")
        return {"teams": []}
    BAT_COLS = ["AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "K", "SB", "TB", "AVG"]
    PIT_COLS = ["IP", "H", "R", "ER", "BB", "K", "OUT", "ERA"]
    teams_out = []
    for side in ("away", "home"):
        t = ((box.get("teams") or {}).get(side)) or {}
        players = t.get("players", {}) or {}
        tname = (t.get("team", {}) or {}).get("name", side.title())
        groups = []
        bat_rows = []
        for pid in (t.get("battingOrder") or []):
            p = players.get(f"ID{pid}") or {}
            b = ((p.get("stats") or {}).get("batting") or {})
            if not b:
                continue
            person = p.get("person", {}) or {}
            pos = (p.get("position", {}) or {}).get("abbreviation", "")
            avg = ((p.get("seasonStats") or {}).get("batting") or {}).get("avg", "")
            bat_rows.append({"name": person.get("fullName", "Batter"), "pos": pos,
                             "stats": [b.get("atBats", 0), b.get("runs", 0), b.get("hits", 0),
                                       b.get("doubles", 0), b.get("triples", 0),
                                       b.get("homeRuns", 0), b.get("rbi", 0),
                                       b.get("baseOnBalls", 0), b.get("strikeOuts", 0),
                                       b.get("stolenBases", 0), b.get("totalBases", 0), avg]})
        if bat_rows:
            groups.append({"title": "Batting", "columns": BAT_COLS, "rows": bat_rows})
        pit_rows = []
        for pid in (t.get("pitchers") or []):
            p = players.get(f"ID{pid}") or {}
            pi = ((p.get("stats") or {}).get("pitching") or {})
            if not pi:
                continue
            person = p.get("person", {}) or {}
            era = ((p.get("seasonStats") or {}).get("pitching") or {}).get("era", "")
            pit_rows.append({"name": person.get("fullName", "Pitcher"), "pos": "P",
                             "stats": [pi.get("inningsPitched", "0.0"), pi.get("hits", 0),
                                       pi.get("runs", 0), pi.get("earnedRuns", 0),
                                       pi.get("baseOnBalls", 0), pi.get("strikeOuts", 0),
                                       pi.get("outs", 0), era]})
        if pit_rows:
            groups.append({"title": "Pitching", "columns": PIT_COLS, "rows": pit_rows})
        if groups:
            teams_out.append({"name": tname,
                              "abbr": (t.get("team", {}) or {}).get("abbreviation", ""),
                              "groups": groups})
    return {"teams": teams_out}


_officials_cache = {}
def get_officials(game_id):
    """Umpire crew for a game from the MLB boxscore. The home-plate umpire is the
    one that matters for totals/strikeouts. Often posts a couple hours before
    first pitch (empty earlier). Names only — the free API carries no tendency
    data, so we never fabricate an umpire 'factor'."""
    import time as _t
    ck = str(game_id)
    c = _officials_cache.get(ck)
    if c and _t.time() - c[0] < 1800:
        return c[1]
    out = {"sport": "mlb", "officials": [], "home_plate": None}
    try:
        box = _get(f"https://statsapi.mlb.com/api/v1/game/{game_id}/boxscore")
        for o in (box.get("officials") or []):
            name = (o.get("official") or {}).get("fullName")
            otype = o.get("officialType") or ""
            if not name:
                continue
            out["officials"].append({"name": name, "position": otype})
            if otype == "Home Plate":
                out["home_plate"] = name
    except Exception:
        pass
    _officials_cache[ck] = (_t.time(), out)
    return out
