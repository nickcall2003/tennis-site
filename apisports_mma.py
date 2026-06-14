"""
apisports_mma.py — API-Sports.io MMA client (https://v1.mma.api-sports.io).

USED ONLY FOR MMA. The free plan is 100 requests/day, so every call is:
  * cached aggressively (the card roster doesn't change; fighter stats change
    only after a fight),
  * counted against a hard daily cap (default 90, leaving headroom), and
  * never issued on the high-frequency live-polling path — live scores/status
    come from ESPN (free); API-Sports is reserved for the card + the deep
    fighter stats that power the "why this fighter wins" detail.

Auth: header `x-apisports-key`. Envelope: {response:[...], errors:[...]}.
Set the key in env as APISPORTS_MMA_KEY (aliases also accepted).
"""
from __future__ import annotations

import datetime as dt
import os
import time

KEY = (os.environ.get("APISPORTS_MMA_KEY")
       or os.environ.get("API_SPORTS_MMA_KEY")
       or os.environ.get("APISPORTS_KEY")
       or os.environ.get("API_MMA_KEY") or "").strip()
BASE = "https://v1.mma.api-sports.io"

_DAILY_CAP = int(os.environ.get("APISPORTS_MMA_DAILY_MAX", "90"))
_TTL_CARD = int(os.environ.get("APISPORTS_MMA_CARD_TTL", "900"))      # 15 min
_TTL_FIGHTER = int(os.environ.get("APISPORTS_MMA_FIGHTER_TTL", "86400"))  # 24 h

_spend = {"day": None, "count": 0}
_cache = {}                       # path|params -> (ts, response_list)
_last = {"remaining": None, "errors": None}


def enabled() -> bool:
    return bool(KEY)


def _today_utc():
    return dt.datetime.utcnow().date().isoformat()


def _spend_ok() -> bool:
    if _spend["day"] != _today_utc():
        _spend["day"] = _today_utc()
        _spend["count"] = 0
    return _spend["count"] < _DAILY_CAP


def _spend_one():
    if _spend["day"] != _today_utc():
        _spend["day"] = _today_utc()
        _spend["count"] = 0
    _spend["count"] += 1


def _ckey(path, params):
    return path + "|" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))


def _get(path, params=None, ttl=600):
    """Cached, quota-guarded GET. Returns the `response` list (or [])."""
    if not KEY:
        return []
    ck = _ckey(path, params)
    c = _cache.get(ck)
    if c and time.time() - c[0] < ttl:
        return c[1]
    if not _spend_ok():
        return c[1] if c else []          # out of daily budget -> serve stale/empty
    try:
        import httpx
        r = httpx.get(BASE + path, params=params or {},
                      headers={"x-apisports-key": KEY}, timeout=15)
        _spend_one()
        _last["remaining"] = r.headers.get("x-ratelimit-requests-remaining")
        r.raise_for_status()
        body = r.json()
        _last["errors"] = body.get("errors") or None
        resp = body.get("response") or []
        _cache[ck] = (time.time(), resp)
        return resp
    except Exception as e:
        print(f"[apisports-mma] {path} failed: {e}")
        return c[1] if c else []


# -------------------------------- endpoints --------------------------------

def get_card(date: dt.date):
    """All fights on a date. One cached call powers the whole board."""
    return _get("/fights", {"date": date.isoformat()}, ttl=_TTL_CARD)


def get_fights_season(season: int):
    return _get("/fights", {"season": str(season)}, ttl=_TTL_CARD)


def get_fighter(fighter_id):
    """Fighter profile + career stats/record (cached 24h)."""
    if not fighter_id:
        return None
    rows = _get("/fighters", {"id": str(fighter_id)}, ttl=_TTL_FIGHTER)
    return rows[0] if rows else None


def get_fight_statistics(fight_id):
    """Per-fighter statistics for a single fight (cached 24h)."""
    if not fight_id:
        return []
    return _get("/fights/statistics/fighters", {"fight": str(fight_id)},
                ttl=_TTL_FIGHTER)


def raw_get(path, params=None):
    """Full API-Sports envelope (response + errors + parameters) for inspecting
    field shapes. Quota-counted, lightly cached (5 min)."""
    if not KEY:
        return {"error": "no APISPORTS_MMA_KEY set in environment"}
    ck = "RAW|" + _ckey(path, params)
    c = _cache.get(ck)
    if c and time.time() - c[0] < 300:
        return c[1]
    if not _spend_ok():
        return {"error": "daily cap reached", **diag()}
    try:
        import httpx
        r = httpx.get(BASE + path, params=params or {},
                      headers={"x-apisports-key": KEY}, timeout=15)
        _spend_one()
        body = r.json()
        _cache[ck] = (time.time(), body)
        return body
    except Exception as e:
        return {"error": str(e)}


def diag():
    if _spend["day"] != _today_utc():
        _spend["count"] = 0
    return {
        "enabled": bool(KEY),
        "daily_used": _spend["count"],
        "daily_cap": _DAILY_CAP,
        "remaining_today": max(0, _DAILY_CAP - _spend["count"]),
        "rate_remaining_header": _last["remaining"],
        "last_errors": _last["errors"],
        "cached_keys": len(_cache),
    }
