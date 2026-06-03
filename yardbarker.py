"""
Yardbarker headline feed (trades, signings, free agency, transfer portal).

There is NO official Yardbarker API or RSS, so this parses the public HTML
section pages and extracts story headlines + links. This is inherently more
fragile than a structured API: if Yardbarker redesigns their markup, the
extraction can return nothing until the pattern is updated. It is wrapped so a
failure NEVER breaks the site -- it just yields an empty list.

We surface headline + source link only (linking back to Yardbarker), not the
article body, which is both safer and the honest way to aggregate.
"""
import re
import time
import html as _html
import httpx

_SECTION = {
    "nfl": "nfl", "mlb": "mlb", "nba": "nba",
    "ncaaf": "college_football", "ncaab": "college_basketball",
}
_BASE = "https://www.yardbarker.com/"
_cache = {}        # league -> (ts, items)
_TTL = 1800        # 30 min

# markdown/href link whose URL points at a Yardbarker /articles/ story
_LINK = re.compile(r'\[([^\]]+)\]\((https://www\.yardbarker\.com/[a-z_]+/articles/[^)]+)\)')
_HREF = re.compile(r'href="(https://www\.yardbarker\.com/[a-z_]+/articles/[^"]+)"[^>]*>([^<]+)<')

# keywords that mark a story as transaction/news (vs quiz/listicle/opinion)
_NEWSY = ("trade", "traded", "sign", "signs", "signed", "signing", "deal",
          "extension", "waive", "release", "released", "acquire", "acquired",
          "portal", "transfer", "commits", "commit", "injury", "injured",
          "out", "return", "agree", "agrees", "free agent", "claim", "activate",
          "designate", "option", "promote", "call up", "ruled out")


def _looks_newsy(title, url):
    t = title.lower()
    if "quiz" in url.lower() or "quiz" in t:
        return False
    if t.startswith("the '") or t.startswith("the \u2018"):
        return False
    # keep if it hits a transaction/news keyword; otherwise treat as soft-news
    return any(k in t for k in _NEWSY)


def get_headlines(league: str, limit: int = 20, only_transactions: bool = False):
    if league not in _SECTION:
        return []
    c = _cache.get(league)
    if c and time.time() - c[0] < _TTL:
        return c[1]
    url = _BASE + _SECTION[league]
    try:
        r = httpx.get(url, timeout=12,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; LineLogic/1.0)"})
        r.raise_for_status()
        body = r.text
    except Exception:
        return []
    items = []
    seen = set()
    for pat in (_LINK, _HREF):
        for m in pat.finditer(body):
            if pat is _LINK:
                title, link = m.group(1), m.group(2)
            else:
                link, title = m.group(1), m.group(2)
            title = _html.unescape(title).strip()
            if not title or link in seen or len(title) < 12:
                continue
            seen.add(link)
            newsy = _looks_newsy(title, link)
            if only_transactions and not newsy:
                continue
            # derive a rough tag from the URL section
            seg = link.split("yardbarker.com/")[1].split("/")[0]
            items.append({"headline": title, "url": link,
                          "transaction": newsy,
                          "section": seg.replace("_", " ").upper()})
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break
    # transactions first, then the rest
    items.sort(key=lambda x: (not x["transaction"]))
    _cache[league] = (time.time(), items)
    return items
