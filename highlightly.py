"""
highlightly.py — Highlightly API (highlightly.net) for NCAA college baseball
team batting/pitching stats. OPTIONAL enrichment, key-ready.

Why this exists: ESPN's free college feed gives records but NOT team ERA/OBP/
SLG. Highlightly's NCAA baseball coverage does, which lets college baseball use
the SAME run-expectancy engine as MLB (expected runs from offense vs opposing
pitching) instead of a strength-only Elo. When no key is set, everything falls
back to the existing ESPN+RPI strength model — nothing breaks.

Auth: set HIGHLIGHTLY_API_KEY in the environment. The key is sent as the
x-rapidapi-key header (works for both the RapidAPI host and Highlightly's own).
Host is configurable via HIGHLIGHTLY_HOST (defaults to the RapidAPI baseball host).

Free tier is ~100 requests/day, so we cache team stats hard (12h) and read the
x-ratelimit-requests-remaining header to pause when low.
"""
from __future__ import annotations

import os
import time
import datetime as dt

API_KEY = os.environ.get("HIGHLIGHTLY_API_KEY", "").strip()
# Two front doors, NOT cross-compatible (per Highlightly docs):
#   - Direct platform (default here): https://baseball.highlightly.net
#   - RapidAPI proxy: https://mlb-college-baseball-api.p.rapidapi.com (adds x-rapidapi-host)
# BOTH require the x-rapidapi-key header (confirmed from docs).
# A key from highlightly.net only works against the direct host; a RapidAPI key
# only works against the RapidAPI host. Set HIGHLIGHTLY_PLATFORM=rapidapi to switch.
PLATFORM = os.environ.get("HIGHLIGHTLY_PLATFORM", "direct").strip().lower()
if PLATFORM == "rapidapi":
    HOST = os.environ.get("HIGHLIGHTLY_HOST", "mlb-college-baseball-api.p.rapidapi.com").strip()
else:
    HOST = os.environ.get("HIGHLIGHTLY_HOST", "baseball.highlightly.net").strip()
BASE = f"https://{HOST}"

_team_cache = {}        # team_name_norm -> (ts, stats dict)
_id_cache = {}          # team_name_norm -> team_id
_games_cache = {}       # 'games:LEAGUE:DATE' -> (ts, [games])
_GAMES_TTL = 300        # 5 min; games/scores move during the day
_TTL = 12 * 3600
_DAILY_MAX = int(os.environ.get("HIGHLIGHTLY_DAILY_MAX", "60"))   # under 100 cap
_spend = {"day": None, "count": 0}
_remaining = {"value": None}
# Circuit breaker: if a call fails/times out, stop calling for a cooldown so a
# slow external API can NEVER hang the per-game prediction loop.
_breaker = {"open_until": 0.0, "fails": 0}


def enabled():
    import time as _t
    return bool(API_KEY) and _t.time() >= _breaker["open_until"]


def _norm(name):
    return "".join(c for c in (name or "").lower() if c.isalnum())


def _quota_ok():
    today = dt.date.today().isoformat()
    if _spend["day"] != today:
        _spend["day"] = today
        _spend["count"] = 0
    if _spend["count"] >= _DAILY_MAX:
        return False
    try:
        if _remaining["value"] is not None and int(_remaining["value"]) <= 5:
            return False
    except (ValueError, TypeError):
        pass
    return True


def _get(path, params=None):
    import httpx
    import time as _t
    # Per Highlightly docs: x-rapidapi-key is required on BOTH platforms. Only the
    # RapidAPI proxy additionally needs x-rapidapi-host. (Sending x-api-key on the
    # direct host returns 403 "Missing mandatory HTTP Headers".)
    headers = {"x-rapidapi-key": API_KEY}
    if PLATFORM == "rapidapi":
        headers["x-rapidapi-host"] = HOST
    try:
        r = httpx.get(BASE + path, params=params or {}, headers=headers, timeout=6.0)
    except Exception:
        _breaker["fails"] += 1
        _breaker["open_until"] = _t.time() + 600
        raise
    _spend["count"] = _spend.get("count", 0) + 1
    rem = r.headers.get("x-ratelimit-requests-remaining")
    if rem is not None:
        _remaining["value"] = rem
    r.raise_for_status()
    _breaker["fails"] = 0
    return r.json()


