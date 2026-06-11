"""
narrate.py — turn the deterministic fact-sheet text into natural Claude prose.

Design notes:
  * The template functions (_long_reason, premium._render) already produce a
    correct, fact-complete sentence block. We hand THAT text to Claude and ask
    it only to rewrite it into vivid prose, under a strict "use only these
    facts" instruction. Claude never sees raw stats it could hallucinate from —
    just the finished notes — so it can't invent prices, injuries, or records.
  * Caching is keyed by a hash of (kind, sport, text). If the underlying facts
    change (odds move, the board reshuffles, the record updates), the text
    changes, the key changes, and we re-narrate. If nothing changed, it's a
    free cache hit — so a Best Bets board reload fires zero API calls.
  * A per-request budget caps how many *uncached* picks call Claude on a cold
    load, so the first page view stays fast. The background warmer (in main.py)
    fills the rest with no budget, so within a half hour the whole slate is
    narrated and cached.
  * If there's no key (llm is None) or anything errors, we return the original
    template text. The site never breaks or blocks on the API.

Env:
  AI_NARRATE_TTL   seconds a narration stays cached   (default 86400 = 24h)
"""

import os
import time
import hashlib

_CACHE: dict[str, tuple[float, str]] = {}
_TTL = int(os.environ.get("AI_NARRATE_TTL", "86400"))


def _key(kind, sport, text):
    raw = f"{kind}|{sport}|{text}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def _prompt(kind, sport, text):
    sp = (sport or "").upper()
    if kind == "premium":
        return (
            "You are a sharp sports-betting analyst writing the premium, "
            "subscriber-only note explaining why a pick rates as a 'best bet'. "
            "Rewrite the notes below into 2-3 confident, punchy sentences that "
            f"sell the edge on this {sp} play. Use ONLY the facts in the notes — "
            "do not invent or add stats, prices, injuries, records, or numbers. "
            "Keep every number exactly as written. No preamble, no bullet points.\n\n"
            f"Notes: {text}"
        )
    return (
        "You are a sharp sports-betting analyst. Rewrite the notes below into a "
        f"vivid, natural 2-4 sentence 'who wins and why' analysis of this {sp} "
        "matchup. Use ONLY the facts in the notes — do not invent or add stats, "
        "prices, injuries, records, or numbers, and keep every number exactly as "
        "written. No preamble, no bullet points, no headings.\n\n"
        f"Notes: {text}"
    )


def prose(text, *, kind, sport, llm, budget=None):
    """Return Claude-narrated prose for `text`, cached; fall back to `text`.

    kind:    "reason" (standard who-wins) or "premium" (best-bet note)
    llm:     the LLM_COMPLETE callable, or None to skip Claude entirely
    budget:  optional {"left": N} dict; decremented per real API call. When it
             hits 0, uncached items return the template (warmer fills later).
    """
    if not text or llm is None:
        return text
    k = _key(kind, sport, text)
    hit = _CACHE.get(k)
    if hit and (time.time() - hit[0] < _TTL):
        return hit[1]
    if budget is not None and budget.get("left", 0) <= 0:
        return text                       # cold + out of budget: template for now
    try:
        out = llm(_prompt(kind, sport, text))
        if budget is not None:
            budget["left"] = budget.get("left", 0) - 1
        if out and out.strip():
            p = out.strip()
            _CACHE[k] = (time.time(), p)
            return p
    except Exception as e:
        print(f"[ai] narrate {kind} failed: {e}")
    return text


def warm(text, *, kind, sport, llm):
    """Background warm: narrate ignoring any per-request budget."""
    return prose(text, kind=kind, sport=sport, llm=llm, budget=None)


def stats():
    """Small introspection helper (cache size), handy for a debug route."""
    return {"cached": len(_CACHE), "ttl": _TTL}
