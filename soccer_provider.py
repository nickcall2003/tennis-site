"""
soccer_provider.py — multi-league soccer via ESPN's hidden API.

Soccer is one sport with MANY leagues (Premier League, La Liga, Champions
League, MLS, World Cup, ...), so it works like tennis's tournament picker: the
caller passes a league key and we hit that league's ESPN endpoint. The
scoreboard/summary JSON is the same shape ESPN uses for every other sport, so we
reuse espn_provider's parsing helpers and add the soccer-specific bits:

  * a running clock that counts UP ("67'", "45'+2'")
  * a THREE-way result probability (home / draw / away) from a double-Poisson
    goals model, computed pregame from team strength and live from the current
    score + minute (recomputed on every refresh, like the other live sports)
  * goal/card events for the match timeline
  * a team-stat comparison (possession, shots, corners...) for Live Stats

No API key needed (ESPN public endpoints). Network failures degrade to [].
"""

import datetime as dt
import math
import time

import espn_provider as E   # reuse _get/_side/_status/_ct_time/_to_int/_record_winpct

# key, ESPN slug, display label  (order = US-facing popularity)
LEAGUES = [
    ("epl",          "eng.1",            "Premier League"),
    ("ucl",          "uefa.champions",   "Champions League"),
    ("laliga",       "esp.1",            "La Liga"),
    ("mls",          "usa.1",            "MLS"),
    ("seriea",       "ita.1",            "Serie A"),
    ("bundesliga",   "ger.1",            "Bundesliga"),
    ("ligue1",       "fra.1",            "Ligue 1"),
    ("uel",          "uefa.europa",      "Europa League"),
    ("uecl",         "uefa.europa.conf", "Conference League"),
    ("worldcup",     "fifa.world",       "World Cup"),
    ("ligamx",       "mex.1",            "Liga MX"),
    ("championship", "eng.2",            "EFL Championship"),
    ("eredivisie",   "ned.1",            "Eredivisie"),
    ("ligaportugal", "por.1",            "Liga Portugal"),
    ("saudi",        "ksa.1",            "Saudi Pro League"),
]
_SLUG = {k: slug for (k, slug, _l) in LEAGUES}
_LABEL = {k: lab for (k, _s, lab) in LEAGUES}
DEFAULT_LEAGUE = "epl"

_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard"
_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/summary"

_cache = {}        # (league, date) -> (ts, [games])
_DAY_TTL = 6 * 3600
_LIVE_TTL = 25


def leagues():
    return [{"key": k, "label": lab} for (k, _s, lab) in LEAGUES]


def _has_live(games):
    return any(g.get("status") == "live" for g in games)


# ---- running clock -> elapsed minutes (for the live model) ----
def _minute(display_clock, period):
    """Best-effort elapsed minutes from ESPN's soccer clock. '67'' -> 67,
    "45'+2'" -> 47. Falls back to period boundaries."""
    if display_clock:
        s = str(display_clock).replace("'", "").strip()
        try:
            if "+" in s:
                a, b = s.split("+", 1)
                return int(float(a)) + int(float(b or 0))
            return int(float(s))
        except (ValueError, TypeError):
            pass
    # fall back: 2nd half -> 45+, otherwise 0
    try:
        return 45 if int(period or 0) >= 2 else 0
    except (ValueError, TypeError):
        return 0


# ---- expected goals (full match) from team strengths ----
def _exp_goals(sh, sa):
    sh = 0.5 if sh is None else sh
    sa = 0.5 if sa is None else sa
    tilt = max(-1.0, min(1.0, (sh - sa)))
    base = 2.7 / 2.0                       # ~2.7 goals/match split two ways
    return base * (1 + 0.55 * tilt) * 1.10, base * (1 - 0.55 * tilt) * 0.90   # home edge


def _imp(o):
    """Implied probability from American odds."""
    if o is None:
        return None
    try:
        o = float(o)
    except (TypeError, ValueError):
        return None
    return 100.0 / (o + 100) if o > 0 else (-o) / ((-o) + 100)


def _parse_odds(comp):
    """ESPN 3-way moneylines from a competition, if present."""
    od = comp.get("odds") or []
    if not od:
        return None
    o = od[0] or {}
    def _ml(d):
        v = (d or {}).get("moneyLine")
        return v if isinstance(v, (int, float)) else None
    ho, ao, do = _ml(o.get("homeTeamOdds")), _ml(o.get("awayTeamOdds")), _ml(o.get("drawOdds"))
    if ho is None and isinstance(o.get("homeMoneyLine"), (int, float)):
        ho = o["homeMoneyLine"]
    if ao is None and isinstance(o.get("awayMoneyLine"), (int, float)):
        ao = o["awayMoneyLine"]
    if do is None and isinstance(o.get("drawMoneyLine"), (int, float)):
        do = o["drawMoneyLine"]
    return {"ml_home": ho, "ml_away": ao, "ml_draw": do} if (ho is not None or ao is not None) else None


