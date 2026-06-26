"""
schedule.py
-----------
Rest-days and schedule-spot adjustment for the ESPN team sports. Markets are
slow to fully price fatigue — back-to-backs, short weeks, post-bye rust — so
folding rest into the prediction is one of the cheaper edges available.

For a given sport + date we scan the prior several days of ESPN scoreboards to
find when each team last played, turn the home-vs-away rest differential into an
Elo nudge (with an extra penalty for a true back-to-back), and shift the home
win probability. Cached per (sport, date).
"""

from __future__ import annotations

import datetime as dt
import time

import espn_provider as _ep
import injuries as _inj          # reuse the logistic prob shift

# Elo points per day of rest advantage, capped, plus a flat back-to-back hit.
_CFG = {
    "nba":   {"per_day": 16, "cap": 45, "b2b": 16, "lookback": 9},
    "nhl":   {"per_day": 14, "cap": 38, "b2b": 14, "lookback": 9},
    "ncaab": {"per_day": 10, "cap": 30, "b2b": 8,  "lookback": 9},
    "nfl":   {"per_day": 6,  "cap": 40, "b2b": 0,  "lookback": 16},   # weekly: bye / short week
    "ncaaf": {"per_day": 5,  "cap": 30, "b2b": 0,  "lookback": 16},
}

_cache = {}                       # (sport, isodate) -> (ts, {team_id: rest_days})


def enabled(sport):
    return sport in _CFG and sport in _ep.SCOREBOARD


def _scan_last_played(sport, target, lookback):
    """Most recent completed-game date for each team strictly before `target`."""
    last = {}
    for i in range(1, lookback + 1):
        d = target - dt.timedelta(days=i)
        try:
            data = _ep._get(_ep.SCOREBOARD[sport], {"dates": d.strftime("%Y%m%d")})
        except Exception:
            continue
        for ev in (data.get("events") or []):
            comp = (ev.get("competitions") or [None])[0]
            if not comp:
                continue
            st = ((comp.get("status") or {}).get("type") or {})
            if not st.get("completed"):
                continue
            for c in comp.get("competitors", []) or []:
                tid = str((c.get("team") or {}).get("id") or "")
                if tid and tid not in last:        # scanning newest->oldest: first hit = latest
                    last[tid] = d
    return last


def rest_table(sport, target):
    if not enabled(sport):
        return {}
    key = (sport, target.isoformat())
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    last = _scan_last_played(sport, target, _CFG[sport]["lookback"])
    rest = {tid: (target - d).days for tid, d in last.items()}
    _cache[key] = (time.time(), rest)
    return rest


def rest_elo(sport, home_rest, away_rest):
    """Elo points favoring HOME from the rest differential (+ extra for a B2B).
    Returns (points, note)."""
    if home_rest is None or away_rest is None:
        return 0.0, None
    cfg = _CFG[sport]
    pts = (home_rest - away_rest) * cfg["per_day"]
    note = None
    if cfg["b2b"]:
        h_b2b, a_b2b = home_rest <= 1, away_rest <= 1
        if h_b2b and not a_b2b:
            pts -= cfg["b2b"]; note = "home back-to-back"
        elif a_b2b and not h_b2b:
            pts += cfg["b2b"]; note = "away back-to-back"
    pts = max(-cfg["cap"], min(cfg["cap"], pts))     # final clamp incl. B2B
    return round(pts, 1), note


def adjust(sport, prob_home, home_id, away_id, target):
    """Return (adjusted_prob, info|None) for a single matchup."""
    if not enabled(sport) or prob_home is None:
        return prob_home, None
    rest = rest_table(sport, target)
    hr = rest.get(str(home_id))
    ar = rest.get(str(away_id))
    pts, note = rest_elo(sport, hr, ar)
    if not pts:
        return prob_home, None
    return _inj.adjust_prob(prob_home, pts), {"home_days": hr, "away_days": ar,
                                              "note": note, "elo": pts}


def game_adjust(sport, game, target):
    """Adjust a built game dict in place (used by the NHL endpoint)."""
    if not enabled(sport) or not isinstance(game, dict):
        return game
    h, a = game.get("home") or {}, game.get("away") or {}
    newp, info = adjust(sport, game.get("prob_home"), h.get("team_id"), a.get("team_id"), target)
    if info:
        game["prob_home"] = newp
        game["rest"] = info
    return game
