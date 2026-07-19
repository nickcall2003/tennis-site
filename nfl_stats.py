"""
nfl_stats.py — NFL advanced data from nflverse (free, GitHub-hosted, no key).

WHAT THIS GIVES US THAT ESPN DOESN'T
  * EPA per play (passing / rushing / receiving) — the single best public measure
    of play quality
  * Target share, air yards share, WOPR — how central a receiver actually is
  * Next Gen Stats — CPOE, time to throw, separation, yards over expected
  * Snap counts — real usage, not guesses

Same rules as the other data modules:
  * Every function returns None on failure and NEVER raises.
  * Disk-cached (these are season files; they change weekly at most).
  * `status()` reports which files loaded, their row counts, and their real column
    names — so a renamed file shows up as a broken source, not as silent zeros.

Kill switch: NFL_STATS=0
Cache path:  NFL_STATS_PATH (default /data/nfl_stats.json)
"""
import csv
import datetime as dt
import io
import json
import os
import time

_ENABLED = os.environ.get("NFL_STATS", "1").strip().lower() not in ("0", "false", "no")
_STORE = os.environ.get("NFL_STATS_PATH", "/data/nfl_stats.json")
_TTL = int(os.environ.get("NFL_STATS_TTL", str(12 * 3600)))

BASE = "https://github.com/nflverse/nflverse-data/releases/download"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "text/csv,application/octet-stream,*/*",
}

_mem = {}
_health = {}


def season_year(when=None):
    """NFL seasons are named for the September they start in. Before September we
    are still describing last season."""
    d = when or dt.date.today()
    return d.year if d.month >= 9 else d.year - 1


def _disk():
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


def _fetch_csv(key, candidates, timeout=45.0, nocache=False, max_rows=40000,
               disk_cache=True):
    """Download an nflverse CSV -> list[dict]. Tries plain .csv then .csv.gz.
    Returns None on ANY failure."""
    if not _ENABLED:
        return None
    now = time.time()
    hit = _mem.get(key)
    if hit and not nocache and now - hit[0] < _TTL:
        _health[key] = {"ok": True, "rows": len(hit[1]), "source": "memory",
                        "cols": list(hit[1][0].keys())[:40] if hit[1] else []}
        return hit[1]
    blob = _disk()
    d = blob.get(key)
    if (isinstance(d, dict) and not nocache and now - (d.get("t") or 0) < _TTL
            and d.get("rows")):
        _mem[key] = (d["t"], d["rows"])
        _health[key] = {"ok": True, "rows": len(d["rows"]), "source": "disk",
                        "cols": list(d["rows"][0].keys())[:40] if d["rows"] else []}
        return d["rows"]
    try:
        import httpx
        text, used, tried = None, None, []
        if isinstance(candidates, tuple):
            candidates = [candidates]
        for asset, filename in candidates:
            for suffix in ("", ".gz"):
                url = f"{BASE}/{asset}/{filename}{suffix}"
                tried.append(url)
                try:
                    r = httpx.get(url, headers=_HEADERS, timeout=timeout,
                                  follow_redirects=True)
                except Exception:
                    continue
                if r.status_code != 200:
                    continue
                raw = r.content
                if suffix == ".gz":
                    import gzip
                    try:
                        raw = gzip.decompress(raw)
                    except Exception:
                        continue
                text = raw.decode("utf-8-sig", errors="replace")
                used = url
                break
            if text:
                break
        if not text:
            _health[key] = {"ok": False, "error": "all candidates 404",
                            "tried": tried[:8]}
            return None
        rows = []
        for i, row in enumerate(csv.DictReader(io.StringIO(text))):
            if i >= max_rows:
                break
            rows.append(row)
        if not rows:
            _health[key] = {"ok": False, "error": "0 rows"}
            return None
        _health[key] = {"ok": True, "rows": len(rows), "source": "network",
                        "url": used, "cols": list(rows[0].keys())[:30]}
        _mem[key] = (now, rows)
        if disk_cache:
            blob[key] = {"t": now, "rows": rows}
            _disk_save(blob)
        return rows
    except Exception as e:
        _health[key] = {"ok": False, "error": str(e)[:120]}
        return None


def _num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


# ----------------------------- sources -----------------------------
def player_week_stats(year=None, nocache=False):
    """Weekly player stats: EPA, target share, air yards share, WOPR, and the
    standard box-score lines."""
    year = year or season_year()
    return _fetch_csv(f"player_stats_{year}_v2", [
        ("stats_player", f"stats_player_week_{year}.csv"),
        ("stats_player", f"stats_player_season_{year}.csv"),
        ("player_stats", f"player_stats_{year}.csv"),
        ("player_stats", "player_stats.csv"),
    ], nocache=nocache, disk_cache=False)   # ~20MB/season raw; we cache the aggregate


def ngs(kind="passing", year=None, nocache=False):
    """Next Gen Stats: passing (CPOE, time to throw), receiving (separation,
    YAC over expected), rushing (yards over expected)."""
    year = year or season_year()
    return _fetch_csv(f"ngs_{kind}_{year}_v2", [
        ("nextgen_stats", f"ngs_{kind}.csv"),
        ("nextgen_stats", f"ngs_{year}_{kind}.csv"),
        ("nextgen_stats", f"ngs_{kind}_{year}.csv"),
    ], nocache=nocache)


