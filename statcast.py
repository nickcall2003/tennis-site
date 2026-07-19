"""
statcast.py — Baseball Savant (Statcast) data: hitter-vs-pitch-type outcomes,
pitcher arm angle, pitch movement, and spin.

WHY THIS EXISTS
The MLB Stats API gives us handedness splits (K% vs LHP/RHP). That's coarse. This
module goes to the level a hitter actually experiences: whiff rate and xwOBA
against a SPECIFIC pitch type, the pitcher's arm slot, and how much his pitches
actually move relative to comparable pitches.

DEFENSIVE BY DESIGN (same rules as hoops_advanced.py)
  * Every function returns None on any failure and NEVER raises.
  * A disk cache means we hit Savant once a day per leaderboard, not per request.
  * If Savant blocks this server, callers simply get None and the app shows the
    coarser MLB-API splits it already has. No fabricated numbers, ever.

Kill switch: STATCAST=0
Cache path:  STATCAST_PATH (default /data/statcast.json)

NOTE ON ENDPOINTS: Savant's leaderboard CSV params change occasionally. Each
fetch is probed independently and `status()` reports exactly which ones returned
rows and what columns they carry — so a broken endpoint is visible, not silent.
"""
import csv
import datetime as dt
import io
import json
import os
import time

_ENABLED = os.environ.get("STATCAST", "1").strip().lower() not in ("0", "false", "no")
_STORE = os.environ.get("STATCAST_PATH", "/data/statcast.json")
_TTL = int(os.environ.get("STATCAST_TTL", str(24 * 3600)))   # refresh daily

BASE = "https://baseballsavant.mlb.com/leaderboard"
_V = "v2"        # bump to invalidate cached rows after a parsing fix

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "text/csv,application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://baseballsavant.mlb.com/",
}

_mem = {}          # key -> (ts, rows)
_health = {}       # key -> {"ok":bool,"rows":int,"error":str,"cols":[...]}


def season():
    now = dt.date.today()
    return now.year if now.month >= 3 else now.year - 1


# ----------------------------- disk cache -----------------------------
def _disk_load():
    try:
        with open(_STORE) as f:
            return json.load(f)
    except Exception:
        return {}


def _disk_save(blob):
    try:
        os.makedirs(os.path.dirname(_STORE) or ".", exist_ok=True)
        with open(_STORE, "w") as f:
            json.dump(blob, f)
    except Exception:
        pass


def _fetch_csv(key, path, params, timeout=20.0, nocache=False):
    """GET a Savant leaderboard as CSV -> list[dict]. None on ANY failure."""
    if not _ENABLED:
        return None
    now = time.time()
    hit = _mem.get(key)
    if hit and now - hit[0] < _TTL and not nocache:
        _health[key] = {"ok": True, "rows": len(hit[1]), "source": "memory",
                        "cols": list(hit[1][0].keys()) if hit[1] else [], "error": None}
        return hit[1]
    disk = _disk_load()
    d = disk.get(key)
    if isinstance(d, dict) and now - (d.get("t") or 0) < _TTL and d.get("rows") and not nocache:
        _mem[key] = (d["t"], d["rows"])
        _health[key] = {"ok": True, "rows": len(d["rows"]), "source": "disk",
                        "cols": list(d["rows"][0].keys()) if d["rows"] else [],
                        "error": None}
        return d["rows"]
    try:
        import httpx
        p = dict(params)
        p["csv"] = "true"
        r = httpx.get(f"{BASE}/{path}", params=p, headers=_HEADERS,
                      timeout=timeout, follow_redirects=True)
        if r.status_code != 200:
            _health[key] = {"ok": False, "error": f"HTTP {r.status_code}", "rows": 0}
            return None
        try:
            text = r.content.decode("utf-8-sig")     # Savant CSVs carry a BOM
        except Exception:
            text = r.text or ""
        if not text.strip() or "<html" in text[:200].lower():
            _health[key] = {"ok": False, "error": "not CSV (blocked or bad params)",
                            "rows": 0}
            return None
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows:
            _health[key] = {"ok": False, "error": "0 rows", "rows": 0}
            return None
        _health[key] = {"ok": True, "rows": len(rows), "source": "network",
                        "cols": list(rows[0].keys()), "error": None}
        _mem[key] = (now, rows)
        disk[key] = {"t": now, "rows": rows}
        _disk_save(disk)
        return rows
    except Exception as e:
        _health[key] = {"ok": False, "error": str(e)[:120], "rows": 0}
        return None


def _norm_name(v):
    """Normalize a player name for matching: strip accents, punctuation and
    suffixes so 'Jose Ramirez Jr.' and 'Jos\u00e9 Ram\u00edrez' land on the same key."""
    import re
    import unicodedata
    if not v:
        return ""
    v = unicodedata.normalize("NFKD", str(v))
    v = "".join(c for c in v if not unicodedata.combining(c))
    v = v.lower().replace(".", " ").replace("'", "").replace("-", " ")
    v = re.sub(r"\b(jr|sr|ii|iii|iv)\b", " ", v)
    return re.sub(r"\s+", " ", v).strip()


