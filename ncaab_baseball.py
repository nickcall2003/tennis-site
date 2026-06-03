"""
ncaab_baseball.py — College Baseball (NCAA D1) provider.

DATA SOURCES (honest scope):
- ESPN free hidden API = backbone: scoreboard (both teams, records, live score,
  status, venue) for the date. This is the same pattern as NBA/NFL. ESPN's
  college baseball coverage is strongest for major conferences; some mid-major
  games may be missing — that's a free-data limitation, not a bug.
- Warren Nolan = OPTIONAL enrichment for RPI / ELO / strength-of-schedule, which
  ESPN does not expose. Warren Nolan has NO API, so this is a cached, low-volume,
  attributed read of their public ratings table. RPI matters a lot in college
  baseball because schedule strength varies enormously between teams.

The prediction blends an ESPN-record Elo with the RPI/ELO signal when available,
and degrades gracefully (records only) when it isn't.
"""
from __future__ import annotations

import datetime as dt
import time

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/baseball/college-baseball/scoreboard"
SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/baseball/college-baseball/summary"
RANKINGS = "https://site.api.espn.com/apis/site/v2/sports/baseball/college-baseball/rankings"

_cache = {}          # date -> (ts, [games])
_DAY_TTL = 6 * 3600
_LIVE_TTL = 30


def _get(url, params=None):
    import httpx
    r = httpx.get(url, params=params or {}, timeout=20.0)
    r.raise_for_status()
    return r.json()


def _record_winpct(team):
    for rec in team.get("records", []) or []:
        summ = rec.get("summary", "")
        if "-" in summ:
            try:
                parts = [int(x) for x in summ.split("-")[:2]]
                w, l = parts[0], parts[1]
                if w + l > 0:
                    return w / (w + l), summ
            except (ValueError, IndexError):
                continue
    return None, ""


def _status(comp):
    st = ((comp.get("status") or {}).get("type") or {})
    state = st.get("state", "")
    if state == "post":
        return "finished"
    if state == "in":
        return "live"
    return "scheduled"


def _ct_time(iso):
    try:
        utc = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        ct = utc - dt.timedelta(hours=5)
        h = ct.hour % 12 or 12
        return f"{h}:{ct.minute:02d} {'AM' if ct.hour < 12 else 'PM'} CT"
    except Exception:
        return ""


def _to_int(v):
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _side(competitor):
    t = competitor.get("team", {}) or {}
    wp, rec = _record_winpct(competitor)
    logo = t.get("logo")
    if not logo:
        logos = t.get("logos") or []
        logo = logos[0]["href"] if logos else None
    rank = None
    cr = competitor.get("curatedRank") or {}
    if cr.get("current") and cr["current"] != 99:
        rank = cr["current"]
    return {
        "team_id": t.get("id"), "name": t.get("displayName", "Team"),
        "abbr": t.get("abbreviation", ""), "logo": logo,
        "record": rec, "win_pct": wp, "rank": rank,
        "location": t.get("location", ""),
        "score": _to_int(competitor.get("score")),
    }


def get_games(date: dt.date, force_live=False):
    key = date.isoformat()
    c = _cache.get(key)
    ttl = _LIVE_TTL if force_live else _DAY_TTL
    if c and not force_live and time.time() - c[0] < _DAY_TTL:
        return c[1]
    try:
        data = _get(SCOREBOARD, {"dates": date.strftime("%Y%m%d"),
                                 "groups": "50", "limit": 400})
    except Exception as e:
        print(f"[ncaabb] scoreboard failed: {e}")
        return _cache.get(key, (0, []))[1]
    games = []
    for ev in data.get("events", []):
        comps = ev.get("competitions", [])
        if not comps:
            continue
        comp = comps[0]
        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        h, a = _side(home), _side(away)
        status = _status(comp)
        venue = (comp.get("venue", {}) or {}).get("fullName", "")
        st = ((comp.get("status") or {}).get("type") or {})
        # prediction blends record-Elo with Warren Nolan RPI/ELO when available
        from ncaa_model import predict_baseball
        pred = predict_baseball(h, a)
        prominence = (h["win_pct"] or 0.5) + (a["win_pct"] or 0.5)
        if h["rank"]:
            prominence += 0.5
        if a["rank"]:
            prominence += 0.5
        games.append({
            "id": ev.get("id"), "sport": "ncaabb", "status": status,
            "event_time": _ct_time(ev.get("date", "")),
            "home": h, "away": a,
            "prob_home": pred["prob_home"], "exp_margin": pred["exp_margin"],
            "confidence": pred["confidence"], "avg_total": pred.get("avg_total"),
            "factors": pred.get("factors", []),
            "venue": venue, "prominence": prominence,
            "score": {"home": h["score"], "away": a["score"],
                      "detail": st.get("shortDetail", "")},
            "winner": ("home" if (status == "finished" and (h["score"] or 0) > (a["score"] or 0))
                       else "away" if status == "finished" else None),
        })
    _cache[key] = (time.time(), games)
    return games
