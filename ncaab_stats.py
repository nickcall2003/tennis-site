"""
ncaab_stats.py — College basketball adjusted efficiency from Bart Torvik (free).

WHY TORVIK
KenPom is the gold standard but paid. Bart Torvik publishes the same class of
data — adjusted offensive/defensive efficiency, tempo, and a win-expectancy
rating (barthag) — for free, with no API key.

WHAT IT FEEDS
ncaab_provider.predict() already runs an adjusted-efficiency model; it just reads
a hand-built JSON file. This module rebuilds that file from live data, exactly
like ncaaf_stats does for SP+.

Same rules as the other data modules:
  * Returns None on failure, never raises.
  * Cached; ratings move daily at most during the season.
  * `status()` reports the source URL, row counts and the real columns, so a
    changed feed shows up as a broken source instead of silent zeros.

Kill switch: NCAAB_STATS=0
"""
import csv
import datetime as dt
import io
import json
import os
import time
import unicodedata

_ENABLED = os.environ.get("NCAAB_STATS", "1").strip().lower() not in ("0", "false", "no")
_TTL = int(os.environ.get("NCAAB_STATS_TTL", str(12 * 3600)))

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "text/csv,text/plain,*/*",
}

_cache = {}
_health = {}


def season_year(when=None):
    """College basketball seasons are named for the year they END in.
    Nov 2025 -> the 2026 season."""
    d = when or dt.date.today()
    return d.year + 1 if d.month >= 11 else d.year


def _norm_team(s):
    """Match ncaab_provider's normalizer so keys line up."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def _num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def fetch_trank(year=None, nocache=False):
    """Torvik's team table. Tries the headered CSV first, then the raw T-Rank
    export. Returns {"rows":[...], "url":..., "mode":"header"|"positional"}."""
    if not _ENABLED:
        return None
    year = year or season_year()
    key = f"trank_{year}"
    now = time.time()
    hit = _cache.get(key)
    if hit and not nocache and now - hit[0] < _TTL:
        _health[key] = {"ok": True, "source": "cache",
                        "rows": len(hit[1].get("rows") or [])}
        return hit[1]
    candidates = [
        f"https://barttorvik.com/{year}_team_results.csv",
        f"https://barttorvik.com/trank.php?year={year}&csv=1",
    ]
    try:
        import httpx
    except Exception as e:
        _health[key] = {"ok": False, "error": f"httpx missing: {e}"}
        return None
    tried = []
    for url in candidates:
        tried.append(url)
        try:
            r = httpx.get(url, headers=_HEADERS, timeout=30.0,
                          follow_redirects=True)
        except Exception as e:
            _health[key] = {"ok": False, "error": str(e)[:100], "tried": tried}
            continue
        if r.status_code != 200 or not (r.text or "").strip():
            _health[key] = {"ok": False, "error": f"HTTP {r.status_code}",
                            "tried": tried}
            continue
        text = r.text
        first = text.split("\n", 1)[0].lower()
        # a header row will contain letters like "adjoe"/"team"; the raw export
        # starts straight into data
        has_header = any(tok in first for tok in
                         ("adjoe", "adj_o", "team", "rank", "barthag"))
        if has_header:
            rows = list(csv.DictReader(io.StringIO(text)))
            mode = "header"
            cols = list(rows[0].keys())[:30] if rows else []
        else:
            rows = [r_ for r_ in csv.reader(io.StringIO(text)) if r_]
            mode = "positional"
            cols = rows[0][:30] if rows else []
        if not rows:
            continue
        out = {"rows": rows, "url": url, "mode": mode}
        _health[key] = {"ok": True, "source": "network", "url": url,
                        "mode": mode, "rows": len(rows), "cols": cols}
        _cache[key] = (now, out)
        return out
    return None


# Column names Torvik has used for the values we need, in preference order.
_COL_ALIASES = {
    "team": ["team", "TeamName", "School"],
    "conf": ["conf", "Conference"],
    "adjoe": ["adjoe", "adj_o", "AdjOE", "adjusted offensive efficiency"],
    "adjde": ["adjde", "adj_d", "AdjDE", "adjusted defensive efficiency"],
    "tempo": ["adj_t", "adjtempo", "AdjTempo", "tempo"],
    "barthag": ["barthag", "Barthag", "wab"],
}


def _pick(row, names):
    for n in names:
        if n in row:
            return row[n]
    # case-insensitive fallback
    low = {str(k).lower(): v for k, v in row.items()}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return None


def team_ratings(year=None):
    """-> {norm_team: {name, off, def, tempo, net, conf}} or None.
    Only parses the headered form; positional data is reported by status() so the
    column order can be mapped deliberately rather than guessed."""
    data = fetch_trank(year)
    if not data or data.get("mode") != "header":
        return None
    out = {}
    for r in data["rows"]:
        name = _pick(r, _COL_ALIASES["team"])
        o = _num(_pick(r, _COL_ALIASES["adjoe"]))
        d = _num(_pick(r, _COL_ALIASES["adjde"]))
        if not name or o is None or d is None:
            continue
        t = _num(_pick(r, _COL_ALIASES["tempo"]))
        out[_norm_team(name)] = {
            "name": name, "off": o, "def": d,
            "tempo": t, "net": round(o - d, 2),
            "conf": _pick(r, _COL_ALIASES["conf"]),
            "barthag": _num(_pick(r, _COL_ALIASES["barthag"])),
        }
    return out or None


def refresh_ratings_file(year=None, path=None):
    """Rebuild the JSON that ncaab_provider.predict() reads, then hot-reload it."""
    year = year or season_year()
    teams = team_ratings(year)
    if not teams:
        d = fetch_trank(year)
        return {"ok": False, "season": year,
                "error": ("no parsable ratings — see status() for the column "
                          "layout that came back"),
                "mode": (d or {}).get("mode"), "url": (d or {}).get("url")}
    offs = [v["off"] for v in teams.values() if v.get("off") is not None]
    tempos = [v["tempo"] for v in teams.values() if v.get("tempo") is not None]
    blob = {
        "teams": teams,
        "avg_eff": round(sum(offs) / len(offs), 2) if offs else 104.0,
        "tempo": round(sum(tempos) / len(tempos), 2) if tempos else 68.0,
        "season": year,
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    target = path or os.environ.get("NCAAB_RATINGS_PATH") or (
        "/data/ncaab_ratings.json" if os.path.isdir("/data") else "ncaab_ratings.json")
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "w") as f:
            json.dump(blob, f)
    except Exception as e:
        return {"ok": False, "error": f"write failed: {str(e)[:120]}",
                "path": target}
    reloaded = False
    try:
        import ncaab_provider as NP
        NP.reload()
        reloaded = True
    except Exception:
        pass
    return {"ok": True, "season": year, "teams": len(teams), "path": target,
            "avg_eff": blob["avg_eff"], "tempo": blob["tempo"],
            "provider_reloaded": reloaded}


def status(year=None):
    """What Torvik actually returned from this server."""
    year = year or season_year()
    d = fetch_trank(year, nocache=True)
    res = {"enabled": _ENABLED, "season": year,
           "fetched": bool(d), "health": _health}
    if d:
        res["url"] = d.get("url")
        res["mode"] = d.get("mode")
        res["row_count"] = len(d.get("rows") or [])
        rows = d.get("rows") or []
        res["first_row"] = rows[0] if rows else None
        res["second_row"] = rows[1] if len(rows) > 1 else None
        t = team_ratings(year)
        res["parsed_teams"] = len(t) if t else 0
        if t:
            k = next(iter(t))
            res["sample"] = {k: t[k]}
    return res
