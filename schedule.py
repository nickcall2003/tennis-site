"""
schedule.py
-----------
Days-of-rest calculation and a small win-probability nudge toward the more-rested
team, across all supported sports.

Rest = days between a team's most recent COMPLETED game and the game in question.
It's real signal: short rest (a back-to-back in the NBA, a Thu game after a Sat)
saps performance; extra rest is roughly neutral-to-slightly-positive.

THE BUG THIS FIXES: the old version defaulted to "1 day" whenever it couldn't
find a team's prior game — so in Week 1/2 of a season (when most teams have played
once, ~7 days earlier, and the schedule scan came up empty) essentially every
team showed "+1d rest", which is wrong (a team that hasn't played in a week is on
long rest, not 1 day). Here, a missing prior game returns None → the rest badge
simply doesn't show for that team, instead of a fabricated "1".

Public interface (consumed by main.py, espn_provider.py, index.html):
  enabled(sport)                         -> bool
  _CFG                                   -> {sport: {...}} supported-sport config
  rest_table(sport, target_date)         -> {team_id: days}  (diagnostics)
  adjust(sport, prob_home, home_id, away_id, date)
                                         -> (new_prob, rest_info | None)
        rest_info = {home_days, away_days, note} — exactly what the UI reads.
  game_adjust(sport, game, target_date)  -> mutate game in place: attach g['rest']
        and nudge g['prob_home'] toward the rested side.

Everything is best-effort and NEVER raises into the caller. No prior game found
-> that team's rest is None and it simply isn't shown or used.
"""
from __future__ import annotations

import datetime as dt
import os
import time

import httpx

# --- per-sport config -------------------------------------------------------
# espn: uses ESPN's per-team schedule endpoint (same host the provider uses).
# max_nudge: cap (in win-prob points, 0..1) the rest edge can move the favorite.
# rest_ref: the "normal" rest for that sport; teams below it are tired, above is
#   neutral. NBA plays every 2-3 days (b2b = 1 day = tired); NFL/college weekly.
_CFG = {
    "nba":   {"kind": "espn", "path": "basketball/nba",
              "rest_ref": 2, "short": 1, "max_nudge": 0.03},
    "wnba":  {"kind": "espn", "path": "basketball/wnba",
              "rest_ref": 2, "short": 1, "max_nudge": 0.03},
    "nfl":   {"kind": "espn", "path": "football/nfl",
              "rest_ref": 7, "short": 5, "max_nudge": 0.02},
    "ncaaf": {"kind": "espn", "path": "football/college-football",
              "rest_ref": 7, "short": 5, "max_nudge": 0.02},
    "ncaab": {"kind": "espn", "path": "basketball/mens-college-basketball",
              "rest_ref": 3, "short": 1, "max_nudge": 0.025},
    "nhl":   {"kind": "espn", "path": "hockey/nhl",
              "rest_ref": 2, "short": 1, "max_nudge": 0.025},
    "mlb":   {"kind": "mlb",  "rest_ref": 1, "short": 0, "max_nudge": 0.015},
    # college baseball: ESPN college-baseball schedule if available; best-effort.
    "ncaabb": {"kind": "espn", "path": "baseball/college-baseball",
               "rest_ref": 2, "short": 1, "max_nudge": 0.015},
}

_TIMEOUT = float(os.environ.get("SCHEDULE_TIMEOUT", "6"))
# How far back to search for a prior game. Must be long enough that a Week-2
# team finds its Week-1 game (~7 days) — the old too-short window is exactly why
# rest defaulted to 1. A month covers any in-season gap incl. byes.
_LOOKBACK_DAYS = int(os.environ.get("SCHEDULE_LOOKBACK_DAYS", "30"))

# team_id -> (fetched_ts, [game_date, ...] completed, sorted asc)
_sched_cache: dict[str, tuple[float, list]] = {}
_SCHED_TTL = float(os.environ.get("SCHEDULE_TTL", str(6 * 3600)))


def enabled(sport: str) -> bool:
    return sport in _CFG


