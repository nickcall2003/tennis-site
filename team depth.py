"""
team_depth.py — season team depth for NBA / NFL / NHL / MLB from the
Sports-Reference family (basketball/pro-football/hockey/baseball-reference),
the same publisher and HTML structure as FBref. One cached page per league
per ~6h gives every team's depth; parsing keys off stable `data-stat`
attributes and strips HTML comments first (SR defers some tables in comments).

Emits the same generic shape soccer uses, so one frontend panel renders all:
  {source, insight, away:{name,bio:[[label,val]...]}, home:{...},
   bars:[{label, away, home, lower_better}]}

Used only on match-detail opens (lazy + cached). SR asks bots to stay under
~20 req/min on these sites; one cached fetch per league makes that trivial.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import time

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_TTL = int(os.environ.get("TEAM_DEPTH_TTL", "21600"))   # 6h
_MIN_GAP = float(os.environ.get("TEAM_DEPTH_MIN_GAP", "4"))
_last = [0.0]
_last_fetch = {"url": None, "status": None, "bytes": 0, "error": None}
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}
_cache = {}        # sport -> (ts, parsed)


def enabled() -> bool:
    return os.environ.get("TEAM_DEPTH_ENABLED", "1") == "1"


def _season_year(sport, d=None):
    d = d or dt.date.today()
    y, m = d.year, d.month
    if sport in ("nba", "nhl"):
        return y + 1 if m >= 10 else y
    if sport == "nfl":
        return y if m >= 9 else y - 1
    return y  # mlb


# bar = (candidate data-stats, label, lower_is_better, per_game, table_id|None)
CFG = {
    "nba": {
        "base": "https://www.basketball-reference.com",
        "path": lambda y: f"/leagues/NBA_{y}_ratings.html",
        "tables": ["ratings"],
        "src": "Basketball-Reference",
        "record": ("wins", "losses", None),
        "bars": [
            (["off_rtg"], "Offensive rating", False, False, None),
            (["def_rtg"], "Defensive rating", True, False, None),
            (["net_rtg"], "Net rating", False, False, None),
            (["mov"], "Margin of victory", False, False, None),
        ],
    },
    "nhl": {
        "base": "https://www.hockey-reference.com",
        "path": lambda y: f"/leagues/NHL_{y}.html",
        "tables": ["stats"],
        "src": "Hockey-Reference",
        "record": ("wins", "losses", None),
        "bars": [
            (["goals_per_game", "goals_for_per_game"], "Goals / game", False, False, None),
            (["opp_goals_per_game", "goals_against_per_game"], "Opp goals / game", True, False, None),
            (["power_play_pct", "pp_pct"], "Power play %", False, False, None),
            (["pen_kill_pct", "pk_pct"], "Penalty kill %", False, False, None),
        ],
    },
    "nfl": {
        "base": "https://www.pro-football-reference.com",
        "path": lambda y: f"/years/{y}/",
        "tables": ["AFC", "NFC"],
        "src": "Pro-Football-Reference",
        "record": ("wins", "losses", "ties"),
        "bars": [
            (["points"], "Points / game", False, True, None),
            (["points_opp"], "Points allowed / game", True, True, None),
            (["srs_total", "srs"], "SRS (overall)", False, False, None),
            (["mov"], "Margin of victory", False, False, None),
        ],
    },
    "mlb": {
        "base": "https://www.baseball-reference.com",
        "path": lambda y: f"/leagues/majors/{y}.shtml",
        "tables": ["teams_standard_batting", "teams_standard_pitching"],
        "src": "Baseball-Reference",
        "record": ("W", "L", None),
        "bars": [
            (["R", "runs"], "Runs / game", False, True, "teams_standard_batting"),
            (["onbase_plus_slugging", "OPS"], "OPS", False, False, "teams_standard_batting"),
            (["earned_run_avg", "ERA", "era"], "ERA", True, False, "teams_standard_pitching"),
            (["whip", "WHIP"], "WHIP", True, False, "teams_standard_pitching"),
        ],
    },
}

_ALIAS = {"utd": "united"}
_FILLER = {"fc", "sc", "the"}


def _get(url):
    now = time.time()
    if now - _last[0] < _MIN_GAP:
        time.sleep(_MIN_GAP - (now - _last[0]))
    try:
        import httpx
        _last[0] = time.time()
        r = httpx.get(url, headers=_HEADERS, timeout=20, follow_redirects=True)
        _last_fetch.update(url=url, status=r.status_code, bytes=len(r.text), error=None)
        r.raise_for_status()
        return r.text
    except Exception as e:
        _last_fetch.update(url=url, error=str(e))
        print(f"[team_depth] GET failed {url}: {e}")
        return ""


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except Exception:
        return None


def _cells(row):
    out = {}
    for m in re.finditer(r'data-stat="([^"]+)"[^>]*>(.*?)</t[hd]>', row, re.S):
        out[m.group(1)] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip()
    return out


def _team_of(c):
    for k in ("team_name", "team", "squad", "franchise", "team_ID"):
        if c.get(k):
            return c[k]
    return None


def _tokens(name):
    n = re.sub(r"[^a-z0-9 ]", "", (name or "").lower().replace("&", " and ").replace(".", " ").replace("-", " "))
    out = set()
    for t in n.split():
        t = _ALIAS.get(t, t)
        if t and t not in _FILLER and len(t) >= 2:
            out.add(t)
    return out


def _parse(sport):
    cfg = CFG[sport]
    html = _get(cfg["base"] + cfg["path"](_season_year(sport)))
    if not html:
        return {}
    html = html.replace("<!--", "").replace("-->", "")
    tables = dict(re.findall(r'<table[^>]*\bid="([^"]+)"[^>]*>(.*?)</table>', html, re.S))
    parsed = {}
    for tid in cfg["tables"]:
        body = tables.get(tid)
        if not body:
            continue
        d = {}
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
            c = _cells(row)
            tm = _team_of(c)
            if tm and (c.get("wins") or c.get("W") or c.get("games") or c.get("g")):
                d[tm] = c
        parsed[tid] = d
    return parsed


def get(sport):
    if not enabled() or sport not in CFG:
        return {}
    c = _cache.get(sport)
    if c and time.time() - c[0] < _TTL:
        return c[1]
    parsed = _parse(sport)
    if parsed and any(parsed.values()):
        _cache[sport] = (time.time(), parsed)
    elif c:
        return c[1]
    return parsed


def _all_teams(parsed):
    names = {}
    for d in parsed.values():
        for tm in d:
            names[tm] = True
    return list(names)


def _match(name, parsed):
    target = _tokens(name)
    if not target:
        return None
    best, score = None, 0
    for tm in _all_teams(parsed):
        s = sum(len(t) for t in (target & _tokens(tm)))
        if s > score:
            score, best = s, tm
    return best if score >= 4 else None


def _stat(parsed, cfg, team, stats, table):
    tids = [table] if table else cfg["tables"]
    for tid in tids:
        rec = parsed.get(tid, {}).get(team)
        if rec:
            for s in stats:
                if rec.get(s) not in (None, ""):
                    return rec[s]
    return None


def _games(parsed, cfg, team):
    w, l, t = cfg["record"]
    for tid in cfg["tables"]:
        rec = parsed.get(tid, {}).get(team)
        if rec:
            if rec.get("games"):
                return _num(rec["games"])
            if rec.get("g"):
                return _num(rec["g"])
            wn, ln = _num(rec.get(w)), _num(rec.get(l))
            tn = _num(rec.get(t)) if t else 0
            if wn is not None and ln is not None:
                return wn + ln + (tn or 0)
    return None


def _record(parsed, cfg, team):
    w, l, t = cfg["record"]
    for tid in cfg["tables"]:
        rec = parsed.get(tid, {}).get(team)
        if rec and rec.get(w) is not None:
            s = f"{rec.get(w)}-{rec.get(l)}"
            if t and rec.get(t) not in (None, "", "0"):
                s += f"-{rec.get(t)}"
            return s
    return ""


def _barval(parsed, cfg, team, bar):
    stats, _label, _lb, per_game, table = bar
    v = _num(_stat(parsed, cfg, team, stats, table))
    if v is not None and per_game:
        g = _games(parsed, cfg, team)
        if g:
            v = round(v / g, 2)
    return v


def _insight(bars, an, hn):
    bits = []
    for b in bars:
        a, h = b.get("away"), b.get("home")
        if a is None or h is None or a == h:
            continue
        better_away = (a < h) if b["lower_better"] else (a > h)
        who = an if better_away else hn
        bits.append(f"{who.split()[-1]} lead {b['label'].lower()} ({(a if better_away else h)})")
        if len(bits) >= 3:
            break
    return ("Season depth: " + "; ".join(bits) + ".") if bits else ""


def match_depth(sport, home_name, away_name):
    parsed = get(sport)
    if not parsed or not any(parsed.values()):
        return None
    cfg = CFG[sport]
    hk, ak = _match(home_name, parsed), _match(away_name, parsed)
    if not hk and not ak:
        return None
    bars = []
    for bar in cfg["bars"]:
        av = _barval(parsed, cfg, ak, bar) if ak else None
        hv = _barval(parsed, cfg, hk, bar) if hk else None
        if av is None and hv is None:
            continue
        bars.append({"label": bar[1], "away": av, "home": hv, "lower_better": bar[2]})
    if not bars:
        return None

    def bio(team):
        return [["Record", _record(parsed, cfg, team)]] if team else []
    out = {
        "source": cfg["src"],
        "away": {"name": away_name, "bio": bio(ak)},
        "home": {"name": home_name, "bio": bio(hk)},
        "bars": bars,
    }
    if hk and ak:
        out["insight"] = _insight(bars, away_name, home_name)
    return out


def diag(sport):
    parsed = get(sport)
    sample = {}
    for tid, d in (parsed or {}).items():
        sample[tid] = {"teams": len(d), "first": (list(d)[0] if d else None),
                       "stats": (sorted(list(d[list(d)[0]].keys()))[:25] if d else [])}
    return {"enabled": enabled(), "sport": sport,
            "season_year": _season_year(sport), "url": CFG[sport]["base"] + CFG[sport]["path"](_season_year(sport)) if sport in CFG else None,
            "tables": sample, "fetch": _last_fetch}
