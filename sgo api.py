"""
SportsGameOdds adapter — free player props.

The Odds API gates player props behind paid plans; SportsGameOdds includes them
on its free "Amateur" tier (2.5k objects/month, ~9 US books, NBA/NFL/MLB/NHL/
college). This module pulls per-game player props and returns them in the SAME
shape main.py already expects from the odds_api props path:

    {player, team?, stat, label, line, over_odds, under_odds, over_prob, lean, source:"book"}

Enable by setting SPORTSGAMEODDS_KEY. With no key everything is inert (the props
endpoints fall back to The Odds API and then to the model projection).

Object budget note: each league slate is fetched at most once per CACHE_TTL and
reused for every game/prop view in that window, to protect the free monthly cap.
"""

import os
import time

API_KEY = os.environ.get("SPORTSGAMEODDS_KEY", "").strip()
BASE = "https://api.sportsgameodds.com/v2"
CACHE_TTL = int(os.environ.get("SGO_CACHE_SECONDS", "600"))   # 10 min (free update freq)
EVENT_LIMIT = int(os.environ.get("SGO_EVENT_LIMIT", "40"))

# app sport -> SportsGameOdds leagueID
SGO_LEAGUE = {
    "mlb": "MLB", "nba": "NBA", "nfl": "NFL", "nhl": "NHL",
    "ncaaf": "NCAAF", "ncaab": "NCAAB", "ufc": "MMA",
}

# app soccer league key (soccer_provider) -> SportsGameOdds soccer leagueID
SGO_SOCCER = {
    "epl": "EPL",
    "ucl": "UEFA_CHAMPIONS_LEAGUE",
    "uel": "UEFA_EUROPA_LEAGUE",
    "uecl": "UEFA_EUROPA_CONFERENCE_LEAGUE",
    "laliga": "LA_LIGA",
    "seriea": "SERIE_A",
    "bundesliga": "BUNDESLIGA",
    "ligue1": "LIGUE_1",
    "mls": "MLS",
    "ligamx": "LIGA_MX",
    "championship": "EFL_CHAMPIONSHIP",
    "eredivisie": "EREDIVISIE",
    "ligaportugal": "PRIMEIRA_LIGA",
    "saudi": "SAUDI_PRO_LEAGUE",
    "worldcup": "FIFA_WORLD_CUP",
}

_events_cache = {}   # league -> (ts, [events])
_cooldown_until = 0.0
SGO_COOLDOWN = int(os.environ.get("SGO_COOLDOWN_SECONDS", "1800"))  # back off 30 min on 429


def available():
    """True only if SGO has a key AND isn't in a 429 cooldown. Callers use this
    to decide whether to lean on SGO or fall back to another odds source."""
    return bool(API_KEY) and time.time() >= _cooldown_until


def _trip_cooldown():
    global _cooldown_until
    _cooldown_until = time.time() + SGO_COOLDOWN
    print(f"[sgo] 429 rate limit — backing off {SGO_COOLDOWN}s")


def enabled():
    return bool(API_KEY)


def _norm(name):
    return "".join(c for c in (name or "").lower() if c.isalnum())


def _nick(name):
    parts = (name or "").lower().split()
    return parts[-1] if parts else ""


def _imp(o):
    """Implied probability from American odds."""
    if o is None:
        return None
    try:
        o = int(o)
    except (TypeError, ValueError):
        return None
    return 100.0 / (o + 100) if o > 0 else (-o) / ((-o) + 100)


