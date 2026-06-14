"""
ufcstats.py — career fight metrics scraped from ufcstats.com (the UFC's official
stats site). No API/key; public factual stats. HTML there is long-stable, but we
parse defensively (regex, no bs4 dependency) and cache hard since a fighter's
career numbers only move after a fight.

Gives the "why" numbers API-Sports' free tier doesn't: significant strikes
landed/absorbed per minute, striking accuracy/defense, takedowns per 15 min,
takedown accuracy/defense, submission average — plus a clean record + bio.
"""
from __future__ import annotations

import os
import re
import time
from urllib.parse import quote

BASE = "http://ufcstats.com"
SEARCH = BASE + "/statistics/fighters/search"
_TTL = int(os.environ.get("UFCSTATS_TTL", "86400"))     # 24h
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}
_last = {"url": None, "status": None, "bytes": 0, "error": None}
_attempts = []
_cache = {}          # url -> (ts, html)
_resolve = {}        # normalized name -> (ts, fighter_url|None)
_stats = {}          # fighter_url -> (ts, stats dict)


def enabled() -> bool:
    return os.environ.get("UFCSTATS_ENABLED", "1") == "1"


def _norm(s):
    return "".join(c for c in (s or "").lower() if c.isalnum())


def _get(url, ttl=_TTL):
    c = _cache.get(url)
    if c and time.time() - c[0] < ttl:
        return c[1]
    try:
        import httpx
        r = httpx.get(url, headers=_HEADERS, timeout=15, follow_redirects=True)
        _last.update(url=url, status=r.status_code, bytes=len(r.text), error=None)
        r.raise_for_status()
        html = r.text
        _cache[url] = (time.time(), html)
        return html
    except Exception as e:
        _last.update(url=url, error=str(e))
        print(f"[ufcstats] GET failed {url}: {e}")
        return c[1] if c else ""


def _resolve_from(html, toks):
    """Find the fighter-details URL whose grouped name links contain all tokens."""
    pairs = re.findall(
        r'href="(https?://ufcstats\.com/fighter-details/[a-zA-Z0-9]+)"[^>]*>\s*([^<]*?)\s*</a>',
        html, re.I)
    groups, order = {}, []
    for href, txt in pairs:
        if href not in groups:
            groups[href] = []
            order.append(href)
        groups[href].append(_norm(txt))
    for href in order:
        joined = "".join(groups[href])
        if toks and all(t in joined for t in toks):
            return href
    return None


def _fighter_url(name):
    """Resolve a fighter to their ufcstats fighter-details URL via the A-Z browse
    (the ?query= search doesn't return results over GET)."""
    key = _norm(name)
    c = _resolve.get(key)
    if c and time.time() - c[0] < _TTL:
        return c[1]
    parts = [p for p in (name or "").split() if p]
    toks = [_norm(p) for p in parts]
    cands = []
    if parts:
        li = parts[-1][0].lower()                       # last-name initial
        cands.append(f"{BASE}/statistics/fighters?char={li}&page=all")
        fi = parts[0][0].lower()                         # first-name initial (fallback)
        if fi != li:
            cands.append(f"{BASE}/statistics/fighters?char={fi}&page=all")
        cands.append(SEARCH + "?query=" + quote(parts[-1]))
    url = None
    _attempts.clear()
    for u in cands:
        html = _get(u, ttl=_TTL)
        n_links = len(re.findall(r"fighter-details/[a-zA-Z0-9]+", html or ""))
        hit = _resolve_from(html, toks) if html else None
        _attempts.append({"url": u, "bytes": len(html or ""), "links_seen": n_links,
                          "matched": hit})
        if hit:
            url = hit
            break
    _resolve[key] = (time.time(), url)
    return url


def _val(html, frag):
    m = re.search(r'b-list__box-item-title[^>]*>\s*' + frag + r'\s*</i>\s*([^<\n]+)',
                  html, re.I)
    if not m:
        return None
    v = m.group(1).strip()
    return v if v and v not in ("--", "—") else None


def _parse(html):
    out = {}
    rec = re.search(r'b-content__title-record"[^>]*>\s*Record:\s*([^<]+)</span>', html, re.I)
    if rec:
        out["record"] = rec.group(1).strip()
    bio = [("height", r"Height:"), ("weight", r"Weight:"), ("reach", r"Reach:"),
           ("stance", r"STANCE:"), ("dob", r"DOB:")]
    for k, frag in bio:
        v = _val(html, frag)
        if v:
            out[k] = v
    metrics = [("slpm", r"SLpM:"), ("str_acc", r"Str\.\s*Acc\.:"),
               ("sapm", r"SApM:"), ("str_def", r"Str\.\s*Def\.?:"),
               ("td_avg", r"TD\s*Avg\.:"), ("td_acc", r"TD\s*Acc\.:"),
               ("td_def", r"TD\s*Def\.:"), ("sub_avg", r"Sub\.\s*Avg\.:")]
    for k, frag in metrics:
        v = _val(html, frag)
        if v:
            out[k] = v
    return out


def get_stats(name):
    """Career metrics + bio for a fighter, or None. Cached 24h per fighter."""
    if not enabled() or not name:
        return None
    url = _fighter_url(name)
    if not url:
        return None
    c = _stats.get(url)
    if c and time.time() - c[0] < _TTL:
        return c[1]
    html = _get(url, ttl=_TTL)
    if not html:
        return None
    data = _parse(html)
    data = data or None
    _stats[url] = (time.time(), data)
    return data


def diag(name="Ilia Topuria"):
    """Resolve a fighter and report what ufcstats returned (for debugging)."""
    url = _fighter_url(name)
    stats = get_stats(name) if url else None
    return {
        "enabled": enabled(),
        "query": name,
        "resolved_url": url,
        "attempts": _attempts,
        "stats_found": list((stats or {}).keys()),
        "stats": stats,
        "fetch": _last,
    }