# ---- double-Poisson 3-way result probability ----
def _winprob(sh, sa, ch, ca, minute, live):
    """sh/sa: home/away strength in [0,1] (win%); ch/ca: current goals;
    minute: elapsed; live: in-progress. Returns (p_home, p_draw, p_away)."""
    lam_h_full, lam_a_full = _exp_goals(sh, sa)
    if live:
        rem = max(0.0, (95.0 - minute)) / 90.0     # ~5' stoppage cushion
    else:
        rem = 1.0
    lh, la = lam_h_full * rem, lam_a_full * rem
    P = 12
    ph = pd = pa = 0.0
    poh = [math.exp(-lh) * lh ** k / math.factorial(k) for k in range(P)]
    poa = [math.exp(-la) * la ** k / math.factorial(k) for k in range(P)]
    for x in range(P):
        for y in range(P):
            p = poh[x] * poa[y]
            fh, fa = ch + x, ca + y
            if fh > fa:
                ph += p
            elif fh == fa:
                pd += p
            else:
                pa += p
    s = ph + pd + pa or 1.0
    return ph / s, pd / s, pa / s


def _events_from_details(comp):
    """Goals & cards from a scoreboard competition's details[] (when present)."""
    out = []
    for d in (comp.get("details") or []):
        typ = ((d.get("type") or {}).get("text") or "").strip()
        low = typ.lower()
        kind = ("goal" if (d.get("scoringPlay") or "goal" in low) else
                "red" if "red" in low else
                "yellow" if "yellow" in low else
                "sub" if "substitution" in low else None)
        if not kind:
            continue
        who = ", ".join(a.get("displayName", "") for a in (d.get("athletesInvolved") or []))
        out.append({
            "kind": kind, "type": typ,
            "clock": ((d.get("clock") or {}).get("displayValue") or "").strip(),
            "team_id": str((d.get("team") or {}).get("id") or ""),
            "player": who,
        })
    return out


