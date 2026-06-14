"""
soccer_stats.py — season team depth from FBref (fbref.com, Sports Reference).

FBref is free and its league/standings table already carries the depth that
matters for predictions: xG, xGA, xG-difference/90, goals for/against, W-D-L,
points and last-5 form — every team on one page. The squad standard table on
the same page adds possession. So one cached fetch per league per ~12h gives
depth for every team, which keeps us far under FBref's 10-requests/minute limit.

Parsing keys off FBref's stable `data-stat` attributes (not column order), and
HTML comments are stripped first (FBref defers some tables inside comments).
Used only on soccer match-detail opens (lazy + cached), like the UFC tale.
"""
from __future__ import annotations

import os
import re
import time

FBREF = "https://fbref.com"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_TTL = int(os.environ.get("SOCCER_STATS_TTL", "43200"))   # 12h
_MIN_GAP = float(os.environ.get("SOCCER_STATS_MIN_GAP", "4"))  # polite spacing

# our league key -> FBref "compId/Slug-Stats" (no year = current season)
COMP = {
    "epl":          "9/Premier-League-Stats",
    "laliga":       "12/La-Liga-Stats",
    "seriea":       "11/Serie-A-Stats",
    "bundesliga":   "20/Bundesliga-Stats",
    "ligue1":       "13/Ligue-1-Stats",
    "ucl":          "8/Champions-League-Stats",
    "uel":          "19/Europa-League-Stats",
    "uecl":         "882/Conference-League-Stats",
    "mls":          "22/Major-League-Soccer-Stats",
    "ligamx":       "31/Liga-MX-Stats",
    "championship": "10/Championship-Stats",
    "eredivisie":   "23/Eredivisie-Stats",
    "ligaportugal": "32/Primeira-Liga-Stats",
    "saudi":        "70/Saudi-Professional-League-Stats",
    "worldcup":     "1/World-Cup-Stats",
}

_ALIAS = {
    "utd": "united", "wolves": "wolverhampton", "spurs": "tottenham",
    "inter": "internazionale", "psg": "parissaintgermain", "atleti": "atletico",
    "nottham": "nottingham", "brighton": "brighton", "leeds": "leeds",
    "gladbach": "monchengladbach", "dortmund": "dortmund",
}
# keep "city"/"united"/"real" etc. as distinguishing tokens (Man City vs Man Utd)
_FILLER = {"fc", "cf", "sc", "afc", "club", "the"}

_cache = {}        # league -> (ts, table dict)
_last_fetch = [0.0]


def enabled() -> bool:
    return os.environ.get("SOCCER_STATS_ENABLED", "1") == "1" and bool(_UA)


def _get(url, ttl=_TTL):
    now = time.time()
    gap = now - _last_fetch[0]
    if gap < _MIN_GAP:
        time.sleep(_MIN_GAP - gap)
    try:
        import httpx
        _last_fetch[0] = time.time()
        r = httpx.get(url, headers={"User-Agent": _UA}, timeout=20,
                      follow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"[soccer_stats] GET failed {url}: {e}")
        return ""


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


def _cells(row):
    out = {}
    for m in re.finditer(r'data-stat="([^"]+)"[^>]*>(.*?)</t[hd]>', row, re.S):
        stat = m.group(1)
        text = re.sub(r"<[^>]+>", "", m.group(2))
        out[stat] = re.sub(r"\s+", " ", text).strip()
    return out


def _tokens(name):
    n = (name or "").lower()
    n = (n.replace("&", " and ").replace(".", " ").replace("-", " ")
         .replace("'", ""))
    n = re.sub(r"[^a-z0-9 ]", "", n)
    toks = set()
    for t in n.split():
        t = _ALIAS.get(t, t)
        if t and t not in _FILLER and len(t) >= 2:
            toks.add(t)
    return toks


