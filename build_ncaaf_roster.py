"""
build_ncaaf_roster.py
---------------------
Builds ncaaf_roster.json: a per-team "roster change" adjustment (in SP+ points)
from real CFBD data, so transfers and incoming talent actually move spreads,
model projections, and win totals — instead of leaning only on SP+ preseason.

Runs on the GitHub Action runner (internet, no Railway timeout pressure, can
afford the heavy join). The live site just reads the small JSON it writes.

HOW THE ADJUSTMENT IS DERIVED (all from free CFBD endpoints):

  1. TRANSFER PORTAL x LAST-SEASON PPA  (the core signal)
     * /player/portal (year)         -> who moved, origin -> destination
     * /ppa/players/season (year-1)  -> what each player PRODUCED last year
     Join them by name+position. A transfer is valued by his prior totalPPA:
       - origin team LOSES that production   (Toledo losing its core)
       - destination team GAINS that production (UConn absorbing it)
     Summed per team => net production swing, weighted by real on-field value —
     not star ratings. A productive QB leaving hurts a lot; a walk-on, little.
     A conference-level multiplier discounts production earned at a weaker level
     when a player moves up (and credits moving down), since MAC PPA != SEC PPA.

  2. RECRUITING  (incoming freshmen the portal/PPA can't see)
     * /recruiting/teams (year) -> class points; nudges teams with strong classes
       up relative to the national-average class.

  3. Output is CAPPED so no single team swings absurdly, and everything is
     expressed in SP+ points so ncaaf_provider can add it straight to the margin.

USAGE (Action or manual):
    CFBD_API_KEY=... python build_ncaaf_roster.py [year]
Writes ./ncaaf_roster.json (or $NCAAF_ROSTER_PATH). Never raises non-zero unless
it produced nothing at all.
"""
from __future__ import annotations

import json
import os
import sys
import time
import unicodedata
import datetime as dt

BASE = "https://api.collegefootballdata.com"
_KEY = (os.environ.get("CFBD_KEY") or os.environ.get("CFBD_API_KEY") or "").strip()

# --- tunables (env-overridable) ------------------------------------------
# Points-per-PPA: converts a team's net seasonal PPA-value swing into raw SP+
# points BEFORE smoothing.
PPA_TO_SP = float(os.environ.get("NCAAF_ROSTER_PPA_SP", "0.35"))
# Recruiting: points per class-point above/below the national average (raw).
RECRUIT_SP = float(os.environ.get("NCAAF_ROSTER_RECRUIT_SP", "0.03"))
# Smoothing: instead of a hard clip (which piled every big team at the cap), we
# squash the combined raw points through a tanh curve. SOFT is where diminishing
# returns kick in; MAX is the asymptotic ceiling the curve approaches but never
# exceeds. So classes still RANK correctly at the top, but the points compress
# gracefully (no absurd +45 swings that would break spreads).
SMOOTH_SOFT = float(os.environ.get("NCAAF_ROSTER_SOFT", "10.0"))
SMOOTH_MAX = float(os.environ.get("NCAAF_ROSTER_MAX", "20.0"))
_TIMEOUT = float(os.environ.get("NCAAF_ROSTER_TIMEOUT", "60"))


def _smooth(raw):
    """Squash raw points onto (-SMOOTH_MAX, +SMOOTH_MAX) with ~linear behavior
    near zero and diminishing returns toward the ceiling. tanh gives exactly
    that shape: small classes ~ linear, huge classes approach but never blow past
    the max, and teams stay ORDERED by raw strength (monotonic)."""
    import math
    return SMOOTH_MAX * math.tanh(raw / SMOOTH_SOFT)

