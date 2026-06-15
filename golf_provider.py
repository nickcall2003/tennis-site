"""
golf_provider.py — golf data from ESPN (free, reachable from Railway).

Golf is a field sport, not head-to-head: the "board" is a leaderboard (the whole
field ranked by score to par), with per-player round scores, position, holes
played, tee times and cut status. This module pulls the current/most-recent
tournament for a tour plus the season schedule, and normalizes each player into
a leaderboard row the UI and (later) the projection engine can use.

Tours: pga, lpga, champions-tour, eur (DP World), liv, ntw (Korn Ferry).
ESPN endpoint: site.api.espn.com/apis/site/v2/sports/golf/{tour}/scoreboard
"""
from __future__ import annotations

import os
import time

BASE = "https://site.api.espn.com/apis/site/v2/sports/golf"
_TTL_LIVE = int(os.environ.get("GOLF_TTL", "30"))      # leaderboard refresh
_TTL_SCHED = 3600
_UA = "Mozilla/5.0"
_cache = {}

TOURS = {
    "pga": "PGA Tour", "lpga": "LPGA", "eur": "DP World Tour",
    "champions-tour": "PGA Tour Champions", "liv": "LIV Golf",
    "ntw": "Korn Ferry Tour",
}
DEFAULT_TOUR = "pga"


def _get(url, ttl):
    c = _cache.get(url)
    if c and time.time() - c[0] < ttl:
        return c[1]
    try:
        import httpx
        r = httpx.get(url, headers={"User-Agent": _UA}, timeout=15, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
        _cache[url] = (time.time(), data)
        return data
    except Exception as e:
        print(f"[golf] GET failed {url}: {e}")
        return c[1] if c else None


def _num_to_par(v):
    """'-12'/'E'/'+3'/-12 -> int (E=0), or None."""
    if v is None:
        return None
    s = str(v).strip().upper()
    if s in ("E", "EVEN", "0"):
        return 0
    try:
        return int(s.replace("+", ""))
    except Exception:
        try:
            return int(float(s))
        except Exception:
            return None


def _fmt_par(n):
    if n is None:
        return "—"
    return "E" if n == 0 else (f"+{n}" if n > 0 else str(n))


def _g(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def _player_row(c):
    ath = c.get("athlete") or c.get("competitor") or {}
    st = c.get("status") or {}
    pos = st.get("position") or {}
    flag = ath.get("flag") or {}
    head = ath.get("headshot") or {}

    # score to par: try several spots ESPN uses
    total_num = _num_to_par(_g(c, "score"))
    if total_num is None:
        total_num = _num_to_par(_g(st, "displayValue"))
    if total_num is None:
        for s in (c.get("statistics") or []):
            if (s.get("name") or "").lower() in ("scoretopar", "topar", "score"):
                total_num = _num_to_par(s.get("displayValue") or s.get("value"))
                break

    rounds = []
    for ls in (c.get("linescores") or []):
        rounds.append(ls.get("displayValue") or ls.get("value"))

    pos_txt = str(pos.get("displayName") or pos.get("abbreviation") or "").strip()
    status_type = ((c.get("status") or {}).get("type") or {})
    state = (status_type.get("name") or status_type.get("state") or "").lower()
    made_cut = None
    low = pos_txt.lower()
    if "cut" in low or "mc" == low:
        made_cut = False
    elif "wd" in low or "withdraw" in low:
        made_cut = None

    return {
        "id": str(ath.get("id") or ""),
        "name": ath.get("displayName") or ath.get("shortName") or "",
        "country": flag.get("alt") or "",
        "flag": flag.get("href") or "",
        "headshot": head.get("href") or "",
        "pos": pos_txt or "—",
        "pos_num": _num_to_par(pos_txt.replace("T", "")) if pos_txt and pos_txt[-1:].isdigit() else None,
        "total": _fmt_par(total_num),
        "total_num": total_num,
        "today": _g(st, "displayValue", default=None) if False else (st.get("today") if "today" in st else None),
        "thru": st.get("thru") if st.get("thru") is not None else st.get("holesPlayed"),
        "tee_time": st.get("teeTime") or st.get("startTime"),
        "rounds": rounds,
        "amateur": bool(ath.get("amateur")),
        "made_cut": made_cut,
        "state": state,
    }


def _current_event(data):
    """Pick the in-progress event, else the most recent/next from events[]."""
    evs = (data or {}).get("events") or []
    if not evs:
        return None
    for e in evs:
        comp = (e.get("competitions") or [{}])[0]
        state = (((comp.get("status") or {}).get("type") or {}).get("state") or "").lower()
        if state == "in":
            return e
    return evs[0]


def get_board(tour=DEFAULT_TOUR):
    tour = tour if tour in TOURS else DEFAULT_TOUR
    data = _get(f"{BASE}/{tour}/scoreboard", _TTL_LIVE)
    e = _current_event(data)
    if not e:
        return {"tour": tour, "event": None, "players": [], "cut_line": None}
    comp = (e.get("competitions") or [{}])[0]
    status_type = ((comp.get("status") or {}).get("type") or {})
    state = (status_type.get("state") or "").lower()
    players = [_player_row(c) for c in (comp.get("competitors") or [])]
    # rank: by total to par (None last)
    players.sort(key=lambda p: (p["total_num"] is None, p["total_num"] if p["total_num"] is not None else 999))
    course = ""
    courses = comp.get("courses") or e.get("courses") or []
    if courses:
        course = courses[0].get("name") or courses[0].get("shortName") or ""
    event = {
        "id": str(e.get("id") or ""),
        "name": e.get("name") or e.get("shortName") or TOURS[tour],
        "short": e.get("shortName") or e.get("name") or "",
        "course": course,
        "purse": comp.get("purse") or e.get("purse"),
        "round": (comp.get("status") or {}).get("period"),
        "status": status_type.get("description") or status_type.get("detail") or "",
        "is_live": state == "in",
        "is_complete": bool(status_type.get("completed")) or state == "post",
        "start": e.get("date"),
        "end": e.get("endDate"),
    }
    cut = comp.get("cutLine") or (comp.get("status") or {}).get("cutLine")
    return {"tour": tour, "event": event, "players": players, "cut_line": cut,
            "field_size": len(players)}


def get_schedule(tour=DEFAULT_TOUR):
    tour = tour if tour in TOURS else DEFAULT_TOUR
    data = _get(f"{BASE}/{tour}/scoreboard", _TTL_SCHED)
    cal = (((data or {}).get("leagues") or [{}])[0]).get("calendar") or []
    out = []
    for c in cal:
        out.append({"id": str(c.get("id") or ""), "name": c.get("label") or "",
                    "start": c.get("startDate"), "end": c.get("endDate")})
    return {"tour": tour, "events": out}


def raw(tour=DEFAULT_TOUR):
    """Trimmed raw current-event competition for locking field names."""
    data = _get(f"{BASE}/{tour}/scoreboard", _TTL_LIVE)
    e = _current_event(data)
    if not e:
        return {"error": "no current event", "events_seen": len((data or {}).get("events") or [])}
    comp = (e.get("competitions") or [{}])[0]
    comps = comp.get("competitors") or []
    return {
        "event_keys": sorted(list(e.keys())),
        "competition_keys": sorted(list(comp.keys())),
        "status": comp.get("status"),
        "n_competitors": len(comps),
        "sample_competitor": comps[0] if comps else None,
    }
