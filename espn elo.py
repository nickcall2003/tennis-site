"""
espn_elo.py
-----------
Builds a team-strength Elo for the ESPN-backed team sports (NBA, NFL, NCAAF,
NCAAB) from completed game results on ESPN's public scoreboard — the same
source the live board already uses, and one Railway can reach. This is the
team-sport analogue of the tennis feed-Elo: when a sport has no stats/efficiency
file (the FALLBACK state), these ratings let team_model predict from real team
strength instead of win%-seeding everyone equal.

Ratings are keyed by ESPN team id (stable, no name matching). Files live on the
persistent volume as /data/{sport}_elo.json so they survive redeploys.

Nothing here runs on the request path — main.py triggers build() in a background
thread; espn_provider only reads the small JSON it produces.
"""

from __future__ import annotations

import datetime as dt
import json
import os

ELO_BASE = 1500.0
K = 20.0

# Default home-edge (Elo pts) used DURING training, mirroring team_model so the
# trained ratings and the live predictor share assumptions.
_HFA = {"nba": 70.0, "nfl": 50.0, "ncaaf": 65.0, "ncaab": 75.0, "wncaab": 72.0}

# Reasonable season windows (month, day) -> (month, day) for "most recent season"
# when explicit start/end aren't given. End-exclusive of next season.
_SEASON = {
    "nba":   ((10, 1), (6, 30)),
    "nfl":   ((9, 1),  (2, 15)),
    "ncaaf": ((8, 24), (1, 15)),
    "ncaab": ((11, 1), (4, 10)),
    "wncaab": ((11, 1), (4, 10)),
}

_cache: dict[str, dict] = {}   # sport -> {team_id: rating}


def _path(sport):
    return f"/data/{sport}_elo.json"


def _expected(ra, rb):
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))


def default_window(sport, today=None):
    """Most recently COMPLETED season window for the sport, as (start, end) dates."""
    today = today or dt.date.today()
    (sm, sd), (em, ed) = _SEASON.get(sport, ((1, 1), (12, 31)))
    # A season labeled by its start year. If we're past this year's end month,
    # the latest finished season started this year; else it started last year.
    start_year = today.year if (today.month, today.day) >= (em, ed) else today.year - 1
    start = dt.date(start_year, sm, sd)
    end_year = start_year + (1 if em < sm else 0)
    end = min(dt.date(end_year, em, ed), today)
    return start, end


def build(sport, start=None, end=None, progress=None):
    """Walk ESPN scoreboard start..end (chronological) and train W/L Elo.
    Returns {"teams": n, "games": g, "path": ...}. progress(callable) gets the
    live dict each day so a status endpoint can poll it."""
    import espn_provider as ep
    if sport not in ep.SCOREBOARD:
        raise ValueError(f"unsupported sport {sport!r}")
    if start is None or end is None:
        ws, we = default_window(sport)
        start = start or ws
        end = end or we
    hfa = _HFA.get(sport, 60.0)
    ratings: dict[str, float] = {}
    names: dict[str, str] = {}

    def R(tid):
        return ratings.get(tid, ELO_BASE)

    games = 0
    cur = start
    report = {"sport": sport, "start": start.isoformat(), "end": end.isoformat(),
              "games": 0, "teams": 0, "day": None}
    while cur <= end:
        try:
            data = ep._get(ep.SCOREBOARD[sport], {"dates": cur.strftime("%Y%m%d")})
        except Exception:
            data = {}
        for ev in (data.get("events") or []):
            comp = (ev.get("competitions") or [None])[0]
            if not comp:
                continue
            st = ((comp.get("status") or {}).get("type") or {})
            if not st.get("completed"):
                continue
            cs = comp.get("competitors", []) or []
            home = next((c for c in cs if c.get("homeAway") == "home"), None)
            away = next((c for c in cs if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue
            ht = (home.get("team") or {})
            at = (away.get("team") or {})
            hid, aid = ht.get("id"), at.get("id")
            if not hid or not aid:
                continue
            try:
                hs, as_ = int(home.get("score")), int(away.get("score"))
            except (TypeError, ValueError):
                continue
            if hs == as_:
                continue
            names[hid] = ht.get("displayName", hid)
            names[aid] = at.get("displayName", aid)
            rh, ra = R(hid), R(aid)
            eh = _expected(rh + hfa, ra)        # home edge baked into expectation
            sh = 1.0 if hs > as_ else 0.0
            ratings[hid] = rh + K * (sh - eh)
            ratings[aid] = ra + K * ((1.0 - sh) - (1.0 - eh))
            games += 1
        report["games"] = games
        report["teams"] = len(ratings)
        report["day"] = cur.isoformat()
        if progress:
            try:
                progress(dict(report))
            except Exception:
                pass
        cur += dt.timedelta(days=1)

    out = {"sport": sport, "updated": dt.datetime.utcnow().isoformat() + "Z",
           "start": start.isoformat(), "end": end.isoformat(),
           "games": games, "k": K, "hfa": hfa,
           "ratings": ratings, "names": names}
    try:
        with open(_path(sport), "w") as f:
            json.dump(out, f)
    except Exception as e:
        report["save_error"] = str(e)
    _cache[sport] = ratings
    report["teams"] = len(ratings)
    report["path"] = _path(sport)
    report["status"] = "done"
    return report


def load(sport):
    """Lazy-load a sport's Elo ratings ({team_id: rating}); {} if none built."""
    if sport in _cache:
        return _cache[sport]
    try:
        with open(_path(sport)) as f:
            data = json.load(f)
        _cache[sport] = {k: float(v) for k, v in (data.get("ratings") or {}).items()}
    except Exception:
        _cache[sport] = {}
    return _cache[sport]


def lookup(sport, team_id):
    """Elo rating for one ESPN team id, or None if we don't have it."""
    if not team_id:
        return None
    return load(sport).get(str(team_id))


def reload(sport):
    _cache.pop(sport, None)
    return len(load(sport))