def snap_counts(year=None, nocache=False):
    """Real snap participation — the honest version of 'is he a starter'."""
    year = year or season_year()
    return _fetch_csv(f"snaps_{year}", [
        ("snap_counts", f"snap_counts_{year}.csv"),
    ], nocache=nocache)


# ----------------------------- aggregation -----------------------------
# canonical field -> the column names nflverse has used for it
_SUM_ALIASES = {
    "completions": ["completions"],
    "attempts": ["attempts", "passing_attempts"],
    "passing_yards": ["passing_yards"],
    "passing_tds": ["passing_tds"],
    "interceptions": ["passing_interceptions", "interceptions"],
    "sacks_taken": ["sacks_suffered", "sacks", "sacks_taken"],
    "carries": ["carries", "rushing_attempts"],
    "rushing_yards": ["rushing_yards"],
    "rushing_tds": ["rushing_tds"],
    "receptions": ["receptions"],
    "targets": ["targets"],
    "receiving_yards": ["receiving_yards"],
    "receiving_tds": ["receiving_tds"],
    "passing_epa": ["passing_epa"],
    "rushing_epa": ["rushing_epa"],
    "receiving_epa": ["receiving_epa"],
    "passing_air_yards": ["passing_air_yards"],
    "receiving_air_yards": ["receiving_air_yards"],
}
_AVG_ALIASES = {
    "target_share": ["target_share"],
    "air_yards_share": ["air_yards_share"],
    "wopr": ["wopr", "wopr_x", "wopr_y"],
    "pacr": ["pacr", "pacr_x"],
    "racr": ["racr"],
    "dakota": ["dakota"],
    "passing_cpoe": ["passing_cpoe"],
}


def _pick(row, names):
    for n in names:
        if n in row:
            v = _num(row.get(n))
            if v is not None:
                return v
    return None


def season_players(year=None, nocache=False):
    """Season totals per player, aggregated from the weekly file.
    The AGGREGATE is what gets cached to disk (a few hundred KB) rather than the
    raw weekly rows (~20MB a season), so multi-year career lookups stay cheap.
    -> {player_lower: {games, team, position, ...totals..., ...rates...}}"""
    year = year or season_year()
    akey = f"agg_{year}_v2"
    if not nocache:
        cached = _mem.get(akey)
        if cached and time.time() - cached[0] < _TTL:
            return cached[1]
        blob = _disk()
        d = blob.get(akey)
        if isinstance(d, dict) and d.get("agg"):
            _mem[akey] = (d.get("t") or time.time(), d["agg"])
            return d["agg"]
    rows = player_week_stats(year, nocache=nocache)
    if not rows:
        return None
    agg = {}
    for r in rows:
        nm = (r.get("player_display_name") or r.get("player_name") or "").strip()
        if not nm:
            continue
        k = nm.lower()
        a = agg.setdefault(k, {"name": nm, "games": 0, "_avg": {}, "_avg_n": {}})
        a["games"] += 1
        a["team"] = r.get("recent_team") or r.get("team") or a.get("team")
        a["position"] = r.get("position") or a.get("position")
        a["player_id"] = r.get("player_id") or a.get("player_id")
        for f, names in _SUM_ALIASES.items():
            v = _pick(r, names)
            if v is not None:
                a[f] = round(a.get(f, 0.0) + v, 2)
        for f, names in _AVG_ALIASES.items():
            v = _pick(r, names)
            if v is not None:
                a["_avg"][f] = a["_avg"].get(f, 0.0) + v
                a["_avg_n"][f] = a["_avg_n"].get(f, 0) + 1
    for a in agg.values():
        for f, tot in (a.pop("_avg", {}) or {}).items():
            n = (a.get("_avg_n") or {}).get(f) or 0
            if n:
                a[f] = round(tot / n, 3)
        a.pop("_avg_n", None)
        g = a.get("games") or 0
        if g:
            for f in ("passing_epa", "rushing_epa", "receiving_epa"):
                if a.get(f) is not None:
                    a[f + "_per_game"] = round(a[f] / g, 3)
    if agg:
        try:
            blob = _disk()
            blob[akey] = {"t": time.time(), "agg": agg}
            _disk_save(blob)
            _mem[akey] = (time.time(), agg)
        except Exception:
            pass
    return agg or None


def player_profile(name, year=None):
    """One player's season profile: totals, per-game EPA, usage, and Next Gen
    Stats where they exist."""
    sp = season_players(year)
    if not sp or not name:
        return None
    key = str(name).strip().lower()
    row = sp.get(key)
    if not row:
        for k, v in sp.items():                 # last-name fallback
            if key in k or k in key:
                row = v
                break
    if not row:
        return None
    out = dict(row)
    pos = (row.get("position") or "").upper()
    kind = ("passing" if pos == "QB" else
            "rushing" if pos in ("RB", "FB") else "receiving")
    try:
        rows = ngs(kind, year) or []
        best = None
        for r in rows:
            nm = (r.get("player_display_name") or r.get("player_name") or "").strip().lower()
            if nm == key and str(r.get("week") or "0") in ("0", ""):
                best = r                        # week 0 = season aggregate
                break
        if best:
            out["nextgen"] = {k: v for k, v in best.items()
                              if v not in (None, "") and k not in
                              ("player_display_name", "player_name", "season",
                               "week", "season_type", "player_gsis_id")}
    except Exception:
        pass
    return out


