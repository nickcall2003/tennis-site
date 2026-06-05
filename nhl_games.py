"""
nhl_games.py — NHL provider (mirrors ncaab_baseball.py).

DATA SOURCE: ESPN free hidden API = backbone scoreboard (both teams, records,
live score, status, venue) for the date — same pattern as the NCAABB provider.
Team strength (goals-for/against per game) comes from nhl_provider, which reads
a local file refreshed from the NHL public API. The xG/Poisson prediction
degrades gracefully to a records fallback when stats aren't loaded.
"""
from __future__ import annotations

import datetime as dt
import time

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"
SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary"

_cache = {}          # date -> (ts, [games])
_DAY_TTL = 300        # 5 min
_LIVE_TTL = 30


def _get(url, params=None):
    import httpx
    r = httpx.get(url, params=params or {}, timeout=8.0)
    r.raise_for_status()
    return r.json()


def _record_winpct(team):
    """NHL records are W-L-OTL. Use standard points percentage:
    points = 2*W + OTL, max = 2*GP. Falls back to W/(W+L) if only two parts."""
    for rec in team.get("records", []) or []:
        summ = rec.get("summary", "")
        if "-" in summ:
            try:
                parts = [int(x) for x in summ.split("-")[:3]]
                if len(parts) >= 3:
                    w, l, otl = parts[0], parts[1], parts[2]
                    gp = w + l + otl
                    if gp > 0:
                        return (2 * w + otl) / (2 * gp), summ
                else:
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
    if c and not force_live and time.time() - c[0] < _DAY_TTL:
        return c[1]
    ds = date.strftime("%Y%m%d")
    nxt = (date + dt.timedelta(days=1)).strftime("%Y%m%d")
    prv = (date - dt.timedelta(days=1)).strftime("%Y%m%d")
    attempts = [
        {"dates": ds, "limit": 400},
        {"dates": f"{prv}-{nxt}", "limit": 400},
    ]
    data = None
    for params in attempts:
        try:
            resp = _get(SCOREBOARD, params)
            if resp.get("events"):
                data = resp
                break
            data = data or resp
        except Exception as e:
            print(f"[nhl] scoreboard attempt {params} failed: {e}")
    if data is None:
        return _cache.get(key, (0, []))[1]
    games = []
    want = date.isoformat()
    for ev in data.get("events", []):
        raw = ev.get("date", "") or ""
        if raw:
            try:
                u = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
                ct_date = (u - dt.timedelta(hours=5)).date().isoformat()
                if ct_date != want:
                    continue
            except Exception:
                continue
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
        try:
            from nhl_model import predict_hockey
            pred = predict_hockey(h, a)
        except Exception as e:
            print(f"[nhl] predict failed for {h.get('name')} vs {a.get('name')}: {e}")
            pred = {"prob_home": 0.5, "exp_margin": None, "confidence": "low",
                    "avg_total": None, "factors": []}
        prominence = (h["win_pct"] or 0.5) + (a["win_pct"] or 0.5)
        games.append({
            "id": ev.get("id"), "sport": "nhl", "status": status,
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
    if not games:
        del _cache[key]
    return games
