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


# ---- Head-to-head season series (NHL teams meet several times a year) --------
_TEAM_SCHED: dict = {}     # (team_id, season) -> [{"opp":id,"won":bool}, ...]
_H2H_BUDGET = [0]          # cap NEW schedule fetches per build so the board can't stall


def _team_schedule(team_id):
    key = (str(team_id), None)
    if key in _TEAM_SCHED:
        return _TEAM_SCHED[key]
    if _H2H_BUDGET[0] <= 0:
        return []                       # out of budget; retried next build (not cached)
    _H2H_BUDGET[0] -= 1
    out = []
    try:
        base = SCOREBOARD.rsplit("/scoreboard", 1)[0]
        data = _get(f"{base}/teams/{team_id}/schedule", {})
        for ev in data.get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            cs = comp.get("competitors", [])
            me = next((c for c in cs if str((c.get("team") or {}).get("id")) == str(team_id)), None)
            opp = next((c for c in cs if str((c.get("team") or {}).get("id")) != str(team_id)), None)
            if not me or not opp:
                continue
            if not (((comp.get("status") or {}).get("type") or {}).get("completed")):
                continue
            out.append({"opp": str((opp.get("team") or {}).get("id")),
                        "won": me.get("winner") is True})
    except Exception:
        out = []
    _TEAM_SCHED[key] = out
    return out


def _season_h2h(home_id, away_id):
    if not home_id or not away_id:
        return None
    try:
        games = [g for g in _team_schedule(home_id) if g["opp"] == str(away_id)]
        if not games:
            return None
        w = sum(1 for x in games if x["won"])
        return {"w": w, "l": len(games) - w, "record": f"{w}-{len(games)-w}",
                "games": len(games), "seasons": 1}
    except Exception:
        return None


def _sc(c):
    s = c.get("score")
    if isinstance(s, dict):
        s = s.get("value", s.get("displayValue"))
    try:
        return int(float(s))
    except Exception:
        return None


def team_profile(team_id, name=None):
    """Honest NHL team profile (record, splits, form, streak, goals for/against)
    from the team's real ESPN schedule."""
    out = {"sport": "nhl", "team_id": str(team_id), "name": name, "score_term": "Goals"}
    try:
        base = SCOREBOARD.rsplit("/scoreboard", 1)[0]
        data = _get(f"{base}/teams/{team_id}/schedule", {})
    except Exception:
        return out
    games = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        cs = comp.get("competitors", [])
        me = next((c for c in cs if str((c.get("team") or {}).get("id")) == str(team_id)), None)
        opp = next((c for c in cs if str((c.get("team") or {}).get("id")) != str(team_id)), None)
        if not me or not opp or not (((comp.get("status") or {}).get("type") or {}).get("completed")):
            continue
        tm = me.get("team") or {}
        if not out.get("name"):
            out["name"] = tm.get("displayName") or tm.get("shortDisplayName")
        out.setdefault("abbr", tm.get("abbreviation"))
        games.append({"won": me.get("winner") is True,
                      "home": me.get("homeAway") == "home",
                      "opp": (opp.get("team") or {}).get("abbreviation") or (opp.get("team") or {}).get("displayName"),
                      "ms": _sc(me), "os": _sc(opp), "date": (ev.get("date", "") or "")[:10]})
    import team_profile_util as TP
    return TP.build(out, games)


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
    is_current = date >= dt.date.today()
    ttl = _LIVE_TTL if force_live else (45 if is_current else _DAY_TTL)
    if c and time.time() - c[0] < ttl:
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
    _H2H_BUDGET[0] = 24        # cap NEW schedule fetches per build so the board can't stall
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
        # best-effort: pull each side's probable/starting goalie + season save%
        # straight from the scoreboard competitor (no extra fetch). No-op if ESPN
        # doesn't carry it; verified live once the season starts.
        for comp_side, sd in ((home, h), (away, a)):
            try:
                probs = comp_side.get("probables") or []
                for pr in probs:
                    ath = pr.get("athlete") or {}
                    pos = ((ath.get("position") or {}) or {}).get("abbreviation", "")
                    if pos and pos != "G":
                        continue
                    sv = None
                    for st in (pr.get("statistics") or []):
                        if str(st.get("name", "")).lower() in ("savepct", "savepercentage", "save%"):
                            sv = st.get("displayValue") or st.get("value")
                    if sv is not None:
                        sd["goalie_sv"] = sv
                        sd["goalie_name"] = ath.get("displayName")
                        break
            except Exception:
                pass
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
            "h2h": _season_h2h(h.get("team_id"), a.get("team_id")),
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
