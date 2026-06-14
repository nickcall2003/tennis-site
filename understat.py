"""
understat.py — season xG / xGA per team from understat.com (big-5 leagues).

Understat embeds its data as JSON inside a <script> (`var teamsData =
JSON.parse('...')`), so parsing is robust (no HTML-structure dependency). It's a
separate site from Sports-Reference, so it should serve a cloud server. Covers
EPL, La Liga, Bundesliga, Serie A, Ligue 1 only.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time

_TTL = int(os.environ.get("UNDERSTAT_TTL", "43200"))   # 12h
_NEG_TTL = 900
_cache = {}
_last = {"url": None, "status": None, "bytes": 0, "error": None}

LEAGUE = {"epl": "EPL", "laliga": "La_liga", "bundesliga": "Bundesliga",
          "seriea": "Serie_A", "ligue1": "Ligue_1"}

_FILLER = {"fc", "sc", "the"}
_ALIAS = {"utd": "united", "wolves": "wolverhampton", "spurs": "tottenham"}


def enabled() -> bool:
    return os.environ.get("UNDERSTAT_ENABLED", "0") == "1"


def supported(league_key):
    return league_key in LEAGUE


def _season_year(d=None):
    d = d or dt.date.today()
    return d.year if d.month >= 8 else d.year - 1


def _get(url):
    try:
        import httpx
        r = httpx.get(url, timeout=15, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
        _last.update(url=url, status=r.status_code, bytes=len(r.text), error=None)
        r.raise_for_status()
        return r.text
    except Exception as e:
        _last.update(url=url, error=str(e))
        print(f"[understat] GET failed {url}: {e}")
        return ""


def _tokens(name):
    n = re.sub(r"[^a-z0-9 ]", "", (name or "").lower().replace("&", " and ").replace(".", " ").replace("-", " "))
    return {(_ALIAS.get(t, t)) for t in n.split() if t not in _FILLER and len(t) >= 2}


def get_table(league_key):
    if not enabled() or league_key not in LEAGUE:
        return {}
    c = _cache.get(league_key)
    if c:
        age = time.time() - c[0]
        if c[1] and age < _TTL:
            return c[1]
        if not c[1] and age < _NEG_TTL:
            return {}
    url = f"https://understat.com/league/{LEAGUE[league_key]}/{_season_year()}"
    html = _get(url)
    table = {}
    if html:
        m = (re.search(r"var\s+teamsData\s*=\s*JSON\.parse\('(.*?)'\)", html, re.S)
             or re.search(r"teamsData\s*=\s*JSON\.parse\('(.*?)'\)", html, re.S)
             or re.search(r"teamsData\s*=\s*JSON\.parse\(\"(.*?)\"\)", html, re.S))
        if m:
            try:
                blob = m.group(1)
                try:
                    blob = blob.encode("utf-8").decode("unicode_escape")
                except Exception:
                    pass
                data = json.loads(blob)
                for td in data.values():
                    hist = td.get("history", [])
                    mp = len(hist)
                    if not mp:
                        continue
                    xg = sum(float(h.get("xG", 0)) for h in hist)
                    xga = sum(float(h.get("xGA", 0)) for h in hist)
                    table[td.get("title", "")] = {
                        "mp": mp, "xg": round(xg, 1), "xga": round(xga, 1),
                        "xg_pg": round(xg / mp, 2), "xga_pg": round(xga / mp, 2),
                    }
            except Exception as e:
                print(f"[understat] parse failed: {e}")
    _cache[league_key] = (time.time(), table)
    return table


def _match(name, table):
    target = _tokens(name)
    if not target:
        return None
    best, score = None, 0
    for nm, rec in table.items():
        s = sum(len(t) for t in (target & _tokens(nm)))
        if s > score:
            score, best = s, rec
    return best if score >= 4 else None


def xg_bars(league_key, home_name, away_name):
    """Return [xG/game, xGA/game] bars for merging into the depth panel, or []."""
    table = get_table(league_key)
    if not table:
        return []
    h = _match(home_name, table)
    a = _match(away_name, table)
    if not h or not a:
        return []
    return [
        {"label": "xG / game", "away": a["xg_pg"], "home": h["xg_pg"], "lower_better": False},
        {"label": "xGA / game", "away": a["xga_pg"], "home": h["xga_pg"], "lower_better": True},
    ]


def diag(league_key="epl"):
    table = get_table(league_key)
    info = {"enabled": enabled(), "supported": supported(league_key),
            "league": league_key, "season": _season_year(), "teams": len(table),
            "sample": list(table.items())[:2], "fetch": dict(_last)}
    if league_key in LEAGUE:
        html = _get(f"https://understat.com/league/{LEAGUE[league_key]}/{_season_year()}")
        info["has_teamsData"] = ("teamsData" in html)
        info["title_in_page"] = ("Understat" in html or "understat" in html)
        info["snippet"] = html[:240]
    return info
