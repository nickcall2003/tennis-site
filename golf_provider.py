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

import datetime as dt
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
    ath = c.get("athlete") or {}
    flag = ath.get("flag") or {}
    head = ath.get("headshot") or {}
    total_num = _num_to_par(c.get("score"))

    ls = sorted([l for l in (c.get("linescores") or []) if l.get("period")],
                key=lambda l: l.get("period") or 0)
    rounds = []
    for l in ls:
        v = l.get("value")
        rounds.append(int(v) if isinstance(v, (int, float)) else v)
    cur = ls[-1] if ls else None
    today_par = _num_to_par(cur.get("displayValue")) if cur else None
    holes = len(cur.get("linescores") or []) if cur else 0
    tee = None
    if cur:
        cats = (cur.get("statistics") or {}).get("categories") or []
        if cats:
            stats = cats[0].get("stats") or []
            if stats and isinstance(stats[-1], dict):
                tee = stats[-1].get("displayValue")

    return {
        "id": str(ath.get("id") or ""),
        "name": ath.get("displayName") or ath.get("fullName") or "",
        "country": flag.get("alt") or "",
        "flag": flag.get("href") or "",
        "headshot": head.get("href") or "",
        "order": c.get("order"),
        "total": _fmt_par(total_num),
        "total_num": total_num,
        "today": _fmt_par(today_par) if today_par is not None else None,
        "today_num": today_par,
        "holes": holes,
        "rounds": rounds,
        "n_rounds": len([r for r in rounds if r not in (None, "")]),
        "tee": fmt_tee(tee),
        "amateur": bool(ath.get("amateur")),
    }


def _next_event(data):
    """Next not-yet-started event from the season calendar (for off-days)."""
    cal = (((data or {}).get("leagues") or [{}])[0]).get("calendar") or []
    now = dt.datetime.now(dt.timezone.utc)
    best = None
    for c in cal:
        s = c.get("startDate")
        if not s:
            continue
        try:
            t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=dt.timezone.utc)
        except Exception:
            continue
        if t > now and (best is None or t < best[0]):
            best = (t, c)
    if not best:
        return None
    c = best[1]
    return {"id": str(c.get("id") or ""), "name": c.get("label") or "",
            "start": c.get("startDate")}


def fmt_tee(s):
    """'Sun Jun 14 12:42:00 PDT 2026' -> 'Sun 12:42 PM'. Best-effort, never raises."""
    if not s:
        return None
    try:
        parts = str(s).split()
        if len(parts) >= 4:
            dow = parts[0]
            hh, mm, _ = parts[3].split(":")
            h = int(hh)
            ap = "AM" if h < 12 else "PM"
            h12 = h % 12 or 12
            return f"{dow} {h12}:{mm} {ap}"
    except Exception:
        pass
    return str(s)


def _current_event(data):
    """Pick the in-progress event, else the first event listed (current/most recent)."""
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
    from collections import Counter
    tour = tour if tour in TOURS else DEFAULT_TOUR
    data = _get(f"{BASE}/{tour}/scoreboard", _TTL_LIVE)
    e = _current_event(data)
    if not e:
        return {"tour": tour, "event": None, "players": [], "cut_line": None,
                "next_event": _next_event(data)}
    comp = (e.get("competitions") or [{}])[0]
    status_type = ((comp.get("status") or {}).get("type") or {})
    state = (status_type.get("state") or "").lower()
    ev_round = (comp.get("status") or {}).get("period") or 0
    is_complete = bool(status_type.get("completed")) or state == "post"

    players = [_player_row(c) for c in (comp.get("competitors") or [])]
    players.sort(key=lambda p: (p.get("order") is None, p.get("order") or 1e9,
                                p["total_num"] is None, p["total_num"] if p["total_num"] is not None else 999))

    # cut heuristic: weekend rounds exist once the cut has happened (R3+)
    for p in players:
        if ev_round and ev_round >= 3:
            p["made_cut"] = p["n_rounds"] >= 3
        else:
            p["made_cut"] = None
        # thru/today only meaningful while playing
        if is_complete:
            p["thru"] = "F"
            p["today"] = None
        elif p["made_cut"] is False:
            p["thru"] = "CUT"
        else:
            p["thru"] = "F" if (p["holes"] >= 18 or p["holes"] == 0) else str(p["holes"])

    # display positions (T-ties) among players still in
    active = [p for p in players if p.get("made_cut") in (True, None)]
    counts = Counter(p["total_num"] for p in active if p["total_num"] is not None)
    prev, posnum = object(), 0
    for idx, p in enumerate(active):
        if p["total_num"] != prev:
            posnum = idx + 1
            prev = p["total_num"]
        tie = p["total_num"] is not None and counts[p["total_num"]] > 1
        p["pos"] = ("T" if tie else "") + str(posnum)
        p["pos_num"] = posnum
    for p in players:
        if p.get("made_cut") is False:
            p["pos"] = "CUT"
            p["pos_num"] = None

    # computed cut line: worst score that still made the weekend (R3+)
    cut_line = cut_made = None
    if ev_round and ev_round >= 3:
        mc = [p["total_num"] for p in players if p.get("made_cut") and p["total_num"] is not None]
        if mc:
            cut_line = _fmt_par(max(mc))
            cut_made = len(mc)

    event = {
        "id": str(e.get("id") or ""),
        "name": e.get("name") or e.get("shortName") or TOURS[tour],
        "short": e.get("shortName") or e.get("name") or "",
        "round": ev_round,
        "status": status_type.get("description") or status_type.get("detail") or "",
        "is_live": state == "in",
        "is_complete": is_complete,
        "start": e.get("date"),
        "end": e.get("endDate"),
    }
    return {"tour": tour, "tour_name": TOURS[tour], "event": event,
            "players": players, "field_size": len(players),
            "cut_line": cut_line, "cut_made": cut_made,
            "next_event": _next_event(data)}


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