def _parse(html):
    html = html.replace("<!--", "").replace("-->", "")
    tables = re.findall(r'<table[^>]*\bid="([^"]+)"[^>]*>(.*?)</table>', html, re.S)
    standings, poss_rows = None, {}
    for tid, body in tables:
        if standings is None and ("xg_for" in body or "_overall" in tid):
            standings = body
        if "possession" in body and ("standard" in tid or not poss_rows):
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
                c = _cells(row)
                if c.get("team") and c.get("possession"):
                    poss_rows[c["team"]] = _num(c["possession"])
    table = {}
    if standings:
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", standings, re.S):
            c = _cells(row)
            team = c.get("team")
            if not team or not c.get("games"):
                continue
            mp = _num(c.get("games"))
            rec = {
                "team": team,
                "rank": _num(c.get("rank")),
                "mp": mp,
                "w": _num(c.get("wins")), "d": _num(c.get("ties")), "l": _num(c.get("losses")),
                "pts": _num(c.get("points")),
                "gf": _num(c.get("goals_for")), "ga": _num(c.get("goals_against")),
                "xg": _num(c.get("xg_for")), "xga": _num(c.get("xg_against")),
                "xgd90": _num(c.get("xg_diff_per90")),
                "form": c.get("last_5") or "",
                "poss": poss_rows.get(team),
            }
            if mp and mp > 0:
                for k_pg, k in (("gf_pg", "gf"), ("ga_pg", "ga"),
                                ("xg_pg", "xg"), ("xga_pg", "xga")):
                    rec[k_pg] = round(rec[k] / mp, 2) if rec[k] is not None else None
                rec["ppg"] = round(rec["pts"] / mp, 2) if rec["pts"] is not None else None
            table[team] = rec
    return table


def get_table(league_key):
    if not enabled() or league_key not in COMP:
        return {}
    c = _cache.get(league_key)
    if c and time.time() - c[0] < _TTL:
        return c[1]
    html = _get(f"{FBREF}/en/comps/{COMP[league_key]}", ttl=_TTL)
    table = _parse(html) if html else {}
    if table:
        _cache[league_key] = (time.time(), table)
    elif c:
        return c[1]
    return table


def _match(name, table):
    target = _tokens(name)
    if not target:
        return None
    best, score = None, 0
    for fb, rec in table.items():
        inter = target & _tokens(fb)
        s = sum(len(t) for t in inter)
        if s > score:
            score, best = s, rec
    return best if score >= 4 else None


def team_depth(league_key, name):
    return _match(name, get_table(league_key))


def _insight(h, a):
    bits = []
    last = lambda r: r.get("team", "")
    if h.get("xg_pg") is not None and a.get("xg_pg") is not None and abs(h["xg_pg"] - a["xg_pg"]) >= 0.25:
        hi = h if h["xg_pg"] > a["xg_pg"] else a
        bits.append(f"{last(hi)} create more ({hi['xg_pg']:.2f} xG/game)")
    if h.get("xga_pg") is not None and a.get("xga_pg") is not None and abs(h["xga_pg"] - a["xga_pg"]) >= 0.25:
        lo = h if h["xga_pg"] < a["xga_pg"] else a
        bits.append(f"{last(lo)} concede less ({lo['xga_pg']:.2f} xGA/game)")
    if h.get("ppg") is not None and a.get("ppg") is not None and abs(h["ppg"] - a["ppg"]) >= 0.4:
        hi = h if h["ppg"] > a["ppg"] else a
        bits.append(f"{last(hi)} are in better form ({hi['ppg']:.2f} pts/game)")
    return ("Season depth: " + "; ".join(bits[:3]) + ".") if bits else ""


def match_depth(league_key, home_name, away_name):
    table = get_table(league_key)
    if not table:
        return None
    h = _match(home_name, table)
    a = _match(away_name, table)
    if not h and not a:
        return None
    out = {"home": h, "away": a, "source": "FBref"}
    if h and a:
        out["insight"] = _insight(h, a)
    return out