# Conference strength tiers -> multiplier applied to production a transfer earned
# there. Moving up a level discounts old production; moving down credits it.
_CONF_LEVEL = {
    "SEC": 1.00, "Big Ten": 0.98, "B1G": 0.98, "Big 12": 0.92, "ACC": 0.90,
    "Pac-12": 0.90, "American Athletic": 0.72, "AAC": 0.72, "Mountain West": 0.70,
    "MWC": 0.70, "Sun Belt": 0.66, "Conference USA": 0.60, "C-USA": 0.60,
    "Mid-American": 0.58, "MAC": 0.58, "FBS Independents": 0.80,
    # FCS and below (portal players coming up from FCS)
    "Missouri Valley": 0.42, "Big Sky": 0.40, "CAA": 0.40, "Southland": 0.34,
    "Ivy": 0.30, "Patriot": 0.30, "SWAC": 0.32, "MEAC": 0.32, "Southern": 0.36,
    "Big South": 0.34, "OVC": 0.34, "Pioneer": 0.26, "UAC": 0.34,
}
_DEFAULT_LEVEL = 0.5

# Talent-grade -> production value (in PPA-equivalent units), so a highly-rated
# transfer who didn't put up big volume last year (a young stud, a backup behind
# a starter, an elite recruit) still gets valued near what his talent implies.
# A transfer's value = MAX(level-adjusted prior PPA, talent value). This is what
# makes a Cam Coleman-type addition swing the number even if his box-score PPA
# was modest. Star tiers are calibrated so a blue-chip ~ a productive starter.
_STAR_PPA = {5: 34.0, 4: 20.0, 3: 8.0, 2: 3.0, 1: 1.0, 0: 0.0}
# CFBD portal `rating` is the 247-style composite (~0.80-1.00 for blue-chips)
# when present; convert it to a PPA-equivalent on a similar scale.
def _rating_to_ppa(rating):
    try:
        rr = float(rating)
    except Exception:
        return None
    if rr <= 0:
        return None
    # 0.85 ~ mid 3-star ~ 8; 0.95 ~ 4-star ~ 20; 1.00+ ~ 5-star ~ 34
    return max(0.0, (rr - 0.75) * 130.0)


def _talent_value(stars, rating):
    """Best available talent-based value estimate for a transfer, in PPA units."""
    vals = []
    rp = _rating_to_ppa(rating)
    if rp is not None:
        vals.append(rp)
    try:
        s = int(stars) if stars is not None else None
    except Exception:
        s = None
    if s is not None and s in _STAR_PPA:
        vals.append(_STAR_PPA[s])
    return max(vals) if vals else 0.0


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def _pname(first, last, pos):
    return f"{_norm(first)}|{_norm(last)}|{_norm(pos)}"


def _get(path, params):
    """GET a CFBD endpoint with retry/backoff — the portal + returning endpoints
    502 / time out intermittently, and since this runs in the Action (off the
    request path) we can afford to wait them out."""
    import httpx
    attempts = int(os.environ.get("NCAAF_ROSTER_RETRIES", "6"))
    last = None
    for i in range(attempts):
        try:
            r = httpx.get(BASE + path, params=params,
                          headers={"Authorization": f"Bearer {_KEY}",
                                   "Accept": "application/json"},
                          timeout=_TIMEOUT, follow_redirects=True)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
            # 502/503/504/429 are transient — back off and retry.
            if r.status_code not in (502, 503, 504, 429):
                break
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        wait = min(30.0, 2.0 * (2 ** i))          # 2,4,8,16,30,30...
        print(f"[roster] {path} attempt {i+1}/{attempts} failed ({last}); retry in {wait:.0f}s")
        time.sleep(wait)
    print(f"[roster] {path} gave up after {attempts} attempts ({last})")
    return None


def season_year(when=None):
    d = when or dt.date.today()
    return d.year if d.month >= 8 else d.year - 1


