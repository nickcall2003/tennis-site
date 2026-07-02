"""
promo_routes.py — turn today's REAL model picks into shareable posts.

Generates ready-to-post content (X/Twitter-sized and a longer Discord version)
straight from the model's actual predictions and track record, and can push the
Discord version to a webhook. Honest by design: only real picks/probabilities,
no guarantees or hype, and it leads with the transparent track record — the
account's genuine edge.

Env:
  DISCORD_WEBHOOK_URL  (optional) target channel webhook for auto-posting
  SITE_URL             (optional) link in posts, defaults to the live site
"""
import os
import datetime as dt

from fastapi import APIRouter

router = APIRouter()

_SITE = os.environ.get("SITE_URL", "https://www.thelinelogic.com")
_DISCORD = os.environ.get("DISCORD_WEBHOOK_URL", "")

_CONF_RANK = {"high": 2, "medium": 1, "low": 0}


def _top_plays(limit=5):
    """Best, most postable picks today: real picks ranked by confidence then
    probability, skipping low-interest ITF tennis noise."""
    try:
        import main
        plays = main._gather_plays(dt.date.today())
    except Exception:
        return []
    cand = []
    for p in plays:
        if (p.get("tier") or "").upper() == "ITF":
            continue
        prob = p.get("prob")
        if p.get("match") and p.get("pick") and prob is not None:
            score = float(prob) + 0.06 * _CONF_RANK.get(p.get("confidence"), 0)
            cand.append((score, p))
    cand.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in cand[:limit]]


def _calib_hook():
    try:
        import reports
        c = reports.calibration()
        n, brier = c.get("n"), c.get("brier")
        if n and n >= 20:
            return f"Every pick is tracked publicly \u2014 {n} scored so far (Brier {brier})."
    except Exception:
        pass
    return "Every pick is tracked publicly \u2014 no deleted losers."


def _pick_line(p):
    pick = (p.get("pick") or "").replace(" to win", "")
    pct = round(float(p.get("prob")) * 100)
    return pick, pct


def build_posts():
    picks = _top_plays(5)
    today = dt.date.today().strftime("%b %-d") if hasattr(dt.date.today(), "strftime") else ""
    calib = _calib_hook()
    if not picks:
        return {"has_picks": False,
                "x": f"No games on the Line Logic board today \u2014 back tomorrow. {_SITE}",
                "discord": f"No games on the board today. {_SITE}", "picks": []}

    # X / Twitter version — keep it tight (aim < 280 chars)
    x_lines = [f"\U0001F3AF Line Logic model picks \u2014 {today}", ""]
    for p in picks[:3]:
        name, pct = _pick_line(p)
        x_lines.append(f"\u2022 {name} ({pct}%)")
    x_lines += ["", calib.split(" \u2014 ")[0] + ".", _SITE]
    x_text = "\n".join(x_lines)
    if len(x_text) > 279:  # trim to 2 picks if needed
        x_lines = [f"\U0001F3AF Line Logic picks \u2014 {today}", ""] + \
                  [f"\u2022 {_pick_line(p)[0]} ({_pick_line(p)[1]}%)" for p in picks[:2]] + \
                  ["", _SITE]
        x_text = "\n".join(x_lines)

    # Discord version — richer
    d_lines = [f"**\U0001F3AF Line Logic \u2014 {today} Model Picks**", ""]
    for p in picks:
        name, pct = _pick_line(p)
        sport = (p.get("sport") or "").upper()
        conf = p.get("confidence")
        tag = f" \u00b7 {conf} confidence" if conf else ""
        d_lines.append(f"**{name}** ({pct}%) \u2014 {sport}{tag}")
    d_lines += ["", calib, f"Full board \u2192 {_SITE}"]
    d_text = "\n".join(d_lines)

    return {"has_picks": True, "x": x_text, "discord": d_text,
            "x_len": len(x_text),
            "picks": [{"pick": _pick_line(p)[0], "prob": _pick_line(p)[1],
                       "sport": p.get("sport"), "match": p.get("match")} for p in picks]}