def _find_team_id(name):
    key = _norm(name)
    if key in _id_cache:
        return _id_cache[key]
    if not _quota_ok():
        return None
    # /teams accepts league/name/displayName/abbreviation (NOT limit). ESPN gives
    # us the school name (e.g. "Texas"), which maps to Highlightly's displayName.
    data = None
    for params in ({"league": "NCAA", "displayName": name},
                   {"league": "NCAA", "name": name}):
        try:
            data = _get("/teams", params)
            if data:
                break
        except Exception as e:
            print(f"[highlightly] team lookup {params} failed: {e}")
            data = None
    if not data:
        return None
    teams = data if isinstance(data, list) else data.get("data", [])
    for t in teams:
        tn = t.get("displayName") or t.get("name") or ""
        if _norm(tn) == key or key in _norm(tn) or _norm(tn) in key:
            tid = t.get("id")
            _id_cache[key] = tid
            return tid
    # if exactly one came back, trust it
    if len(teams) == 1:
        tid = teams[0].get("id")
        _id_cache[key] = tid
        return tid
    return None


def get_team_stats(name, season=None):
    """
    Return {rpg, era, obp, slg, ops, whip} for an NCAA team, or {} if
    unavailable / no key / out of quota. Cached 12h.
    """
    if not API_KEY:
        return {}
    key = _norm(name)
    c = _team_cache.get(key)
    if c and time.time() - c[0] < _TTL:
        return c[1]
    if not _quota_ok():
        return c[1] if c else {}
    tid = _find_team_id(name)
    if not tid:
        return {}
    season = season or dt.date.today().year
    from_date = f"{season}-02-01"
    data = None
    for path in (f"/teams/statistics/{tid}", f"/teams/{tid}/statistics"):
        try:
            data = _get(path, {"fromDate": from_date})
            if data:
                break
        except Exception as e:
            print(f"[highlightly] stats {path} failed for {name}: {e}")
    if data is None:
        return c[1] if c else {}
    stats = _parse_team_stats(data)
    if stats:
        _team_cache[key] = (time.time(), stats)
    return stats


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_team_stats(data):
    """Extract the fields the run-expectancy model needs from Highlightly's
    team statistics payload. Defensive: returns {} if shape is unexpected."""
    # unwrap {data: ...} or [ ... ] wrappers
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return {}
    bat = data.get("batting") or data.get("hitting") or {}
    pit = data.get("pitching") or {}
    games = _num(data.get("games") or data.get("gamesPlayed")) or 0
    runs = _num(bat.get("runs") or bat.get("R"))
    out = {}
    if runs and games:
        out["rpg"] = runs / games
    out["obp"] = _num(bat.get("onBasePercentage") or bat.get("OBP"))
    out["slg"] = _num(bat.get("sluggingPercentage") or bat.get("SLG"))
    out["ops"] = _num(bat.get("onBasePlusSlugging") or bat.get("OPS"))
    out["era"] = _num(pit.get("earnedRunAverage") or pit.get("ERA"))
    # WHIP if components present
    bb = _num(pit.get("walksAllowed") or pit.get("BB"))
    h = _num(pit.get("hitsAllowed") or pit.get("H"))
    ip = _num(pit.get("inningsPitched") or pit.get("IP"))
    if bb is not None and h is not None and ip:
        out["whip"] = (bb + h) / ip
    return {k: v for k, v in out.items() if v is not None}


def get_team_stats_cached(name):
    """Return cached stats only (no network). For use inside hot request paths."""
    c = _team_cache.get(_norm(name))
    return c[1] if c else {}


def _ct_time_from_iso(iso):
    """Format an ISO UTC timestamp as Central time, e.g. '6:30 PM CT'."""
    try:
        utc = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        ct = utc - dt.timedelta(hours=5)
        h = ct.hour % 12 or 12
        return f"{h}:{ct.minute:02d} {'AM' if ct.hour < 12 else 'PM'} CT"
    except Exception:
        return ""


_STATE_MAP = {
    "Finished": "finished", "Final": "finished",
    "In Progress": "live", "Half Time": "live", "Rain Delay": "live",
    "Suspended": "live", "Period End": "live",
    "Scheduled": "scheduled", "Postponed": "scheduled", "Unknown": "scheduled",
    "Canceled": "scheduled", "Abandoned": "finished",
}