def _name_of(row):
    """Savant puts the player in a single \"last_name, first_name\" column
    (e.g. 'Skenes, Paul'). Normalize to 'paul skenes'."""
    for k in ("last_name, first_name", "player_name", "pitcher_name", "name",
              "last_name,first_name"):
        v = row.get(k)
        if v:
            v = str(v).strip().strip('"')
            if "," in v:
                last, _, first = v.partition(",")
                return _norm_name(f"{first.strip()} {last.strip()}")
            return _norm_name(v)
    return ""


def _num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


# ----------------------------- leaderboards -----------------------------
def batter_arsenal(yr=None, nocache=False):
    """How each HITTER performs against each pitch type.
    -> {player_lower: {pitch_type: {whiff_pct, ba, slg, woba, pa}}}"""
    yr = yr or season()
    rows = _fetch_csv(f"batter_arsenal_{yr}_{_V}", "pitch-arsenal-stats",
                      {"type": "batter", "year": yr, "minPA": 25}, nocache=nocache)
    if not rows:
        return None
    out = {}
    for r in rows:
        nm = _name_of(r)
        pt = (r.get("pitch_type") or r.get("pitch_name") or "").strip().upper()
        if not nm or not pt:
            continue
        out.setdefault(nm, {})[pt] = {
            "whiff_pct": _num(r.get("whiff_percent")),
            "ba": _num(r.get("ba")),
            "slg": _num(r.get("slg")),
            "woba": _num(r.get("woba")),
            "k_pct": _num(r.get("k_percent")),
            "pa": _num(r.get("pa")),
        }
    return out or None


def pitcher_arsenal(yr=None, nocache=False):
    """How each PITCHER's pitch types perform (whiff%, xwOBA against, usage)."""
    yr = yr or season()
    rows = _fetch_csv(f"pitcher_arsenal_{yr}_{_V}", "pitch-arsenal-stats",
                      {"type": "pitcher", "year": yr, "minPA": 25}, nocache=nocache)
    if not rows:
        return None
    out = {}
    for r in rows:
        nm = _name_of(r)
        pt = (r.get("pitch_type") or r.get("pitch_name") or "").strip().upper()
        if not nm or not pt:
            continue
        out.setdefault(nm, {})[pt] = {
            "whiff_pct": _num(r.get("whiff_percent")),
            "k_pct": _num(r.get("k_percent")),
            "ba": _num(r.get("ba")),
            "woba": _num(r.get("woba")),
            "pa": _num(r.get("pa")),
            "usage": _num(r.get("pitch_usage")),
        }
    return out or None


def arm_angles(yr=None, nocache=False):
    """Pitcher arm slot at release, in degrees. -> {player_lower: {arm_angle, ...}}"""
    yr = yr or season()
    rows = (_fetch_csv(f"arm_angle_{yr}_{_V}", "pitcher-arm-angles", {"year": yr}, nocache=nocache)
            or _fetch_csv(f"arm_angle_alt_{yr}_{_V}", "arm-angles", {"year": yr}, nocache=nocache))
    if not rows:
        return None
    out = {}
    for r in rows:
        nm = _name_of(r)
        if not nm:
            continue
        ang = _num(r.get("ball_angle") or r.get("arm_angle"))
        if ang is None:
            continue
        out[nm] = {"arm_angle": ang,
                   "hand": (r.get("pitch_hand") or "").strip(),
                   "release_height": _num(r.get("release_ball_z")
                                          or r.get("relative_release_ball_height")),
                   "n_pitches": _num(r.get("n_pitches"))}
    return out or None


def pitch_movement(yr=None, pitch="FF", nocache=False):
    """Movement vs. comparable pitches for one pitch type.
    -> {player_lower: {pitch, break_z_vs_avg, break_x_vs_avg, velo, spin}}"""
    yr = yr or season()
    rows = _fetch_csv(f"movement_{pitch}_{yr}_{_V}", "pitch-movement",
                      {"year": yr, "pitch_type": pitch, "min": "q"}, nocache=nocache)
    if not rows:
        return None
    out = {}
    for r in rows:
        nm = _name_of(r)
        if not nm:
            continue
        out[nm] = {k: v for k, v in {
            "pitch": pitch,
            "break_z_vs_avg": _num(r.get("diff_z") or r.get("rise_diff")
                                   or r.get("pitcher_break_z_induced")),
            "break_x_vs_avg": _num(r.get("diff_x") or r.get("break_x_diff")
                                   or r.get("pitcher_break_x")),
            "break_z": _num(r.get("pitcher_break_z")),
            "velo": _num(r.get("avg_speed") or r.get("velocity")),
            "spin": _num(r.get("avg_spin") or r.get("spin_rate")),
        }.items() if v is not None}
    return out or None


# ----------------------------- lookups the app uses -----------------------------
def hitter_vs_pitch(batter, pitch_type, yr=None):
    """One hitter's real outcomes against one pitch type, or None."""
    a = batter_arsenal(yr)
    if not a or not batter:
        return None
    row = a.get(_norm_name(batter))
    if not row:
        return None
    return row.get(str(pitch_type).strip().upper())