@router.get("/api/promo/preview")
def preview():
    """Ready-to-post content generated from today's real picks."""
    return build_posts()


def _recap_data(day):
    """Graded picks for a given day: [{pick, prob, sport, won, match}, ...]."""
    from db import SessionLocal
    from models import LockedPickSet, PickResult
    import json
    lo = dt.datetime.combine(day, dt.time.min)
    hi = dt.datetime.combine(day, dt.time.max)
    out = []
    with SessionLocal() as db:
        try:
            row = (db.query(LockedPickSet)
                     .filter(LockedPickSet.view == "free",
                             LockedPickSet.pick_date >= lo, LockedPickSet.pick_date <= hi)
                     .first())
        except Exception:
            row = None
        if not row:
            return []
        try:
            plist = json.loads(row.payload)
        except Exception:
            return []
        outcomes = {(r.sport, str(r.ref)): r.correct for r in db.query(PickResult).all()}
    for p in plist:
        c = outcomes.get((p.get("sport"), str(p.get("id"))))
        if c is None:
            continue
        out.append({"pick": (p.get("pick") or "").replace(" to win", ""),
                    "prob": round(float(p.get("prob", 0)) * 100), "sport": p.get("sport"),
                    "won": bool(c), "match": p.get("match")})
    return out


def build_recap(offset=1):
    """Recap post for a prior day (default yesterday): W-L record + each result."""
    day = dt.date.today() - dt.timedelta(days=offset)
    try:
        label = day.strftime("%b %-d")
    except Exception:
        label = day.isoformat()
    res = _recap_data(day)
    if not res:
        return {"has_results": False,
                "x": f"No graded Line Logic picks for {label} yet. {_SITE}",
                "discord": f"No graded picks for {label} yet. {_SITE}", "results": []}
    w = sum(1 for r in res if r["won"])
    l = len(res) - w
    def mark(r):
        return "\u2705" if r["won"] else "\u274C"

    x = [f"\U0001F4C8 Line Logic recap \u2014 {label}", f"Model went {w}-{l}.", ""]
    for r in res[:4]:
        x.append(f"{mark(r)} {r['pick']} ({r['prob']}%)")
    x += ["", "Every result tracked \u2014 win or lose.", _SITE]
    x_text = "\n".join(x)
    if len(x_text) > 279:
        x = [f"\U0001F4C8 Line Logic \u2014 {label}: {w}-{l}", ""] + \
            [f"{mark(r)} {r['pick']}" for r in res[:4]] + ["", _SITE]
        x_text = "\n".join(x)

    d = [f"**\U0001F4C8 Line Logic Recap \u2014 {label}**", f"Model record: **{w}-{l}**", ""]
    for r in res:
        d.append(f"{mark(r)} **{r['pick']}** ({r['prob']}%) \u2014 {(r['sport'] or '').upper()}")
    d += ["", "Every pick tracked publicly \u2014 win or lose.", f"Today\u2019s board \u2192 {_SITE}"]
    return {"has_results": True, "x": x_text, "discord": "\n".join(d),
            "record": f"{w}-{l}", "results": res}


@router.get("/api/promo/recap")
def recap(offset: int = 1):
    """Ready-to-post recap of a prior day's graded picks (default yesterday)."""
    return build_recap(offset)


@router.post("/api/promo/discord")
def post_discord(kind: str = "picks"):
    """Push today's picks (kind=picks) or yesterday's recap (kind=recap) to Discord."""
    if not _DISCORD:
        return {"ok": False, "error": "DISCORD_WEBHOOK_URL not set"}
    posts = build_recap() if kind == "recap" else build_posts()
    try:
        import httpx
        r = httpx.post(_DISCORD, json={"content": posts["discord"][:1900]}, timeout=15.0)
        ok = r.status_code in (200, 204)
        return {"ok": ok, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
