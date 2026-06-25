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
import math
import os

ELO_BASE = 1500.0
K = 20.0

# Default home-edge (Elo pts) used DURING training, mirroring team_model so the
# trained ratings and the live predictor share assumptions.
_HFA = {"nba": 70.0, "nfl": 50.0, "ncaaf": 65.0, "ncaab": 75.0, "wncaab": 72.0}

# How far to regress a team's rating toward the mean at each season boundary
# (carryover). Football turns over rosters more, so it regresses harder.
_REGRESS = {"nba": 0.25, "nfl": 0.33, "ncaaf": 0.35, "ncaab": 0.30, "wncaab": 0.30}

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
    """Most recent season that has already STARTED as of `today`, end-capped at
    today. Handles seasons that wrap into the next calendar year (NFL/NCAAF/
    NCAAB) and in-progress seasons (returns season-start -> today)."""
    today = today or dt.date.today()
    (sm, sd), (em, ed) = _SEASON.get(sport, ((1, 1), (12, 31)))
    wrap = 1 if (em, ed) < (sm, sd) else 0
    best = None
    for sy in (today.year - 2, today.year - 1, today.year):
        start = dt.date(sy, sm, sd)
        end = dt.date(sy + wrap, em, ed)
        if start <= today:                       # season has begun
            best = (start, min(end, today))
    if best:
        return best
    sy = today.year - 1                           # nothing begun yet -> last year
    return dt.date(sy, sm, sd), dt.date(sy + wrap, em, ed)


def _mov_mult(margin, elo_diff_winner):
    """538's margin-of-victory multiplier: bigger wins move ratings more, but the
    autocorrelation term shrinks the move when a heavy favorite wins (so good
    teams can't farm rating by running up the score) and grows it on an upset."""
    return math.log(abs(margin) + 1.0) * (2.2 / ((elo_diff_winner * 0.001) + 2.2))


def _season_windows(sport, n, today=None):
    """The last `n` season (start, end) windows, oldest first, newest capped at
    today. Used to train with carryover instead of a single cold-started year."""
    today = today or dt.date.today()
    cur_start, _ = default_window(sport, today)
    (sm, sd), (em, ed) = _SEASON.get(sport, ((1, 1), (12, 31)))
    wrap = 1 if (em, ed) < (sm, sd) else 0
    wins = []
    for k in range(n - 1, -1, -1):
        s = dt.date(cur_start.year - k, sm, sd)
        e = min(dt.date(cur_start.year - k + wrap, em, ed), today)
        if s <= today and s < e:
            wins.append((s, e))
    return wins or [default_window(sport, today)]


def _train_window(ep, sport, ws, we, ratings, names, hfa, k, report, progress):
    """Walk one season window day-by-day, applying MOV-weighted Elo updates."""
    def R(tid):
        return ratings.get(tid, ELO_BASE)
    cur = ws
    while cur <= we:
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
            ht, at = (home.get("team") or {}), (away.get("team") or {})
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
            eh = _expected(rh + hfa, ra)
            sh = 1.0 if hs > as_ else 0.0
            margin = abs(hs - as_)
            diff_w = (rh + hfa - ra) if sh == 1.0 else (ra - rh - hfa)  # winner's edge
            delta = k * _mov_mult(margin, diff_w) * (sh - eh)
            ratings[hid] = rh + delta
            ratings[aid] = ra - delta
            report["games"] += 1
        report["teams"] = len(ratings)
        report["day"] = cur.isoformat()
        if progress:
            try:
                progress(dict(report))
            except Exception:
                pass
        cur += dt.timedelta(days=1)


def build(sport, start=None, end=None, seasons=2, progress=None):
    """Train MOV-weighted W/L Elo from ESPN results, with multi-season carryover
    (each new season regressed toward the mean) so current ratings carry real
    history instead of cold-starting at 1500. If explicit start/end are given,
    trains just that single window. Writes /data/{sport}_elo.json."""
    import espn_provider as ep
    if sport not in ep.SCOREBOARD:
        raise ValueError(f"unsupported sport {sport!r}")
    hfa = _HFA.get(sport, 60.0)
    regress = _REGRESS.get(sport, 0.30)
    ratings, names = {}, {}
    report = {"sport": sport, "games": 0, "teams": 0, "day": None, "seasons": []}

    if start is not None or end is not None:
        ws, we = default_window(sport)
        windows = [(start or ws, end or we)]
    else:
        windows = _season_windows(sport, max(1, int(seasons)))

    for i, (ws, we) in enumerate(windows):
        if i > 0 and ratings:                       # regress toward mean between seasons
            for tid in list(ratings):
                ratings[tid] = ELO_BASE + (ratings[tid] - ELO_BASE) * (1.0 - regress)
        report["seasons"].append({"start": ws.isoformat(), "end": we.isoformat()})
        report["start"], report["end"] = ws.isoformat(), we.isoformat()
        _train_window(ep, sport, ws, we, ratings, names, hfa, K, report, progress)

    out = {"sport": sport, "updated": dt.datetime.utcnow().isoformat() + "Z",
           "windows": report["seasons"], "games": report["games"],
           "k": K, "hfa": hfa, "regress": regress, "mov": True,
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
