"""
injuries.py
-----------
Per-player injury impact, summed per team, applied as a rating adjustment on top
of whatever model produced a game's prediction. Pulls ESPN's public injuries
feed, weights each OUT/limited player by their individual value (position-based,
sport-specific) scaled by how likely they are to miss the game, sums per team,
caps it, and converts the home-vs-away difference into a probability nudge.

Lookups work by ESPN team id, normalized team name, OR abbreviation, so this
serves every provider regardless of which id space it uses (espn_provider keys
by id; nhl_games by ESPN id; mlb_provider by team name).

Value is position-based: genuinely accurate where position maps to value (NFL
quarterback, NHL goalie); for the NBA it's a sensible floor we can later refine
with usage/stat data. Baseball is pitching-dominated and the day's starter is
handled separately, so MLB injury weights are deliberately light.
"""

from __future__ import annotations

import math
import re
import time

import espn_provider as _ep

# --- per-position value, in Elo points, if the player is fully OUT -----------
_POS = {
    "nfl": {"QB": 190, "RB": 38, "WR": 42, "TE": 26, "LT": 28, "RT": 22, "OT": 24,
            "G": 16, "OG": 16, "C": 20, "OL": 18, "DE": 34, "EDGE": 34, "DT": 26,
            "NT": 20, "LB": 22, "ILB": 22, "OLB": 24, "CB": 34, "S": 24, "FS": 24,
            "SS": 24, "DB": 24, "K": 8, "P": 5, "LS": 3, "FB": 10, "_": 18},
    "nba": {"PG": 55, "SG": 50, "SF": 50, "PF": 48, "C": 50, "G": 52, "F": 49, "_": 45},
    "nhl": {"G": 110, "C": 32, "LW": 28, "RW": 28, "D": 26, "F": 28, "_": 26},
    "ncaaf": {"QB": 150, "RB": 28, "WR": 30, "TE": 18, "DE": 24, "EDGE": 24,
              "CB": 24, "S": 18, "LB": 16, "OL": 14, "K": 6, "_": 14},
    "ncaab": {"G": 45, "F": 42, "C": 44, "_": 40},
    "wncaab": {"G": 45, "F": 42, "C": 44, "_": 40},
    # baseball: pitching dominates and the starter is handled elsewhere, so keep
    # these light -- this only nudges for notable position players / pen arms out.
    "mlb": {"SP": 22, "P": 12, "RP": 10, "C": 16, "1B": 14, "2B": 14, "3B": 14,
            "SS": 16, "LF": 14, "CF": 16, "RF": 14, "DH": 14, "OF": 14, "IF": 13, "_": 12},
}

# ESPN injuries endpoints for sports NOT in espn_provider.SCOREBOARD
_EXTRA_URL = {
    "nhl": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries",
    "mlb": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/injuries",
}

# --- how likely the player is to actually miss, by ESPN status ----------------
_STATUS = {
    "out": 1.0, "injured reserve": 1.0, "ir": 1.0, "suspension": 1.0,
    "60-day-il": 1.0, "15-day-il": 1.0, "10-day-il": 1.0, "day-to-day": 0.25,
    "doubtful": 0.7, "questionable": 0.4, "game-time decision": 0.5, "probable": 0.1,
    "active": 0.0,
}

_MAX_TEAM = {"mlb": 80.0}        # baseball capped lower; everything else below
_MAX_DEFAULT = 230.0
_TTL = 3600
_cache = {}


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("&", "and"))


def _inj_url(sport):
    base = _ep.SCOREBOARD.get(sport)
    if base:
        return base.replace("/scoreboard", "/injuries")
    return _EXTRA_URL.get(sport)


def _status_factor(s):
    s = (s or "").strip().lower()
    if s in _STATUS:
        return _STATUS[s]
    for k, v in _STATUS.items():
        if k in s:
            return v
    return 0.3


def _player_value(sport, pos, status):
    table = _POS.get(sport, {})
    base = table.get((pos or "").upper(), table.get("_", 25))
    return base * _status_factor(status)


