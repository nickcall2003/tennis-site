"""
Home-plate umpire tendencies, computed for FREE from the official MLB statsapi.

There is no public feed of umpire 'tendency' numbers, but the raw ingredients
are all in the free statsapi: every final game's boxscore carries the officials
(including the Home Plate umpire) plus total runs, strikeouts, and walks. So we
aggregate completed games per home-plate umpire into a runs/strikeout profile
versus the league average, entirely from data we already have access to.

Design notes:
- Incremental: we remember which game IDs we've already counted, so each refresh
  only fetches boxscores for new finals. The first backfill is API-heavy, hence
  a per-call `max_games` cap; later refreshes are cheap.
- Persistent: state lives on the /data volume next to the SQLite db.
- Honest: a tendency is only reported once an umpire has a minimum sample, and
  the run-environment nudge it produces is dampened and hard-capped.
"""
import os
import json
import time
import datetime as dt

import httpx

_BASE = "https://statsapi.mlb.com/api/v1"
_PATH = os.environ.get("UMP_DATA",
                       "/data/ump_tendencies.json" if os.path.isdir("/data")
                       else "ump_tendencies.json")

_MIN_GAMES = 8        # don't report a tendency below this sample
_MIN_LEAGUE = 60      # don't report anything until the league baseline is stable

_cache = {"ts": 0, "state": None}


def _blank():
    return {"processed": [], "umps": {}, "league": {"games": 0, "runs": 0,
            "ks": 0, "bbs": 0}, "updated": None}


def _load():
    # tiny in-process cache so per-game lookups don't hit disk every call
    if _cache["state"] is not None and time.time() - _cache["ts"] < 300:
        return _cache["state"]
    try:
        with open(_PATH) as f:
            state = json.load(f)
        for k in ("processed", "umps", "league"):
            state.setdefault(k, _blank()[k])
    except Exception:
        state = _blank()
    _cache["state"] = state
    _cache["ts"] = time.time()
    return state


def _save(state):
    try:
        tmp = _PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, _PATH)
    except Exception as e:
        print(f"[umpire] save failed: {e}")
    _cache["state"] = state
    _cache["ts"] = time.time()


def _get(url, params=None):
    r = httpx.get(url, params=params or {}, timeout=20.0)
    r.raise_for_status()
    return r.json()


def refresh(days=70, max_games=120):
    """Scan recent FINAL games and fold any not-yet-counted ones into the
    per-umpire totals. Returns a small progress summary. Bounded by max_games so
    a single call returns well within a request timeout; call again to continue
    a large backfill."""
    state = _load()
    processed = set(state["processed"])
    today = dt.date.today()
    start = today - dt.timedelta(days=days)
    try:
        sched = _get(f"{_BASE}/schedule",
                     {"sportId": 1, "startDate": start.isoformat(),
                      "endDate": today.isoformat(), "gameType": "R"})
    except Exception as e:
        return {"error": f"schedule fetch failed: {e}"}

    todo = []
    for d in sched.get("dates", []):
        for g in d.get("games", []):
            if (g.get("status", {}) or {}).get("abstractGameState") == "Final":
                gid = g.get("gamePk")
                if gid and str(gid) not in processed:
                    todo.append(gid)
    todo = todo[:max_games]

    counted = 0
    for gid in todo:
        try:
            box = _get(f"https://statsapi.mlb.com/api/v1/game/{gid}/boxscore")
        except Exception:
            continue   # transient — leave unprocessed so we retry next time
        hp = None
        for o in (box.get("officials") or []):
            if o.get("officialType") == "Home Plate":
                hp = (o.get("official") or {}).get("fullName")
                break
        # mark processed regardless so we never re-fetch a final game; a final
        # with no listed HP ump simply contributes nothing.
        processed.add(str(gid))
        state["processed"].append(str(gid))
        if not hp:
            continue
        try:
            th = box["teams"]["home"]["teamStats"]["batting"]
            ta = box["teams"]["away"]["teamStats"]["batting"]
            runs = (th.get("runs", 0) or 0) + (ta.get("runs", 0) or 0)
            ks = (th.get("strikeOuts", 0) or 0) + (ta.get("strikeOuts", 0) or 0)
            bbs = (th.get("baseOnBalls", 0) or 0) + (ta.get("baseOnBalls", 0) or 0)
        except Exception:
            continue
        u = state["umps"].setdefault(hp, {"games": 0, "runs": 0, "ks": 0, "bbs": 0})
        u["games"] += 1; u["runs"] += runs; u["ks"] += ks; u["bbs"] += bbs
        lg = state["league"]
        lg["games"] += 1; lg["runs"] += runs; lg["ks"] += ks; lg["bbs"] += bbs
        counted += 1

    # keep the processed list from growing without bound (a season is ~2.4k games)
    if len(state["processed"]) > 6000:
        state["processed"] = state["processed"][-6000:]
    state["updated"] = dt.datetime.utcnow().isoformat()
    _save(state)
    remaining = max(0, len(todo) - counted) + 0
    return {"counted_now": counted, "total_games": state["league"]["games"],
            "umpires": len(state["umps"]), "updated": state["updated"],
            "more_pending": len(todo) >= max_games}


def get_tendency(name, min_games=_MIN_GAMES):
    """Per-umpire run/strikeout profile vs the league average, or None when the
    sample (or league baseline) is too thin to be meaningful."""
    if not name:
        return None
    state = _load()
    u = state["umps"].get(name)
    lg = state["league"]
    if not u or u["games"] < min_games or lg["games"] < _MIN_LEAGUE:
        return None
    g = u["games"]
    lrpg = lg["runs"] / lg["games"]
    lkpg = lg["ks"] / lg["games"]
    rpg = u["runs"] / g
    kpg = u["ks"] / g
    runs_vs = rpg - lrpg
    if runs_vs >= 0.25:
        lean = "hitter-friendly"
    elif runs_vs <= -0.25:
        lean = "pitcher-friendly"
    else:
        lean = "neutral"
    return {"name": name, "games": g,
            "runs_per_game": round(rpg, 2), "runs_vs_avg": round(runs_vs, 2),
            "k_per_game": round(kpg, 2), "k_vs_avg": round(kpg - lkpg, 2),
            "lg_runs": round(lrpg, 2), "lg_ks": round(lkpg, 2), "lean": lean}


def runs_factor(name, min_games=_MIN_GAMES):
    """A dampened, ±5%-capped multiplier on the run environment for this ump.
    1.0 when there's no usable tendency."""
    t = get_tendency(name, min_games)
    if not t:
        return 1.0
    base = t["lg_runs"] or 8.6
    raw = (t["runs_vs_avg"] / base) * 0.5      # half-weight the deviation
    return round(1.0 + max(-0.05, min(0.05, raw)), 3)


def summary():
    state = _load()
    lg = state["league"]
    return {"total_games": lg["games"], "umpires": len(state["umps"]),
            "updated": state.get("updated"),
            "lg_runs": round(lg["runs"] / lg["games"], 2) if lg["games"] else None,
            "lg_ks": round(lg["ks"] / lg["games"], 2) if lg["games"] else None,
            "path": _PATH}