def status(year=None):
    """Which nflverse files load from THIS server, with row counts and columns."""
    year = year or season_year()
    res = {}
    for label, fn in (("player_stats", lambda: player_week_stats(year)),
                      ("ngs_passing", lambda: ngs("passing", year)),
                      ("ngs_receiving", lambda: ngs("receiving", year)),
                      ("snap_counts", lambda: snap_counts(year))):
        t0 = time.time()
        try:
            d = fn()
        except Exception as e:
            d = None
            _health[label] = {"ok": False, "error": str(e)[:120]}
        res[label] = {"rows": len(d) if d else 0, "secs": round(time.time() - t0, 1)}
    try:
        sp = season_players(year)
        res["season_players"] = {"players": len(sp) if sp else 0}
        if sp:
            k = next(iter(sp))
            res["sample_player"] = {k: {kk: vv for kk, vv in list(sp[k].items())[:26]}}
    except Exception as e:
        res["season_players"] = {"error": str(e)[:120]}
    return {"enabled": _ENABLED, "season": year, "results": res,
            "endpoint_health": _health}


_CAREER_BACK = int(os.environ.get("NFL_CAREER_SEASONS", "6"))


def career_profile(name, back=None):
    """Career totals + season-by-season for one player, aggregated across the last
    N season files (default 6, via NFL_CAREER_SEASONS).

    Honest limit: nflverse publishes a file per season, so 'career' here means the
    seasons we actually loaded — not a player's full history. The span is returned
    so the UI can label it truthfully.
    """
    if not name:
        return None
    back = back or _CAREER_BACK
    cur = season_year()
    key = str(name).strip().lower()
    totals, years, seasons_used = {}, [], []
    for yr in range(cur, cur - back, -1):
        try:
            sp = season_players(yr)
        except Exception:
            sp = None
        if not sp:
            continue
        row = sp.get(key)
        if not row:
            continue
        seasons_used.append(yr)
        yr_row = {"season": yr}
        for k, v in row.items():
            if isinstance(v, (int, float)) and not k.endswith("_per_game"):
                yr_row[k] = v
                # rate stats shouldn't be summed across seasons
                if k not in ("target_share", "air_yards_share", "wopr", "pacr",
                             "racr", "dakota", "passing_cpoe"):
                    totals[k] = round(totals.get(k, 0.0) + v, 2)
        yr_row["team"] = row.get("team")
        years.append(yr_row)
    if not years:
        return None
    years.sort(key=lambda r: -r["season"])
    return {"name": name, "career": totals, "by_season": years,
            "seasons_covered": sorted(seasons_used),
            "span": f"{min(seasons_used)}\u2013{max(seasons_used)}"}


def prop_context(name, stat):
    """A real, sourced context line for an NFL player prop — or None.
    Uses only measured usage/efficiency: target share, WOPR, air-yards share,
    separation, CPOE. Never a narrative claim."""
    prof = player_profile(name)
    if not prof:
        return None
    pos = (prof.get("position") or "").upper()
    st = (stat or "").lower()
    ng = prof.get("nextgen") or {}
    bits = []

    def f(v, nd=2):
        try:
            return round(float(v), nd)
        except (TypeError, ValueError):
            return None

    if pos in ("WR", "TE", "RB") and ("recept" in st or "receiv" in st or "yard" in st):
        ts = f(prof.get("target_share"), 3)
        if ts:
            bits.append(f"{round(ts * 100, 1)}% target share")
        wo = f(prof.get("wopr"), 2)
        if wo:
            bits.append(f"{wo} WOPR")
        sep = f(ng.get("avg_separation"))
        if sep:
            bits.append(f"{sep} yds separation")
        yac = f(ng.get("avg_yac_above_expectation"))
        if yac is not None:
            bits.append(f"{'+' if yac >= 0 else ''}{yac} YAC over expected")
    elif pos == "QB":
        cp = f(prof.get("passing_cpoe"))
        if cp is not None:
            bits.append(f"{'+' if cp >= 0 else ''}{cp}% CPOE")
        tt = f(ng.get("avg_time_to_throw"))
        if tt:
            bits.append(f"{tt}s to throw")
        epg = f(prof.get("passing_epa_per_game"))
        if epg is not None:
            bits.append(f"{'+' if epg >= 0 else ''}{epg} EPA/game")
    elif pos in ("RB", "FB"):
        epg = f(prof.get("rushing_epa_per_game"))
        if epg is not None:
            bits.append(f"{'+' if epg >= 0 else ''}{epg} rush EPA/game")
    if not bits:
        return None
    return " \u00b7 ".join(bits) + f" (last {prof.get('games', 0)} games)"