def get_games(date, league="NCAA"):
    """
    College baseball games for a date via Highlightly /matches. Returns a list in
    the same shape the site's team renderer expects. Cached 5 min. Empty list on
    any failure (caller falls back). Uses the confirmed API spec:
      GET /matches?league=NCAA&date=YYYY-MM-DD&timezone=America/Chicago
    """
    if not API_KEY:
        return []
    key = f"games:{league}:{date.isoformat()}"
    c = _games_cache.get(key)
    if c and time.time() - c[0] < _GAMES_TTL:
        return c[1]
    if not _quota_ok():
        return c[1] if c else []
    try:
        data = _get("/matches", {"league": league, "date": date.isoformat(),
                                 "timezone": "America/Chicago", "limit": 100})
    except Exception as e:
        print(f"[highlightly] matches failed: {e}")
        return c[1] if c else []
    rows = data.get("data", []) if isinstance(data, dict) else (data or [])
    games = []
    import datetime as _dt
    want = date.isoformat()
    for m in rows:
        try:
            raw = m.get("date", "") or ""
            if raw:
                try:
                    utc = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    ct_date = (utc - _dt.timedelta(hours=5)).date().isoformat()
                    if ct_date != want:
                        continue
                except Exception:
                    pass
            g = _match_to_game(m)
            if g:
                games.append(g)
        except Exception as e:
            print(f"[highlightly] match parse skipped: {e}")
    # only cache non-empty results (an empty cache entry would block real games
    # for the full TTL — same bug class as the ESPN provider)
    if games:
        _games_cache[key] = (time.time(), games)
    return games


def _side_from_team(t, score_block):
    """Build a team side dict from Highlightly team + optional score."""
    runs = None
    if isinstance(score_block, dict):
        innings = score_block.get("innings") or []
        # total runs = sum of innings if present, else parse 'current'
        if innings:
            try:
                runs = sum(int(x) for x in innings if x is not None)
            except (ValueError, TypeError):
                runs = None
    return {
        "team_id": t.get("id"), "name": t.get("displayName") or t.get("name", "Team"),
        "abbr": t.get("abbreviation", ""), "logo": t.get("logo"),
        "record": "", "win_pct": None, "rank": None,
        "location": "", "score": runs,
    }


def _match_to_game(m):
    state = m.get("state", {}) or {}
    desc = state.get("description", "Scheduled")
    status = _STATE_MAP.get(desc, "scheduled")
    score = state.get("score", {}) or {}
    home_t = m.get("homeTeam", {}) or {}
    away_t = m.get("awayTeam", {}) or {}
    h = _side_from_team(home_t, score.get("home"))
    a = _side_from_team(away_t, score.get("away"))
    # parse 'current' like '5 - 8' as a fallback for scores
    cur = score.get("current") or ""
    if (h["score"] is None or a["score"] is None) and "-" in cur:
        try:
            hs, as_ = [int(x.strip()) for x in cur.split("-")[:2]]
            h["score"], a["score"] = hs, as_
        except (ValueError, IndexError):
            pass
    # prediction: use cached run-expectancy stats if present, else strength model
    from ncaa_model import predict_baseball
    pred = predict_baseball(h, a)
    winner = None
    if status == "finished" and h["score"] is not None and a["score"] is not None:
        winner = "home" if h["score"] > a["score"] else "away"
    return {
        "id": str(m.get("id")), "sport": "ncaabb", "status": status,
        "event_time": _ct_time_from_iso(m.get("date", "")),
        "home": h, "away": a,
        "prob_home": pred["prob_home"], "exp_margin": pred["exp_margin"],
        "confidence": pred["confidence"], "avg_total": pred.get("avg_total"),
        "factors": pred.get("factors", []),
        "venue": (m.get("venue", {}) or {}).get("name", "") if isinstance(m.get("venue"), dict) else "",
        "prominence": 1.0,
        "score": {"home": h["score"], "away": a["score"],
                  "detail": state.get("report", "")},
        "winner": winner,
    }


def quota():
    return {"remaining": _remaining["value"],
            "spend_today": _spend.get("count", 0), "cap": _DAILY_MAX}