def build(year=None):
    if not _KEY:
        print("[roster] no CFBD key set")
        return {"ok": False, "error": "no key"}
    year = int(year or season_year())
    prior = year - 1
    report = {"year": year, "prior": prior}

    # 1) last season's player production, indexed by name+position, with the
    #    team's conference (for the level multiplier).
    try:
        ppa_rows = _get("/ppa/players/season", {"year": prior})
    except Exception as e:
        print(f"[roster] ppa fetch failed: {e}")
        ppa_rows = []
    ppa_by_player = {}
    for r in ppa_rows or []:
        tot = ((r.get("totalPPA") or {}) or {}).get("all")
        if tot is None:
            continue
        key = _pname(r.get("name", "").split(" ")[0] if r.get("name") else "",
                     " ".join(r.get("name", "").split(" ")[1:]) if r.get("name") else "",
                     r.get("position"))
        # name field is "First Last"; also store a looser last-name+pos fallback
        ppa_by_player[key] = {"ppa": float(tot), "conf": r.get("conference"),
                              "team": r.get("team")}
    # looser index: lastname|pos -> list of (ppa, conf) for fallback joins
    loose = {}
    for r in ppa_rows or []:
        nm = r.get("name") or ""
        parts = nm.split(" ")
        if len(parts) < 2:
            continue
        tot = ((r.get("totalPPA") or {}) or {}).get("all")
        if tot is None:
            continue
        lk = f"{_norm(parts[-1])}|{_norm(r.get('position'))}"
        loose.setdefault(lk, []).append({"ppa": float(tot), "conf": r.get("conference")})
    report["ppa_players"] = len(ppa_by_player)

    # 2) the portal: value each move by the player's prior production.
    try:
        portal = _get("/player/portal", {"year": year})
    except Exception as e:
        print(f"[roster] portal fetch failed: {e}")
        portal = []
    report["portal_rows"] = len(portal or [])

    net = {}   # norm(team) -> {"name":.., "in_ppa":x, "out_ppa":y, "moves":n}

    def _team(entry, key):
        return entry.get(key)

    def _level(conf):
        return _CONF_LEVEL.get(conf, _DEFAULT_LEVEL)

    for mv in portal or []:
        first, last = mv.get("firstName"), mv.get("lastName")
        pos = mv.get("position")
        origin = mv.get("origin")
        dest = mv.get("destination")
        if not first or not last:
            continue
        # prior production: exact name+pos, else loose last+pos (unique only)
        rec = ppa_by_player.get(_pname(first, last, pos))
        if rec is None:
            cand = loose.get(f"{_norm(last)}|{_norm(pos)}")
            rec = cand[0] if cand and len(cand) == 1 else None
        # level-adjusted production value (0 if no PPA record)
        prod_val = 0.0
        if rec is not None:
            prod_val = rec["ppa"] * _level(rec.get("conf"))
        # talent value from stars / composite rating (catches elite talent who
        # didn't accumulate volume — the Cam Coleman case)
        talent_val = _talent_value(mv.get("stars"), mv.get("rating"))
        # a transfer is worth the BETTER of proven production and talent grade
        val = max(prod_val, talent_val)
        if val <= 0:
            continue                    # no signal either way -> skip
        pname = f"{first} {last}".strip()
        basis = "production" if prod_val >= talent_val else "talent"
        if origin:
            k = _norm(origin)
            e = net.setdefault(k, {"name": origin, "in_ppa": 0.0, "out_ppa": 0.0,
                                   "moves": 0, "out_players": [], "in_players": []})
            e["out_ppa"] += val
            e["moves"] += 1
            e["out_players"].append({"name": pname, "pos": pos, "value": round(val, 1),
                                     "to": dest, "basis": basis})
        if dest:
            k = _norm(dest)
            e = net.setdefault(k, {"name": dest, "in_ppa": 0.0, "out_ppa": 0.0,
                                   "moves": 0, "out_players": [], "in_players": []})
            e["in_ppa"] += val
            e["moves"] += 1
            e["in_players"].append({"name": pname, "pos": pos, "value": round(val, 1),
                                    "from": origin, "basis": basis})

    # 3) recruiting classes (incoming freshmen), relative to national average.
    try:
        rec_rows = _get("/recruiting/teams", {"year": year})
    except Exception as e:
        print(f"[roster] recruiting fetch failed: {e}")
        rec_rows = []
    rec_pts = {}
    if rec_rows:
        pts = [r.get("points") or 0 for r in rec_rows if r.get("points") is not None]
        avg = (sum(pts) / len(pts)) if pts else 0.0
        for r in rec_rows:
            t = r.get("team")
            p = r.get("points")
            if t and p is not None:
                rec_pts[_norm(t)] = (p - avg)     # above/below average class
    report["recruiting_teams"] = len(rec_pts)

    # 4) combine into a per-team SP+ point adjustment (smoothed, not clipped) plus
    #    a 0-100 impact score for display/ranking.
    teams = {}
    all_keys = set(net) | set(rec_pts)
    raw_by_key = {}
    for k in all_keys:
        e = net.get(k, {})
        net_ppa = (e.get("in_ppa", 0.0) - e.get("out_ppa", 0.0))
        raw_pts = net_ppa * PPA_TO_SP + rec_pts.get(k, 0.0) * RECRUIT_SP
        raw_by_key[k] = raw_pts

    # 0-100 impact score: 50 = no net change; scaled by the raw magnitude across
    # the league so the biggest movers approach 100 / 0. Uses the max abs raw as
    # the reference so it's self-normalizing to whatever this cycle looks like.
    max_abs = max((abs(v) for v in raw_by_key.values()), default=1.0) or 1.0

    for k in all_keys:
        e = net.get(k, {})
        raw_pts = raw_by_key[k]
        adj = _smooth(raw_pts)                       # SP+ points fed to the model
        impact = round(50.0 + 50.0 * (raw_pts / max_abs), 1)   # 0..100, 50 = neutral
        impact = max(0.0, min(100.0, impact))
        ins = sorted(e.get("in_players", []), key=lambda x: -x["value"])
        outs = sorted(e.get("out_players", []), key=lambda x: -x["value"])
        teams[k] = {
            "name": e.get("name") or k,
            "adj_sp": round(adj, 2),                 # smoothed points (model input)
            "impact": impact,                        # 0-100 display score
            "raw_pts": round(raw_pts, 2),
            "net_ppa": round(net_ppa, 2),
            "in_value": round(e.get("in_ppa", 0.0), 1),
            "out_value": round(e.get("out_ppa", 0.0), 1),
            "moves": e.get("moves", 0),
            "recruit_pts": round(rec_pts.get(k, 0.0) * RECRUIT_SP, 2),
            # key transfers = top 5 each way by value; plus full lists for the board
            "key_in": ins[:5],
            "key_out": outs[:5],
            "all_in": ins,
            "all_out": outs,
        }

    blob = {
        "year": year, "prior_season": prior,
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(),
        "params": {"ppa_to_sp": PPA_TO_SP, "recruit_sp": RECRUIT_SP,
                   "smooth_soft": SMOOTH_SOFT, "smooth_max": SMOOTH_MAX},
        "teams": teams,
    }
    path = os.environ.get("NCAAF_ROSTER_PATH") or (
        "/data/ncaaf_roster.json" if os.path.isdir("/data") else "ncaaf_roster.json")
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(blob, f)
    except Exception as e:
        # in the Action, /data won't exist — fall back to repo root
        path = "ncaaf_roster.json"
        with open(path, "w") as f:
            json.dump(blob, f)
    report["ok"] = True
    report["teams_out"] = len(teams)
    report["path"] = path
    # show the biggest movers both ways, as a sanity check in the log
    movers = sorted(teams.values(), key=lambda x: x["adj_sp"])
    report["biggest_down"] = [(m["name"], m["adj_sp"]) for m in movers[:8]]
    report["biggest_up"] = [(m["name"], m["adj_sp"]) for m in movers[-8:]][::-1]
    return report


if __name__ == "__main__":
    yr = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    rep = build(yr or None)
    print(json.dumps(rep, indent=2)[:3000])
    sys.exit(0 if rep.get("ok") else 1)