def _client():
    return httpx.Client(timeout=_TIMEOUT, headers={
        "User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        follow_redirects=True)


def _to_date(s):
    """Parse an ISO-ish date/datetime string to a date, or None."""
    if not s:
        return None
    try:
        s = str(s).replace("Z", "+00:00")
        if "T" in s:
            return dt.datetime.fromisoformat(s).date()
        return dt.date.fromisoformat(s[:10])
    except Exception:
        try:
            return dt.datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
        except Exception:
            return None


# --- schedule fetch per source ---------------------------------------------

def _espn_completed_dates(sport, team_id):
    """All completed-game DATES for an ESPN team, sorted ascending. []
    on any failure (never raises)."""
    cfg = _CFG.get(sport) or {}
    path = cfg.get("path")
    if not path or not team_id:
        return []
    key = f"{sport}:{team_id}"
    hit = _sched_cache.get(key)
    if hit and (time.time() - hit[0] < _SCHED_TTL):
        return hit[1]
    dates = []
    try:
        base = f"https://site.api.espn.com/apis/site/v2/sports/{path}"
        with _client() as c:
            r = c.get(f"{base}/teams/{team_id}/schedule")
            if r.status_code != 200:
                _sched_cache[key] = (time.time(), [])
                return []
            data = r.json()
        for ev in data.get("events", []) or []:
            comp = (ev.get("competitions") or [{}])[0]
            status = ((comp.get("status") or {}).get("type") or {})
            completed = bool(status.get("completed"))
            d = _to_date(ev.get("date") or comp.get("date"))
            if completed and d:
                dates.append(d)
    except Exception:
        dates = []
    dates.sort()
    _sched_cache[key] = (time.time(), dates)
    return dates


def _mlb_completed_dates(team_id):
    """Completed-game dates for an MLB team via the public MLB Stats API."""
    if not team_id:
        return []
    key = f"mlb:{team_id}"
    hit = _sched_cache.get(key)
    if hit and (time.time() - hit[0] < _SCHED_TTL):
        return hit[1]
    dates = []
    try:
        today = dt.date.today()
        start = (today - dt.timedelta(days=_LOOKBACK_DAYS)).isoformat()
        end = today.isoformat()
        url = ("https://statsapi.mlb.com/api/v1/schedule"
               f"?sportId=1&teamId={team_id}&startDate={start}&endDate={end}")
        with _client() as c:
            r = c.get(url)
            data = r.json() if r.status_code == 200 else {}
        for day in data.get("dates", []) or []:
            d = _to_date(day.get("date"))
            for gm in day.get("games", []) or []:
                st = ((gm.get("status") or {}).get("abstractGameState") or "")
                if d and st == "Final":
                    dates.append(d)
    except Exception:
        dates = []
    dates.sort()
    _sched_cache[key] = (time.time(), dates)
    return dates


def _completed_dates(sport, team_id):
    cfg = _CFG.get(sport) or {}
    if cfg.get("kind") == "mlb":
        return _mlb_completed_dates(team_id)
    return _espn_completed_dates(sport, team_id)


# --- rest computation -------------------------------------------------------

def rest_days(sport, team_id, target_date):
    """Days of rest before `target_date` = days since the team's most recent
    COMPLETED game strictly before that date. Returns None when no prior game is
    found (the fix: None means 'unknown', never a fabricated 1)."""
    if not enabled(sport) or not team_id:
        return None
    if isinstance(target_date, str):
        target_date = _to_date(target_date)
    if not target_date:
        return None
    dates = _completed_dates(sport, team_id)
    prior = [d for d in dates
             if d < target_date and (target_date - d).days <= _LOOKBACK_DAYS]
    if not prior:
        return None                        # <-- no prior game => unknown, not 1
    last = max(prior)
    return (target_date - last).days


def rest_table(sport, target_date):
    """{team_id: rest_days} for every team playing on target_date. Used by
    /api/rest/diag. Only includes teams with a KNOWN prior game (no fake 1s).
    Scans that day's scoreboard to find who's playing."""
    out = {}
    cfg = _CFG.get(sport)
    if not cfg:
        return out
    if isinstance(target_date, str):
        target_date = _to_date(target_date)
    if not target_date:
        return out
    team_ids = []
    try:
        if cfg.get("kind") == "espn":
            base = f"https://site.api.espn.com/apis/site/v2/sports/{cfg['path']}"
            ymd = target_date.strftime("%Y%m%d")
            with _client() as c:
                r = c.get(f"{base}/scoreboard", params={"dates": ymd, "limit": 400})
                data = r.json() if r.status_code == 200 else {}
            for ev in data.get("events", []) or []:
                comp = (ev.get("competitions") or [{}])[0]
                for cs in comp.get("competitors", []) or []:
                    tid = str((cs.get("team") or {}).get("id") or "")
                    if tid:
                        team_ids.append(tid)
        elif cfg.get("kind") == "mlb":
            url = ("https://statsapi.mlb.com/api/v1/schedule?sportId=1"
                   f"&date={target_date.isoformat()}")
            with _client() as c:
                r = c.get(url)
                data = r.json() if r.status_code == 200 else {}
            for day in data.get("dates", []) or []:
                for gm in day.get("games", []) or []:
                    for side in ("home", "away"):
                        tid = str((((gm.get("teams") or {}).get(side) or {})
                                   .get("team") or {}).get("id") or "")
                        if tid:
                            team_ids.append(tid)
    except Exception:
        team_ids = []
    for tid in set(team_ids):
        rd = rest_days(sport, tid, target_date)
        if rd is not None:                 # omit unknowns entirely
            out[tid] = rd
    return out


def _rest_info(sport, home_id, away_id, date):
    """Build the {home_days, away_days, note} object the UI reads, or None if
    NEITHER team's rest is known (so nothing renders rather than a fake badge)."""
    hd = rest_days(sport, home_id, date)
    ad = rest_days(sport, away_id, date)
    if hd is None and ad is None:
        return None
    info = {"home_days": hd, "away_days": ad}
    # A short-rest flag as an optional human note (used when one side is clearly
    # on a back-to-back / short week and the other isn't).
    cfg = _CFG.get(sport) or {}
    short = cfg.get("short")
    if short is not None:
        if hd is not None and hd <= short and (ad is None or ad > short):
            info["note"] = "home on short rest"
        elif ad is not None and ad <= short and (hd is None or hd > short):
            info["note"] = "away on short rest"
    return info


def adjust(sport, prob_home, home_id, away_id, date):
    """Nudge the home win-prob toward the more-rested team and return
    (new_prob, rest_info | None). Conservative: capped by the sport's max_nudge,
    and only applied when BOTH teams' rest is known (so a one-sided unknown never
    creates a phantom edge)."""
    info = _rest_info(sport, home_id, away_id, date)
    if not info or prob_home is None:
        return prob_home, info
    hd, ad = info.get("home_days"), info.get("away_days")
    if hd is None or ad is None:
        return prob_home, info             # need both to compare fairly
    cfg = _CFG.get(sport) or {}
    ref = cfg.get("rest_ref", 3)
    max_nudge = cfg.get("max_nudge", 0.02)
    # Diminishing rest advantage: cap each side's effective rest at ref+ (extra
    # rest beyond normal is ~neutral), so the edge comes from one side being
    # BELOW normal (tired), not from piling up rest.
    eff_h = min(hd, ref)
    eff_a = min(ad, ref)
    if ref <= 0:
        return prob_home, info
    # rest differential as a fraction of a normal rest period, then scaled.
    diff = (eff_h - eff_a) / float(ref)     # -1..+1 roughly
    nudge = max(-max_nudge, min(max_nudge, diff * max_nudge))
    new_p = max(0.02, min(0.98, prob_home + nudge))
    return new_p, info


def game_adjust(sport, game, target_date):
    """Attach rest info to a game dict and nudge its prob_home. Mutates in place,
    best-effort. `game` carries home/away with team_id."""
    try:
        if not enabled(sport) or not isinstance(game, dict):
            return game
        h = (game.get("home") or {})
        a = (game.get("away") or {})
        hid = h.get("team_id") or h.get("id")
        aid = a.get("team_id") or a.get("id")
        new_p, info = adjust(sport, game.get("prob_home"), hid, aid, target_date)
        if info:
            game["rest"] = info
            if new_p is not None:
                game["prob_home"] = new_p
                if game.get("prob_away") is not None:
                    game["prob_away"] = round(1 - new_p, 4)
    except Exception:
        pass
    return game
