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


def _fetch_csv(key, path, params, timeout=20.0):
    """GET a Savant leaderboard as CSV -> list[dict]. None on ANY failure."""
    if not _ENABLED:
        return None
    now = time.time()
    hit = _mem.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    disk = _disk_load()
    d = disk.get(key)
    if isinstance(d, dict) and now - (d.get("t") or 0) < _TTL and d.get("rows"):
        _mem[key] = (d["t"], d["rows"])
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
        text = r.text or ""
        if not text.strip() or "<html" in text[:200].lower():
            _health[key] = {"ok": False, "error": "not CSV (blocked or bad params)",
                            "rows": 0}
            return None
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows:
            _health[key] = {"ok": False, "error": "0 rows", "rows": 0}
            return None
        _health[key] = {"ok": True, "rows": len(rows),
                        "cols": list(rows[0].keys())[:14], "error": None}
        _mem[key] = (now, rows)
        disk[key] = {"t": now, "rows": rows}
        _disk_save(disk)
        return rows
    except Exception as e:
        _health[key] = {"ok": False, "error": str(e)[:120], "rows": 0}
        return None


def _num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


# ----------------------------- leaderboards -----------------------------
def batter_arsenal(yr=None):
    """How each HITTER performs against each pitch type.
    -> {player_lower: {pitch_type: {whiff_pct, ba, slg, woba, pa}}}"""
    yr = yr or season()
    rows = _fetch_csv(f"batter_arsenal_{yr}", "pitch-arsenal-stats",
                      {"type": "batter", "year": yr, "minPA": 25})
    if not rows:
        return None
    out = {}
    for r in rows:
        nm = (r.get("name") or r.get("player_name") or "").strip().lower()
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


def pitcher_arsenal(yr=None):
    """How each PITCHER's pitch types perform (whiff%, xwOBA against, usage)."""
    yr = yr or season()
    rows = _fetch_csv(f"pitcher_arsenal_{yr}", "pitch-arsenal-stats",
                      {"type": "pitcher", "year": yr, "minPA": 25})
    if not rows:
        return None
    out = {}
    for r in rows:
        nm = (r.get("name") or r.get("player_name") or "").strip().lower()
        pt = (r.get("pitch_type") or r.get("pitch_name") or "").strip().upper()
        if not nm or not pt:
            continue
        out.setdefault(nm, {})[pt] = {
            "whiff_pct": _num(r.get("whiff_percent")),
            "k_pct": _num(r.get("k_percent")),
            "ba": _num(r.get("ba")),
            "woba": _num(r.get("woba")),
            "pa": _num(r.get("pa")),
        }
    return out or None


def arm_angles(yr=None):
    """Pitcher arm slot at release, in degrees. -> {player_lower: {arm_angle, ...}}"""
    yr = yr or season()
    rows = (_fetch_csv(f"arm_angle_{yr}", "pitcher-arm-angles", {"year": yr})
            or _fetch_csv(f"arm_angle_alt_{yr}", "arm-angles", {"year": yr}))
    if not rows:
        return None
    out = {}
    for r in rows:
        nm = (r.get("player_name") or r.get("name") or "").strip().lower()
        if not nm:
            continue
        ang = _num(r.get("ball_angle") or r.get("arm_angle"))
        if ang is None:
            continue
        out[nm] = {"arm_angle": ang,
                   "release_height": _num(r.get("relative_release_ball_height")
                                          or r.get("release_pos_z"))}
    return out or None


def pitch_movement(yr=None, pitch="FF"):
    """Movement vs. comparable pitches for one pitch type.
    -> {player_lower: {pitch, break_z_vs_avg, break_x_vs_avg, velo, spin}}"""
    yr = yr or season()
    rows = _fetch_csv(f"movement_{pitch}_{yr}", "pitch-movement",
                      {"year": yr, "pitch_type": pitch, "min": "q"})
    if not rows:
        return None
    out = {}
    for r in rows:
        nm = (r.get("name") or r.get("player_name") or "").strip().lower()
        if not nm:
            continue
        out[nm] = {
            "pitch": pitch,
            "break_z_vs_avg": _num(r.get("diff_z") or r.get("rise_diff")),
            "break_x_vs_avg": _num(r.get("diff_x") or r.get("break_x_diff")),
            "velo": _num(r.get("avg_speed") or r.get("velocity")),
            "spin": _num(r.get("avg_spin") or r.get("spin_rate")),
        }
    return out or None


# ----------------------------- lookups the app uses -----------------------------
def hitter_vs_pitch(batter, pitch_type, yr=None):
    """One hitter's real outcomes against one pitch type, or None."""
    a = batter_arsenal(yr)
    if not a or not batter:
        return None
    row = a.get(str(batter).strip().lower())
    if not row:
        return None
    return row.get(str(pitch_type).strip().upper())


def pitcher_profile(name, yr=None):
    """Everything we actually know about a pitcher's stuff: arm angle, per-pitch
    whiff rates, and fastball movement/spin. Only real values are included."""
    if not name:
        return None
    key = str(name).strip().lower()
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
    mv = pitch_movement(yr, "FF")
    if mv and key in mv:
        out["fastball"] = mv[key]
    return out or None


def status(yr=None):
    """Which Savant endpoints actually work from THIS server, with row counts and
    the columns they returned. Run this before trusting any of the above."""
    yr = yr or season()
    probes = {
        "batter_arsenal": lambda: batter_arsenal(yr),
        "pitcher_arsenal": lambda: pitcher_arsenal(yr),
        "arm_angles": lambda: arm_angles(yr),
        "pitch_movement_FF": lambda: pitch_movement(yr, "FF"),
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