def _amer(p):
    """American odds from an implied probability (vig included)."""
    if p is None or p <= 0 or p >= 1:
        return None
    return -round(100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _format_player(player_id):
    """'LEBRON_JAMES_1_NBA' -> 'Lebron James'."""
    if not player_id:
        return "Unknown"
    parts = player_id.split("_")
    name_parts = parts[:-2] if len(parts) > 2 else parts
    return " ".join(w.capitalize() for w in name_parts) or player_id


def _league_events(league):
    """Fetch (and cache) the league's events-with-odds slate. One request per
    league per CACHE_TTL, reused for every game in that window. Honors a 429
    cooldown so we don't keep hammering SGO when it's rate-limited."""
    c = _events_cache.get(league)
    if c and time.time() - c[0] < CACHE_TTL:
        return c[1]
    if time.time() < _cooldown_until:        # in 429 backoff — serve stale/empty
        return c[1] if c else []
    try:
        import httpx
        r = httpx.get(f"{BASE}/events",
                      params={"leagueID": league, "finalized": "false",
                              "oddsAvailable": "true", "limit": EVENT_LIMIT},
                      headers={"x-api-key": API_KEY}, timeout=6)
        if r.status_code == 429:
            _trip_cooldown()
            return c[1] if c else []
        r.raise_for_status()
        events = (r.json() or {}).get("data") or []
        _events_cache[league] = (time.time(), events)
        return events
    except Exception as ex:
        print(f"[sgo] events {league} failed: {ex}")
        return c[1] if c else []


def _match_event(events, home, away):
    """Find the event whose home/away teams match the app's game."""
    hn, an = _norm(home), _norm(away)
    hk, ak = _nick(home), _nick(away)
    for ev in events:
        teams = ev.get("teams") or {}
        h = ((teams.get("home") or {}).get("names") or {})
        a = ((teams.get("away") or {}).get("names") or {})
        h_norms = {_norm(h.get(k)) for k in ("long", "medium", "short")}
        a_norms = {_norm(a.get(k)) for k in ("long", "medium", "short")}
        home_ok = hn in h_norms or hk == _nick(h.get("long"))
        away_ok = an in a_norms or ak == _nick(a.get("long"))
        if home_ok and away_ok:
            return ev
    return None


def get_player_props(sport, home, away, league=None):
    """Player props for one game, aggregated across books (median line,
    de-vigged over probability, median over/under price). `league` overrides
    the sport->league lookup (used for soccer, which has one league per match).
    Returns [] on: no key, unsupported league, no event match, or none posted."""
    lg = league or SGO_LEAGUE.get(sport)
    if not API_KEY or not lg:
        return []
    events = _league_events(lg)
    if not events:
        return []
    ev = _match_event(events, home, away)
    if not ev:
        return []
    odds = ev.get("odds") or {}
    acc = {}   # (player_id, statID) -> {lines, over_imp, under_imp}
    for odd in odds.values():
        if odd.get("periodID") != "game":
            continue                       # full-game props only (skip halves/qtrs)
        ent = odd.get("statEntityID")
        if ent in ("all", "home", "away", None):
            continue                       # team-level, not a player prop
        side = odd.get("sideID")           # over/under props only (skip yes/no etc.)
        if side not in ("over", "under"):
            continue
        stat_id = odd.get("statID") or "stat"
        # line can live on the bookmaker entry or, as a fallback, on the odd itself
        odd_line = (odd.get("overUnder") or odd.get("fairOverUnder")
                    or odd.get("bookOverUnder"))
        d = acc.setdefault((ent, stat_id), {"lines": [], "over_imp": [], "under_imp": []})
        for bd in (odd.get("byBookmaker") or {}).values():
            if not bd.get("available", True):
                continue
            ou = bd.get("overUnder")
            if ou is None:
                ou = bd.get("line")
            if ou is None:
                ou = odd_line
            if ou is not None:
                try:
                    d["lines"].append(float(ou))
                except (TypeError, ValueError):
                    pass
            ip = _imp(bd.get("odds"))
            if ip is not None:
                if side == "over":
                    d["over_imp"].append(ip)
                elif side == "under":
                    d["under_imp"].append(ip)
    out = []
    for (player_id, stat_id), d in acc.items():
        line = _median(d["lines"])
        if line is None:
            continue
        oimp, uimp = _median(d["over_imp"]), _median(d["under_imp"])
        if oimp is not None and uimp is not None and (oimp + uimp) > 0:
            over_prob = oimp / (oimp + uimp)        # de-vig
        elif oimp is not None:
            over_prob = oimp
        else:
            over_prob = 0.5
        label = stat_id.replace("_", " ").title()
        out.append({
            "player": _format_player(player_id),
            "stat": label, "label": label,
            "line": round(line, 1),
            "over_odds": _amer(oimp),
            "under_odds": _amer(uimp),
            "over_prob": round(over_prob, 3),
            "lean": "over" if over_prob >= 0.5 else "under",
            "source": "book",
        })
    out.sort(key=lambda p: (p["stat"], p["player"]))
    return out


def get_game_odds(sport, home, away, league=None):
    """Game moneyline (home / away, plus draw for soccer) for one match,
    aggregated across books to a fair price. Returns {ml_home, ml_away[,
    ml_draw]} or None. Used as a free fallback when The Odds API isn't
    configured, so the model-vs-market edge can render on every game."""
    lg = league or SGO_LEAGUE.get(sport)
    if not API_KEY or not lg:
        return None
    try:
        events = _league_events(lg)
    except Exception:
        return None
    if not events:
        return None
    ev = _match_event(events, home, away)
    if not ev:
        return None
    sides = {"home": [], "away": [], "draw": []}
    for odd in (ev.get("odds") or {}).values():
        if odd.get("periodID") != "game":
            continue
        if odd.get("betTypeID") not in ("ml", "ml3way", "moneyline"):
            continue
        # SGO carries the team side in statEntityID ("home"/"away"/"draw");
        # sideID is used for over/under markets, so prefer the entity here.
        sd = odd.get("statEntityID")
        if sd not in sides:
            sd = odd.get("sideID")
        if sd not in sides:
            continue
        booked = False
        for bd in (odd.get("byBookmaker") or {}).values():
            if not bd.get("available", True):
                continue
            ip = _imp(bd.get("odds"))
            if ip is not None:
                sides[sd].append(ip)
                booked = True
        if not booked:                       # fall back to the odd-level price
            ip = _imp(odd.get("odds") or odd.get("fairOdds") or odd.get("bookOdds"))
            if ip is not None:
                sides[sd].append(ip)
    mh, ma, md = _median(sides["home"]), _median(sides["away"]), _median(sides["draw"])
    if mh is None and ma is None:
        return None
    out = {"ml_home": _amer(mh) if mh is not None else None,
           "ml_away": _amer(ma) if ma is not None else None}
    if md is not None:
        out["ml_draw"] = _amer(md)
    return out


def get_game_markets(sport, home, away, league=None):
    """Full three-market read for one game from the same cached slate:

        {ml_home, ml_away[, ml_draw],
         spread_home, spread_away, spread_home_price, spread_away_price,
         total, total_over_price, total_under_price}

    Costs nothing beyond get_game_odds — same cached /events response, we just
    also read the spread ("sp") and total ("ou") bet types instead of throwing
    them away. For UFC the total is total rounds. Missing markets are None.
    Returns None when the event can't be matched at all."""
    lg = league or SGO_LEAGUE.get(sport)
    if not API_KEY or not lg:
        return None
    try:
        events = _league_events(lg)
    except Exception:
        return None
    if not events:
        return None
    ev = _match_event(events, home, away)
    if not ev:
        return None

    ml = {"home": [], "away": [], "draw": []}
    sp_line = {"home": [], "away": []}
    sp_px = {"home": [], "away": []}        # [(point, implied)]
    tot_line = []
    tot_px = {"over": [], "under": []}      # [(point, implied)]

    def _line_of(odd, bd):
        for src in (bd, odd):
            for k in ("spread", "overUnder", "line", "bookSpread", "bookOverUnder",
                      "fairSpread", "fairOverUnder"):
                v = src.get(k) if isinstance(src, dict) else None
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
        return None

    for odd in (ev.get("odds") or {}).values():
        if odd.get("periodID") != "game":
            continue
        bt = odd.get("betTypeID")
        ent = odd.get("statEntityID")
        side = odd.get("sideID")
        if bt in ("ml", "ml3way", "moneyline"):
            sd = ent if ent in ml else side
            if sd not in ml:
                continue
            got = False
            for bd in (odd.get("byBookmaker") or {}).values():
                if not bd.get("available", True):
                    continue
                ip = _imp(bd.get("odds"))
                if ip is not None:
                    ml[sd].append(ip)
                    got = True
            if not got:
                ip = _imp(odd.get("odds") or odd.get("fairOdds") or odd.get("bookOdds"))
                if ip is not None:
                    ml[sd].append(ip)
        elif bt in ("sp", "spread", "spreads"):
            sd = ent if ent in sp_line else side
            if sd not in sp_line:
                continue
            for bd in (odd.get("byBookmaker") or {}).values():
                if not bd.get("available", True):
                    continue
                ln = _line_of(odd, bd)
                ip = _imp(bd.get("odds"))
                if ln is not None:
                    sp_line[sd].append(ln)
                    if ip is not None:
                        sp_px[sd].append((ln, ip))
            if not sp_line[sd]:
                ln = _line_of(odd, None)
                if ln is not None:
                    sp_line[sd].append(ln)
                    ip = _imp(odd.get("odds") or odd.get("bookOdds"))
                    if ip is not None:
                        sp_px[sd].append((ln, ip))
        elif bt in ("ou", "total", "totals", "overunder"):
            if ent not in ("all", None) and side not in ("over", "under"):
                continue                    # team totals etc. — skip
            if side not in ("over", "under"):
                continue
            for bd in (odd.get("byBookmaker") or {}).values():
                if not bd.get("available", True):
                    continue
                ln = _line_of(odd, bd)
                ip = _imp(bd.get("odds"))
                if ln is not None:
                    tot_line.append(ln)
                    if ip is not None:
                        tot_px[side].append((ln, ip))
            if not tot_line:
                ln = _line_of(odd, None)
                if ln is not None:
                    tot_line.append(ln)
                    ip = _imp(odd.get("odds") or odd.get("bookOdds"))
                    if ip is not None:
                        tot_px[side].append((ln, ip))

    def _px_at(pairs, line):
        if not pairs:
            return None
        if line is None:
            return _median([p for _, p in pairs])
        near = [p for pt, p in pairs if abs(pt - line) <= 0.5]
        return _median(near or [p for _, p in pairs])

    mh, ma, md = _median(ml["home"]), _median(ml["away"]), _median(ml["draw"])
    sh, sa = _median(sp_line["home"]), _median(sp_line["away"])
    tl = _median(tot_line)
    if mh is None and ma is None and sh is None and tl is None:
        return None
    out = {
        "ml_home": _amer(mh) if mh is not None else None,
        "ml_away": _amer(ma) if ma is not None else None,
        "spread_home": sh, "spread_away": sa,
        "spread_home_price": _amer(_px_at(sp_px["home"], sh)),
        "spread_away_price": _amer(_px_at(sp_px["away"], sa)),
        "total": tl,
        "total_over_price": _amer(_px_at(tot_px["over"], tl)),
        "total_under_price": _amer(_px_at(tot_px["under"], tl)),
    }
    if md is not None:
        out["ml_draw"] = _amer(md)
    return out


def diag_game(sport, home, away, league=None):
    """Step-by-step why get_game_odds() returns None for a game: HTTP status,
    event count, SGO's team naming, whether our match found it, and which bet
    types the matched event actually carries. One request; for /api/odds/diag."""
    lg = league or SGO_LEAGUE.get(sport)
    out = {"enabled": enabled(), "league": lg}
    if not API_KEY or not lg:
        out["stop"] = "no_key_or_league"
        return out
    try:
        import httpx
        r = httpx.get(f"{BASE}/events",
                      params={"leagueID": lg, "finalized": "false",
                              "oddsAvailable": "true", "limit": EVENT_LIMIT},
                      headers={"x-api-key": API_KEY}, timeout=6)
        out["http_status"] = r.status_code
        try:
            body = r.json()
        except Exception:
            body = {}
            out["raw_body"] = r.text[:200]
        events = (body or {}).get("data") or []
        out["events_count"] = len(events)
        if not events and isinstance(body, dict):
            out["body_keys"] = list(body.keys())[:8]
            for k in ("error", "message", "success", "rateLimited"):
                if k in body:
                    out[k] = body[k]
        if events:
            out["sgo_sample_teams"] = [
                {"home": (((e.get("teams") or {}).get("home") or {}).get("names") or {}).get("long"),
                 "away": (((e.get("teams") or {}).get("away") or {}).get("names") or {}).get("long")}
                for e in events[:6]]
            ev = _match_event(events, home, away)
            out["matched_our_game"] = bool(ev)
            if ev:
                bt = {}
                for odd in (ev.get("odds") or {}).values():
                    b = odd.get("betTypeID")
                    bt[b] = bt.get(b, 0) + 1
                out["bet_types_in_matched_event"] = bt
    except Exception as e:
        out["fetch_error"] = str(e)
    return out