def pitcher_profile(name, yr=None):
    """Everything we actually know about a pitcher's stuff: arm angle, per-pitch
    whiff rates, and fastball movement/spin. Only real values are included."""
    if not name:
        return None
    key = _norm_name(name)
    out = {}
    aa = arm_angles(yr)
    if aa and key in aa:
        out["arm_angle"] = aa[key].get("arm_angle")
    pa = pitcher_arsenal(yr)
    if pa and key in pa:
        out["arsenal"] = pa[key]
        best = None
        for pt, v in pa[key].items():
            w = v.get("whiff_pct")
            if w is not None and (best is None or w > best[1]):
                best = (pt, w)
        if best:
            out["best_whiff_pitch"] = {"pitch": best[0], "whiff_pct": best[1]}
        prim = None
        for pt, v in pa[key].items():
            u = v.get("usage")
            if u is not None and (prim is None or u > prim[1]):
                prim = (pt, u)
        if prim:
            out["primary_pitch"] = {"pitch": prim[0], "usage": prim[1]}
    mv = pitch_movement(yr, "FF")
    if mv and key in mv:
        out["fastball"] = mv[key]
    return out or None


def status(yr=None, fresh=False):
    """Which Savant endpoints actually work from THIS server, with row counts and
    the columns they returned. Run this before trusting any of the above."""
    yr = yr or season()
    probes = {
        "batter_arsenal": lambda: batter_arsenal(yr, nocache=fresh),
        "pitcher_arsenal": lambda: pitcher_arsenal(yr, nocache=fresh),
        "arm_angles": lambda: arm_angles(yr, nocache=fresh),
        "pitch_movement_FF": lambda: pitch_movement(yr, "FF", nocache=fresh),
    }
    res = {}
    for k, fn in probes.items():
        t0 = time.time()
        try:
            d = fn()
        except Exception as e:
            d = None
            _health[k] = {"ok": False, "error": str(e)[:120], "rows": 0}
        res[k] = {"players": len(d) if d else 0,
                  "secs": round(time.time() - t0, 1)}
    return {"enabled": _ENABLED, "season": yr, "results": res,
            "endpoint_health": _health,
            "note": ("If everything is 0 players, Savant is unreachable from this "
                     "server (same as stats.nba.com) and we'd need a PC-side fetch.")}


def league_stat_by_pitch(stat="whiff_pct", yr=None):
    """League-average value of any batter stat for each pitch type, PA-weighted and
    computed from Savant's own rows — so every baseline we compare against is real,
    never a guessed constant."""
    a = batter_arsenal(yr)
    if not a:
        return None
    agg = {}
    for _nm, pitches in a.items():
        for pt, v in pitches.items():
            x, pa = v.get(stat), v.get("pa")
            if x is None or not pa:
                continue
            s = agg.setdefault(pt, [0.0, 0.0])
            s[0] += x * pa
            s[1] += pa
    return {pt: round(tot / n, 4) for pt, (tot, n) in agg.items() if n} or None


def league_whiff_by_pitch(yr=None):
    """League-average whiff% by pitch type."""
    return league_stat_by_pitch("whiff_pct", yr)


def batter_vs_pitch(batter, pitch_type, yr=None, min_pa=15):
    """One hitter vs one pitch type, WITH the league baselines for context.
    None unless the hitter has a real sample against that pitch."""
    v = hitter_vs_pitch(batter, pitch_type, yr)
    if not v or not v.get("pa") or v["pa"] < min_pa:
        return None
    pt = str(pitch_type).strip().upper()
    out = dict(v)
    out["pitch"] = pt
    for stat, key in (("ba", "league_ba"), ("whiff_pct", "league_whiff_pct"),
                      ("woba", "league_woba")):
        lg = (league_stat_by_pitch(stat, yr) or {}).get(pt)
        if lg is not None:
            out[key] = lg
    return out


def lineup_whiff_vs_pitch(names, pitch_type, yr=None):
    """How a LINEUP actually fares against one pitch type: PA-weighted whiff% of
    the listed hitters, plus the league baseline for that pitch.
    Returns None unless we matched at least 3 hitters — no thin-sample claims."""
    a = batter_arsenal(yr)
    if not a or not names or not pitch_type:
        return None
    pt = str(pitch_type).strip().upper()
    tot = n = 0.0
    matched = []
    for nm in names:
        row = a.get(_norm_name(nm))
        if not row:
            continue
        v = row.get(pt)
        if not v:
            continue
        w, pa = v.get("whiff_pct"), v.get("pa")
        if w is None or not pa:
            continue
        tot += w * pa
        n += pa
        matched.append(nm)
    if len(matched) < 3 or not n:
        return None
    lg = (league_whiff_by_pitch(yr) or {}).get(pt)
    return {"whiff_pct": round(tot / n, 1), "hitters": len(matched),
            "pitch": pt, "league_whiff_pct": lg}