def _build(sport):
    url = _inj_url(sport)
    if not url:
        return {"by_id": {}, "by_name": {}, "by_abbr": {}}
    try:
        data = _ep._get(url, {})
    except Exception:
        return {"by_id": {}, "by_name": {}, "by_abbr": {}}
    cap = _MAX_TEAM.get(sport, _MAX_DEFAULT)
    by_id, by_name, by_abbr = {}, {}, {}
    for blk in (data.get("injuries") or []):
        team = blk.get("team") or {}
        tid = str(team.get("id") or "")
        tname = team.get("displayName") or team.get("name") or ""
        tabbr = team.get("abbreviation") or ""
        items = blk.get("injuries") or blk.get("items") or []
        pen = 0.0
        notable = []
        for it in items:
            ath = it.get("athlete") or {}
            pos = ((ath.get("position") or {}).get("abbreviation")
                   or (ath.get("position") or {}).get("name") or "")
            status = (it.get("status") or (it.get("type") or {}).get("description")
                      or (it.get("details") or {}).get("type") or "")
            val = _player_value(sport, pos, status)
            if val <= 0:
                continue
            pen += val
            if val >= 18:
                notable.append({"name": ath.get("displayName", "?"), "pos": pos,
                                "status": status, "impact": round(val)})
        if pen <= 0:
            continue
        notable.sort(key=lambda x: -x["impact"])
        entry = {"penalty": round(min(pen, cap), 1), "players": notable[:6], "team": tname}
        if tid:
            by_id[tid] = entry
        if tname:
            by_name[_norm(tname)] = entry
        if tabbr:
            by_abbr[tabbr.upper()] = entry
    return {"by_id": by_id, "by_name": by_name, "by_abbr": by_abbr}


def _table(sport):
    now = time.time()
    hit = _cache.get(sport)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    tbl = _build(sport)
    _cache[sport] = (now, tbl)
    return tbl


def enabled(sport):
    return sport in _POS and _inj_url(sport) is not None


def for_team(sport, team_id=None, name=None, abbr=None):
    """Injury entry for one team by id, normalized name, or abbreviation."""
    empty = {"penalty": 0.0, "players": []}
    if not enabled(sport):
        return empty
    t = _table(sport)
    if team_id and str(team_id) in t["by_id"]:
        return t["by_id"][str(team_id)]
    if name and _norm(name) in t["by_name"]:
        return t["by_name"][_norm(name)]
    if abbr and abbr.upper() in t["by_abbr"]:
        return t["by_abbr"][abbr.upper()]
    return empty


def penalty(sport, team_id=None, name=None, abbr=None):
    return float(for_team(sport, team_id, name, abbr).get("penalty") or 0.0)


def adjust_prob(prob_home, net_elo):
    """Shift a home win probability by a net Elo edge (+ favors home)."""
    if not net_elo or prob_home is None:
        return prob_home
    p = min(0.999, max(0.001, prob_home))
    e = -400.0 * math.log10(1.0 / p - 1.0)
    return round(1.0 / (1.0 + 10 ** (-((e + net_elo) / 400.0))), 4)


def game_adjust(sport, game):
    """Adjust a built game dict in place: read home/away identifiers, shift
    prob_home by the net injury Elo, and attach a game['injuries'] panel.
    Safe no-op when the sport is unsupported or nobody's hurt."""
    if not enabled(sport) or not isinstance(game, dict):
        return game
    h = game.get("home") or {}
    a = game.get("away") or {}
    ih = for_team(sport, h.get("team_id"), h.get("name"), h.get("abbr"))
    ia = for_team(sport, a.get("team_id"), a.get("name"), a.get("abbr"))
    net = (ia.get("penalty", 0.0) - ih.get("penalty", 0.0))
    if net and game.get("prob_home") is not None:
        game["prob_home"] = adjust_prob(game["prob_home"], net)
        game["injuries"] = {"home": ih.get("players", []), "away": ia.get("players", []),
                            "home_pts": ih.get("penalty", 0.0), "away_pts": ia.get("penalty", 0.0)}
    return game


def reload(sport=None):
    if sport:
        _cache.pop(sport, None)
    else:
        _cache.clear()
    return True