def _build(ev, league):
    comps = ev.get("competitions", [])
    if not comps:
        return None
    comp = comps[0]
    cs = comp.get("competitors", [])
    home = next((c for c in cs if c.get("homeAway") == "home"), None)
    away = next((c for c in cs if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None
    h, a = E._side(home), E._side(away)
    status = E._status(comp)
    status_obj = comp.get("status") or {}
    st = (status_obj.get("type") or {})
    clock = status_obj.get("displayClock")
    period = status_obj.get("period")
    live = status == "live"
    minute = _minute(clock, period) if live else (95 if status == "finished" else 0)
    ch = h["score"] or 0
    ca = a["score"] or 0
    # de-vigged market 3-way when ESPN carries odds (differentiates matches);
    # otherwise a neutral Poisson prior (same for all — no ratings without odds)
    odds = _parse_odds(comp)
    mph = mpd = mpa = None
    if odds and odds.get("ml_home") is not None and odds.get("ml_away") is not None:
        ih = _imp(odds["ml_home"]) or 0.0
        ia = _imp(odds["ml_away"]) or 0.0
        idr = _imp(odds.get("ml_draw")) or 0.0
        tot = ih + idr + ia
        if tot > 0:
            mph, mpd, mpa = ih / tot, idr / tot, ia / tot
    if mph is not None and (mph + mpa) > 0:
        sh, sa = mph / (mph + mpa), mpa / (mph + mpa)      # strength from market
    else:
        sh, sa = h["win_pct"], a["win_pct"]
    if live:
        ph, pd, pa = _winprob(sh, sa, ch, ca, minute, True)
    elif mph is not None:
        ph, pd, pa = mph, mpd, mpa                          # pregame: trust market
    else:
        ph, pd, pa = _winprob(sh, sa, 0, 0, 0, False)
    top = max(ph, pd, pa)
    conf = "high" if top >= 0.6 else "medium" if top >= 0.45 else "low"
    home_id = str((home.get("team") or {}).get("id") or "")
    away_id = str((away.get("team") or {}).get("id") or "")
    events = _events_from_details(comp)
    for e in events:
        e["side"] = ("home" if e["team_id"] == home_id else
                     "away" if e["team_id"] == away_id else None)
    venue = (comp.get("venue", {}) or {}).get("fullName", "")
    eh, ea = _exp_goals(sh, sa)
    return {
        "id": ev.get("id"), "sport": "soccer", "league": league,
        "league_label": _LABEL.get(league, league),
        "status": status, "event_time": E._ct_time(ev.get("date", "")),
        "kickoff_iso": ev.get("date", ""),
        "home": h, "away": a,
        "home_id": home_id, "away_id": away_id,
        "prob_home": round(ph, 4), "prob_draw": round(pd, 4), "prob_away": round(pa, 4),
        "exp_goals_home": round(eh, 2), "exp_goals_away": round(ea, 2),
        "confidence": conf, "odds": odds,
        "clock": clock, "minute": minute, "period": period,
        "score": {"home": ch, "away": ca, "detail": st.get("shortDetail", ""),
                  "clock": clock, "period": period},
        "events": events, "venue": venue,
        "winner": ("home" if (status == "finished" and ch > ca)
                   else "away" if (status == "finished" and ca > ch)
                   else "draw" if status == "finished" else None),
        "prominence": (h["win_pct"] or 0.5) + (a["win_pct"] or 0.5),
    }


def get_today(date: dt.date, force_live=False):
    """Aggregate across ALL leagues — the default 'what's live today' board.
    Live matches first, then upcoming by prominence, then finished. Each game
    keeps its league/league_label so the board can badge it."""
    key = ("__all__", date.isoformat())
    c = _cache.get(key)
    ttl = _LIVE_TTL if (c and _has_live(c[1])) else _DAY_TTL
    if c and not force_live and time.time() - c[0] < ttl:
        return c[1]
    allg = []
    for (k, _slug, _lab) in LEAGUES:
        try:
            allg += get_games(date, k, force_live=force_live)
        except Exception as ex:
            print(f"[soccer] {k} today failed: {ex}")
    order = {"live": 0, "scheduled": 1, "finished": 2}
    allg.sort(key=lambda g: (order.get(g["status"], 3), -g["prominence"]))
    _cache[key] = (time.time(), allg)
    return allg


def get_games(date: dt.date, league: str = DEFAULT_LEAGUE, force_live=False):
    league = league if league in _SLUG else DEFAULT_LEAGUE
    key = (league, date.isoformat())
    c = _cache.get(key)
    ttl = _LIVE_TTL if (c and _has_live(c[1])) else _DAY_TTL
    if c and not force_live and time.time() - c[0] < ttl:
        return c[1]
    try:
        data = E._get(_SCOREBOARD.format(slug=_SLUG[league]),
                      {"dates": date.strftime("%Y%m%d")})
    except Exception:
        return c[1] if c else []
    games = []
    for ev in data.get("events", []):
        try:
            g = _build(ev, league)
            if g:
                games.append(g)
        except Exception as ex:
            print(f"[soccer] build failed: {ex}")
    games.sort(key=lambda g: (g["status"] != "live", -g["prominence"]))
    _cache[key] = (time.time(), games)
    return games


_today_cache = {}     # date -> (ts, [games])


def get_today(date: dt.date):
    """Whatever's on across ALL leagues today — live first, then by kickoff.
    This is the soccer landing view. Aggregates the per-league caches."""
    key = date.isoformat()
    c = _today_cache.get(key)
    if c:
        ttl = _LIVE_TTL if _has_live(c[1]) else 600
        if time.time() - c[0] < ttl:
            return c[1]
    allg = []
    for (k, _slug, _lab) in LEAGUES:
        try:
            allg.extend(get_games(date, k))
        except Exception as ex:
            print(f"[soccer] today {k} failed: {ex}")
    allg.sort(key=lambda g: (g["status"] != "live",
                             g.get("kickoff_iso") or "z", -g["prominence"]))
    _today_cache[key] = (time.time(), allg)
    return allg


def get_game(date: dt.date, game_id: str, league: str = DEFAULT_LEAGUE):
    """One match, enriched with summary events when available."""
    g = next((x for x in get_games(date, league) if str(x["id"]) == str(game_id)), None)
    if not g:
        return None
    try:
        data = E._get(_SUMMARY.format(slug=_SLUG[league]), {"event": game_id})
        ev = _summary_events(data, g["home_id"], g["away_id"])
        if ev:
            g = dict(g)
            g["events"] = ev
    except Exception as ex:
        print(f"[soccer] summary failed: {ex}")
    return g


def _summary_events(data, home_id, away_id):
    """Richer goal/card timeline from the summary 'keyEvents'/'details'."""
    raw = data.get("keyEvents") or data.get("details") or []
    out = []
    for d in raw:
        typ = ((d.get("type") or {}).get("text") or "").strip()
        low = typ.lower()
        kind = ("goal" if (d.get("scoringPlay") or "goal" in low) else
                "red" if "red" in low else
                "yellow" if ("yellow" in low or "caution" in low) else
                "sub" if "substitut" in low else None)
        if not kind:
            continue
        tid = str((d.get("team") or {}).get("id") or "")
        who = ", ".join(a.get("displayName", "")
                        for a in (d.get("athletesInvolved") or []))
        out.append({
            "kind": kind, "type": typ,
            "clock": ((d.get("clock") or {}).get("displayValue") or "").strip(),
            "team_id": tid,
            "side": "home" if tid == home_id else "away" if tid == away_id else None,
            "player": who,
        })
    return out


# ---- live team-stat comparison (possession, shots, corners...) ----
_STAT_ORDER = [
    "possessionPct", "totalShots", "shotsOnTarget", "wonCorners",
    "foulsCommitted", "yellowCards", "redCards", "offsides", "saves",
]
_STAT_LABEL = {
    "possessionPct": "Possession %", "totalShots": "Shots",
    "shotsOnTarget": "Shots on target", "wonCorners": "Corners",
    "foulsCommitted": "Fouls", "yellowCards": "Yellow cards",
    "redCards": "Red cards", "offsides": "Offsides", "saves": "Saves",
}


def get_boxscore(date: dt.date, game_id: str, league: str = DEFAULT_LEAGUE):
    """Team-vs-team match stats for the Live Stats tab."""
    try:
        data = E._get(_SUMMARY.format(slug=_SLUG[league]), {"event": game_id})
    except Exception:
        return {"stats": []}
    teams = ((data.get("boxscore") or {}).get("teams") or [])
    if len(teams) < 2:
        return {"stats": []}
    # ESPN orders [home, away] or marks homeAway; normalize defensively
    def _is_home(t):
        return (t.get("homeAway") == "home")
    home_t = next((t for t in teams if _is_home(t)), teams[0])
    away_t = next((t for t in teams if t is not home_t), teams[-1])

    def _map(t):
        m = {}
        for s in (t.get("statistics") or []):
            m[s.get("name")] = s.get("displayValue", s.get("value"))
        return m
    hm, am = _map(home_t), _map(away_t)
    keys = [k for k in _STAT_ORDER if k in hm or k in am]
    keys += [k for k in (set(hm) | set(am)) if k not in keys]    # any extras
    rows = []
    for k in keys:
        rows.append({"label": _STAT_LABEL.get(k, k.replace("Pct", " %")),
                     "home": hm.get(k, "\u2014"), "away": am.get(k, "\u2014"),
                     "key": k})
    name = lambda t: ((t.get("team") or {}).get("displayName")
                      or (t.get("team") or {}).get("abbreviation") or "")
    return {"home": name(home_t), "away": name(away_t), "stats": rows}


# ===== per-player box score + game log (for soccer props grading) =====
_SGAMELOG = ("https://site.web.api.espn.com/apis/common/v3/sports/soccer/"
             "{slug}/athletes/{pid}/gamelog")


def get_player_boxscore(date: dt.date, game_id: str, league: str = DEFAULT_LEAGUE):
    """Per-player stats for a match, in the uniform box shape consumed by the
    props grader: {teams:[{name, abbr, groups:[{title, columns, rows:[{name,
    pos, stats[]}]}]}]}. Tries boxscore.players first, then the rosters block
    (where ESPN usually puts soccer player stats)."""
    slug = _SLUG.get(league, _SLUG[DEFAULT_LEAGUE])
    try:
        data = E._get(_SUMMARY.format(slug=slug), {"event": game_id})
    except Exception:
        return {"teams": []}
    # 1) standard boxscore.players path (reuse ESPN parser)
    try:
        parsed = E._parse_espn_boxscore(data)
        if parsed.get("teams") and any(t.get("groups") for t in parsed["teams"]):
            return parsed
    except Exception:
        pass
    # 2) soccer rosters -> a single "Players" group per team
    teams = []
    for r in (data.get("rosters") or []):
        team = r.get("team") or {}
        tname = (team.get("displayName") or team.get("abbreviation") or "Team")
        colset, rows = [], []
        for pl in (r.get("roster") or []):
            ath = pl.get("athlete") or {}
            name = ath.get("displayName") or ath.get("shortName") or "\u2014"
            sd = {}
            for st in (pl.get("stats") or []):
                if not isinstance(st, dict):
                    continue
                k = (st.get("name") or st.get("abbreviation") or "").lower()
                v = st.get("value")
                if v is None:
                    v = st.get("displayValue")
                if k:
                    sd[k] = v
                    if k not in colset:
                        colset.append(k)
            if sd:
                rows.append({"name": name, "sd": sd})
        if rows and colset:
            out_rows = [{"name": rw["name"], "pos": "",
                         "stats": [rw["sd"].get(c, "") for c in colset]}
                        for rw in rows]
            teams.append({"name": tname, "abbr": team.get("abbreviation") or "",
                          "groups": [{"title": "Players", "columns": colset,
                                      "rows": out_rows}]})
    return {"teams": teams}


def _soccer_athlete_id(data, player):
    """Resolve an ESPN athlete id from a display name within a match's rosters."""
    want = "".join(c for c in (player or "").lower() if c.isalnum())
    wl = (player or "").lower().split()
    for r in (data.get("rosters") or []):
        for pl in (r.get("roster") or []):
            ath = pl.get("athlete") or {}
            nm = (ath.get("displayName") or ath.get("shortName") or "")
            norm = "".join(c for c in nm.lower() if c.isalnum())
            parts = nm.lower().split()
            if norm == want or (wl and parts and parts[-1] == wl[-1]
                                and parts[0][:1] == wl[0][:1]):
                aid = ath.get("id") or ath.get("uid", "")
                if aid and "a:" in str(aid):
                    aid = str(aid).split("a:")[-1]
                return str(aid) if aid else None
    return None


# soccer prop stat -> candidate gamelog column labels (matched case-insensitively)
_SLOG_LABEL = {
    "shots": ["sh", "shots", "totalshots", "tsh"],
    "total shots": ["sh", "shots", "totalshots", "tsh"],
    "shots on target": ["sog", "st", "shotsontarget", "sot"],
    "shots on goal": ["sog", "st", "shotsontarget", "sot"],
    "goals": ["g", "goals", "gls"],
    "assists": ["a", "ast", "assists"],
    "saves": ["sv", "saves"],
    "passes": ["pass", "passes", "totalpasses"],
    "tackles": ["tkl", "tackles"],
    "fouls": ["fc", "fouls", "foulscommitted"],
}


def get_prop_history(date: dt.date, game_id: str, player, stat, line,
                     league: str = DEFAULT_LEAGUE):
    """Best-effort last-N match log for a soccer player+stat. Returns the same
    shape as the other providers; empty list if the gamelog can't be read."""
    slug = _SLUG.get(league, _SLUG[DEFAULT_LEAGUE])
    if line is None:
        line = 0.5
    try:
        summary = E._get(_SUMMARY.format(slug=slug), {"event": game_id})
    except Exception:
        return {"player": player, "label": stat, "line": line, "games": [],
                "hits": 0, "total": 0}
    pid = _soccer_athlete_id(summary, player)
    if not pid:
        return {"player": player, "label": stat, "line": line, "games": [],
                "hits": 0, "total": 0}
    try:
        data = E._get(_SGAMELOG.format(slug=slug, pid=pid))
    except Exception:
        return {"player": player, "label": stat, "line": line, "games": [],
                "hits": 0, "total": 0}
    labels = [str(l).lower() for l in (data.get("labels") or [])]
    names = [str(n).lower() for n in (data.get("names") or [])]
    cands = _SLOG_LABEL.get((stat or "").lower(), [(stat or "").lower()])
    col = None
    for src in (names, labels):
        for i, lab in enumerate(src):
            if any(lab == c or lab.startswith(c) for c in cands):
                col = i
                break
        if col is not None:
            break
    events = data.get("events") or {}
    rows = []
    for stp in (data.get("seasonTypes") or []):
        for cat in stp.get("categories", []):
            rows.extend(cat.get("events", []))
    if not rows and isinstance(data.get("events"), list):
        rows = data["events"]
    games = []
    for ev in rows[-10:]:
        stats = ev.get("stats", [])
        if col is None or col >= len(stats):
            continue
        try:
            val = float(str(stats[col]).replace(",", ""))
        except (ValueError, TypeError):
            continue
        meta = events.get(ev.get("eventId"), {}) if isinstance(events, dict) else {}
        opp = ""
        try:
            opp = (meta.get("opponent", {}) or {}).get("abbreviation", "") or ""
        except Exception:
            opp = ""
        games.append({"date": "", "opp": opp, "value": val})
    hits = sum(1 for x in games if x["value"] > line)
    return {"player": player, "label": stat, "line": line, "games": games,
            "hits": hits, "total": len(games)}
