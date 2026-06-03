"""
warrennolan.py — College baseball RPI / ELO enrichment from WarrenNolan.com.

IMPORTANT HONESTY / ETIQUETTE NOTES:
- Warren Nolan has NO public API. These ratings (RPI, ELO, SOS) are their own
  computed, proprietary work and are the core value of their free site.
- We therefore read it RESPECTFULLY: cached 12 hours (their ratings only move
  after games finish, so this is plenty), a single page fetch per refresh, a
  descriptive User-Agent, and we ATTRIBUTE the data to Warren Nolan in the UI.
- This is enrichment only. If the fetch/parse fails (e.g. they change their HTML,
  or block us), college baseball still works on ESPN records alone. We never let
  a Warren Nolan failure break the page.

Ratings are keyed by a normalized team name so we can match ESPN's team names.
"""
from __future__ import annotations

import re
import time
import html as _html

# RPI rankings page (one table, all D1 teams). We parse team name + RPI rank.
RPI_URL = "https://www.warrennolan.com/baseball/2026/rpi-live"
ELO_URL = "https://www.warrennolan.com/baseball/2026/elo-live"

_cache = {"ts": 0.0, "rpi": {}, "elo": {}}
_TTL = 12 * 3600     # ratings only change after games; twice a day is plenty


def _norm(name):
    """Normalize a team name for matching across ESPN and Warren Nolan."""
    n = (name or "").lower()
    n = n.replace("&", "and")
    n = re.sub(r"[^a-z0-9 ]", "", n)
    # common school-name noise
    for w in (" university", " univ", " state university"):
        pass
    n = re.sub(r"\s+", " ", n).strip()
    return n


_breaker = {"open_until": 0.0}


def _fetch(url):
    import httpx
    r = httpx.get(url, timeout=6,
                  headers={"User-Agent": "LineLogic/1.0 (personal sports model; contact via site)"})
    r.raise_for_status()
    return r.text


def _parse_rpi(html_text):
    """
    Parse the RPI table into {norm_name: {'rpi_rank': int, 'rpi': float,
    'record': str, 'sos': float}}. Robust to markup drift: we look for rows
    that contain a team link and numeric cells, and fail soft (return {}).
    """
    out = {}
    # Warren Nolan rows typically: rank, team (link), record, RPI value, SOS...
    # We capture team-name link text and the surrounding numbers conservatively.
    row_re = re.compile(
        r'(\d{1,3})\s*</td>.*?<a[^>]*>([^<]+)</a>.*?'
        r'(\d{1,3}-\d{1,3}(?:-\d{1,2})?)?.*?'
        r'(0?\.\d{3,5})',
        re.S)
    for m in row_re.finditer(html_text):
        try:
            rank = int(m.group(1))
            name = _html.unescape(m.group(2)).strip()
            record = (m.group(3) or "").strip()
            rpi = float(m.group(4))
        except (ValueError, TypeError):
            continue
        if not name or rank > 320:
            continue
        out[_norm(name)] = {"rpi_rank": rank, "rpi": rpi, "record": record}
    return out


def _load():
    if time.time() - _cache["ts"] < _TTL and _cache["rpi"]:
        return
    if time.time() < _breaker["open_until"]:
        return   # cooling down after a failure; skip the fetch
    try:
        rpi = _parse_rpi(_fetch(RPI_URL))
        if rpi:
            _cache["rpi"] = rpi
            _cache["ts"] = time.time()
    except Exception as e:
        print(f"[warrennolan] rpi load failed: {e}")
        _breaker["open_until"] = time.time() + 600   # 10 min cooldown


def get_rating(team_name):
    """Return {'rpi_rank', 'rpi', 'record'} for a team, or {} if unavailable."""
    _load()
    if not _cache["rpi"]:
        return {}
    key = _norm(team_name)
    if key in _cache["rpi"]:
        return _cache["rpi"][key]
    # try a loose contains-match (e.g. "North Carolina" vs "North Carolina Tar Heels")
    for k, v in _cache["rpi"].items():
        if k and (k in key or key in k):
            return v
    return {}


def available():
    _load()
    return bool(_cache["rpi"])
