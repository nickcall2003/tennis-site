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

from fastapi import APIRouter, Header

router = APIRouter()

_SITE = os.environ.get("SITE_URL", "https://www.thelinelogic.com")
_DISCORD = os.environ.get("DISCORD_WEBHOOK_URL", "")
_MENTION = os.environ.get("DISCORD_MENTION", "@here").strip()
_CRON_TOKEN = os.environ.get("PROMO_CRON_TOKEN", "").strip()
_ADMIN = os.environ.get("ADMIN_USERNAME", "").strip().lower()


def _is_admin(authorization):
    """True only for the owner account (ADMIN_USERNAME). If ADMIN_USERNAME is
    unset, nobody is admin \u2014 the promo tools stay locked by default."""
    if not _ADMIN:
        return False
    try:
        from db import SessionLocal
        import accounts
        with SessionLocal() as db:
            u = accounts._user_from_token(db, accounts._bearer(authorization))
        if not u:
            return False
        uname = (getattr(u, "username", "") or "").strip().lower()
        email = (getattr(u, "email", "") or "").strip().lower()
        return uname == _ADMIN or (email and email == _ADMIN)
    except Exception:
        return False

_CONF_RANK = {"high": 2, "medium": 1, "low": 0}


import re as _re
_ITF_RE = _re.compile(r"^[MW]\d{2,3}\b")


def _is_itf(p):
    """ITF detection that works on both live plays (tier) and stored picks
    (tournament name like 'M15 Skopje')."""
    if (p.get("tier") or "").upper() == "ITF":
        return True
    return bool(_ITF_RE.match((p.get("tournament") or "").strip()))


def _select_featured(picks, n=5):
    """Shared selection used by BOTH the morning picks post and the recap so they
    always reference the same set: drop ITF tennis, rank by probability (nudged by
    confidence), take the top n."""
    cand = []
    for p in picks:
        if _is_itf(p):
            continue
        prob = p.get("prob")
        if p.get("match") and p.get("pick") and prob is not None:
            score = float(prob) + 0.06 * _CONF_RANK.get(p.get("confidence"), 0)
            cand.append((score, p))
    cand.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in cand[:n]]


def _locked_picks_for(day):
    """The locked (tracked) pick set for a day, as raw pick dicts."""
    from db import SessionLocal
    from models import LockedPickSet
    import json
    lo = dt.datetime.combine(day, dt.time.min)
    hi = dt.datetime.combine(day, dt.time.max)
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
            return json.loads(row.payload)
        except Exception:
            return []


def _top_plays(limit=5):
    """Today's featured picks \u2014 taken from the locked (tracked) set so they match
    the recap exactly. Falls back to the live board only if today isn't locked yet."""
    picks = _locked_picks_for(dt.date.today())
    if not picks:
        try:
            import main
            picks = main._gather_plays(dt.date.today())
        except Exception:
            picks = []
    return _select_featured(picks, limit)


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


_HOOKS = [
    "\U0001F3AF The model\u2019s card is locked for {d}.",
    "\U0001F4CA Ran the {d} slate \u2014 here\u2019s where the model is sharpest:",
    "\U0001F9E0 {d} model reads. Numbers, not narratives:",
    "\U0001F525 Today\u2019s top model plays ({d}):",
    "\U0001F4C8 The model vs. the market \u2014 {d}:",
]


def _fair_line(prob):
    """Model-implied American odds from its probability (its own number)."""
    p = max(0.02, min(0.98, float(prob)))
    return f"-{round(100 * p / (1 - p))}" if p >= 0.5 else f"+{round(100 * (1 - p) / p)}"


def _hook(day_label):
    import hashlib
    idx = int(hashlib.md5(day_label.encode()).hexdigest(), 16) % len(_HOOKS)
    return _HOOKS[idx].format(d=day_label)


def build_posts():
    picks = _top_plays(5)
    try:
        today = dt.date.today().strftime("%b %-d")
    except Exception:
        today = dt.date.today().isoformat()
    calib = _calib_hook()
    if not picks:
        return {"has_picks": False,
                "x": f"No games on the Line Logic board today \u2014 back tomorrow. {_SITE}",
                "discord": f"No games on the board today. {_SITE}", "picks": []}

    top = picks[0]
    tname, tpct = _pick_line(top)
    hook = _hook(today)
    receipts = calib.split(" \u2014 ")[0]

    # X / Twitter \u2014 headline the strongest pick with its fair line, then quick hits
    def _x(n_extra):
        L = [hook, "", f"\u2B50 {tname} \u2014 model {tpct}% (fair {_fair_line(top['prob'])})"]
        for p in picks[1:1 + n_extra]:
            nm, pc = _pick_line(p)
            L.append(f"\u2022 {nm} \u2014 {pc}%")
        L += ["", f"{receipts} \u2014 receipts, not hype.", f"Full card \U0001F447", _SITE]
        return "\n".join(L)
    x_text = _x(2)
    if len(x_text) > 279:
        x_text = _x(1)
    if len(x_text) > 279:
        x_text = "\n".join([hook, "", f"\u2B50 {tname} \u2014 {tpct}% (fair {_fair_line(top['prob'])})", "", _SITE])

    # Discord \u2014 richer, with fair line + confidence
    d_lines = [f"**\U0001F3AF Line Logic \u2014 {today}**", "*Model picks. Every one graded in public.*", ""]
    for p in picks:
        nm, pc = _pick_line(p)
        sport = (p.get("sport") or "").upper()
        conf = p.get("confidence")
        tag = f" \u00b7 {conf} confidence" if conf else ""
        d_lines.append(f"**{nm}** \u2014 model {pc}% (fair {_fair_line(p['prob'])}) \u00b7 {sport}{tag}")
    d_lines += ["", calib, f"Full card \u2192 {_SITE}"]
    d_text = "\n".join(d_lines)

    return {"has_picks": True, "x": x_text, "discord": d_text, "x_len": len(x_text),
            "picks": [{"pick": _pick_line(p)[0], "prob": _pick_line(p)[1],
                       "sport": p.get("sport"), "match": p.get("match")} for p in picks]}


@router.get("/api/promo/allowed")
def promo_allowed(authorization: str | None = Header(None)):
    """Whether the current account may see the promo tools (owner only)."""
    return {"admin": _is_admin(authorization)}


@router.get("/api/promo/preview")
def preview(authorization: str | None = Header(None)):
    """Ready-to-post content generated from today's real picks (owner only)."""
    if not _is_admin(authorization):
        return {"error": "forbidden"}
    return build_posts()


def _recap_data(day):
    """Graded results for the SAME featured picks that were posted that day \u2014 uses
    the identical selection as the morning post, then joins outcomes."""
    from db import SessionLocal
    from models import PickResult
    featured = _select_featured(_locked_picks_for(day), 5)
    if not featured:
        return []
    with SessionLocal() as db:
        outcomes = {(r.sport, str(r.ref)): r.correct for r in db.query(PickResult).all()}
    out = []
    for p in featured:
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
def recap(offset: int = 1, authorization: str | None = Header(None)):
    """Ready-to-post recap of a prior day's graded picks (owner only)."""
    if not _is_admin(authorization):
        return {"error": "forbidden"}
    return build_recap(offset)


def _send_discord(kind):
    """Build and push a post to the Discord webhook. Returns a result dict."""
    if not _DISCORD:
        return {"ok": False, "error": "DISCORD_WEBHOOK_URL not set"}
    posts = build_recap() if kind == "recap" else build_posts()
    try:
        import httpx
        content = ((_MENTION + "\n") if _MENTION else "") + posts["discord"]
        r = httpx.post(_DISCORD, json={"content": content[:1980],
                                       "allowed_mentions": {"parse": ["everyone"]}}, timeout=15.0)
        return {"ok": r.status_code in (200, 204), "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.post("/api/promo/discord")
def post_discord(kind: str = "picks", authorization: str | None = Header(None),
                 x_promo_token: str | None = Header(None)):
    """Push today's picks (kind=picks) or yesterday's recap (kind=recap) to Discord.
    Allowed for the owner account, or for an automated job presenting the matching
    PROMO_CRON_TOKEN (via the X-Promo-Token header)."""
    cron_ok = bool(_CRON_TOKEN) and x_promo_token == _CRON_TOKEN
    if not (cron_ok or _is_admin(authorization)):
        return {"ok": False, "error": "forbidden"}
    return _send_discord(kind)


# ---- In-app daily scheduler (reliable; the app runs 24/7 on Railway) ---------
# Posts once per day per kind, at/after the target Central time. In-memory day
# tracking means a redeploy right after a post time could repost once \u2014 fine.
# Toggle with DISCORD_AUTO_POST=0; override times with PICKS_TIME / RECAP_TIME
# (e.g. "09:00", "23:30" in Central).
_AUTO = os.environ.get("DISCORD_AUTO_POST", "1").strip().lower() not in ("0", "false", "no", "off")
_last_auto = {}


def _parse_hhmm(s, default):
    try:
        h, m = s.split(":")
        return int(h), int(m)
    except Exception:
        return default


_POST_TIMES = {
    "picks": _parse_hhmm(os.environ.get("PICKS_TIME", "09:00"), (9, 0)),
    # the branded "Line Logic Projection" card — posts itself, no manual step
    "verdict": _parse_hhmm(os.environ.get("VERDICT_TIME", "10:00"), (10, 0)),
    "recap": _parse_hhmm(os.environ.get("RECAP_TIME", "23:30"), (23, 30)),
}


def _ct_now():
    import datetime as _d
    try:
        from zoneinfo import ZoneInfo
        return _d.datetime.now(ZoneInfo("America/Chicago"))
    except Exception:
        return _d.datetime.utcnow() - _d.timedelta(hours=5)


def _scheduler_loop():
    import time as _tt
    while True:
        try:
            if _DISCORD:
                now = _ct_now()
                today = now.date().isoformat()
                for kind, (hh, mm) in _POST_TIMES.items():
                    if _last_auto.get(kind) == today:
                        continue
                    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if now >= target:
                        _last_auto[kind] = today          # mark first to avoid double-fire
                        if kind == "verdict":
                            _send_verdict_discord()
                        else:
                            _send_discord(kind)
        except Exception:
            pass
        _tt.sleep(90)


def _start_scheduler():
    if not _AUTO:
        return
    try:
        import threading
        threading.Thread(target=_scheduler_loop, daemon=True).start()
    except Exception:
        pass


_start_scheduler()


# ===================== "Model Verdict" long-form pick format =====================
# Every field below comes from REAL model/provider data. Fields we don't actually
# track (playing style, altitude effects, injury status) are deliberately absent —
# a blank or invented bullet is worse than no bullet.

def _implied_pct(american):
    try:
        a = float(american)
    except (TypeError, ValueError):
        return None
    return round((100 / (a + 100) if a > 0 else abs(a) / (abs(a) + 100)) * 100, 1)


def _confidence_10(prob, edge_pct):
    """1-10 confidence from the model's own numbers: how far above a coin flip the
    pick is, plus how much it disagrees with the market. Capped, and deliberately
    conservative — a big 'edge' on a thin market is not high confidence."""
    try:
        p = float(prob)
        e = float(edge_pct or 0)
    except (TypeError, ValueError):
        return None
    base = (p - 0.5) * 12.0          # 50% -> 0, 80% -> 3.6
    mkt = min(max(e, 0), 15) / 15 * 4.0   # up to +4 for a real (not absurd) edge
    if e > 20:                        # off-market: the model probably has it wrong
        mkt = 0.5
    return round(min(10.0, max(1.0, 3.0 + base + mkt)), 1)


def _tennis_stat_block(provider, player_name, surface=None):
    """Real serve/return numbers from api-tennis match statistics. Returns only the
    fields that actually came back — never a blank placeholder."""
    try:
        a = provider.player_averages(player_name, surface=surface)
    except Exception:
        return {}
    if not a or not a.get("_matches"):
        return {}
    out = {}
    if a.get("service_points_pct") is not None:
        out["Service pts won"] = f"{a['service_points_pct']}%"
    if a.get("return_points_pct") is not None:
        out["Return pts won"] = f"{a['return_points_pct']}%"
    if a.get("first_serve_won_pct") is not None:
        out["1st serve won"] = f"{a['first_serve_won_pct']}%"
    if a.get("second_serve_won_pct") is not None:
        out["2nd serve won"] = f"{a['second_serve_won_pct']}%"
    bpw, bpc = a.get("break_points_won"), a.get("break_points_chances")
    if bpc:
        out["Break pts won"] = f"{round(100 * bpw / bpc)}% ({bpw}/{bpc})"
    bps, bpf = a.get("break_points_saved"), a.get("break_points_faced")
    if bpf:
        out["Break pts saved"] = f"{round(100 * bps / bpf)}% ({bps}/{bpf})"
    if a.get("aces") is not None:
        out["Aces/match"] = a["aces"]
    if a.get("double_faults") is not None:
        out["DFs/match"] = a["double_faults"]
    out["_n"] = a.get("_matches")
    out["_surface"] = a.get("_surface")
    return out


def _grade(conf):
    """Letter grade from the honest confidence score."""
    if conf is None:
        return None
    for cut, g in ((9.0, "A+"), (8.2, "A"), (7.5, "A-"), (6.8, "B+"), (6.0, "B"),
                   (5.2, "B-"), (4.4, "C+"), (3.6, "C"), (2.8, "C-")):
        if conf >= cut:
            return g
    return "D"


def _stars(conf):
    if conf is None:
        return ""
    filled = max(1, min(5, int(round(conf / 2.0))))
    return "\u2b50" * filled + "\u2606" * (5 - filled)


def verdict_post(p, provider=None):
    """One pick in the branded 'Line Logic Projection' format. Only real numbers."""
    pick, pct = _pick_line(p)
    prob = float(p.get("prob") or 0)
    mkt = p.get("market_odds")
    edge = p.get("edge_pct")
    conf = _confidence_10(prob, edge)
    lines = []
    lines.append(f"\U0001F3BE {pick} ML" + (f" ({_fmt_odds(mkt)})" if mkt is not None else ""))
    lines.append("")

    # --- real stat block (tennis) ---
    if p.get("sport") == "tennis" and provider is not None:
        stats = _tennis_stat_block(provider, pick, p.get("surface"))
        if stats:
            n, sfc = stats.pop("_n", None), stats.pop("_surface", None)
            lines.append("Key Stats" + (f" (last {n}{' on ' + sfc if sfc else ''})" if n else ""))
            for k, v in stats.items():
                lines.append(f"\u2022 {k}: {v}")
            lines.append("")
    if p.get("h2h"):
        lines.append(f"\u2022 H2H: {p['h2h']}")
        lines.append("")

    # --- verified conditions (elevation is a fact; effect is established physics) ---
    if p.get("sport") == "tennis":
        try:
            import tennis_conditions as TC
            tname = p.get("tournament") or ""
            sp = None
            if provider is not None:
                _a = provider.player_averages(pick, surface=p.get("surface")) or {}
                sp = _a.get("first_serve_won_pct")
            note = TC.altitude_edge(tname, sp) or TC.conditions_note(tname)
            if note:
                lines.append("Conditions")
                lines.append(f"\u2022 {note}")
                lines.append("")
        except Exception:
            pass

    # --- the branded projection block: aligned, every figure real ---
    lines.append("\U0001F4CA Line Logic Projection")
    lines.append("")
    rows = []
    if mkt is not None:
        rows.append(("Market Odds:", _fmt_odds(mkt)))
    rows.append(("Fair Odds:", _fair_line(prob)))
    rows.append(("Model Win %:", f"{round(prob * 100, 1)}%"))
    if mkt is not None:
        imp = _implied_pct(mkt)
        if imp is not None:
            rows.append(("Implied Win %:", f"{imp}%"))
    if edge is not None:
        rows.append(("Edge:", f"{'+' if float(edge) >= 0 else ''}{edge}%"))
    for k, v in rows:
        lines.append(f"{k:<17}{v}")
    lines.append("")
    if conf:
        lines.append(_stars(conf))
        lines.append(f"Confidence: {_grade(conf)}")
    # honesty guard: a huge model-vs-market gap usually means the MODEL is off
    try:
        if edge is not None and abs(float(edge)) > 20:
            lines.append("")
            lines.append("\u26A0\uFE0F Off-market: the model and the book disagree "
                         "sharply \u2014 treat as low-confidence, not a lock.")
    except (TypeError, ValueError):
        pass
    if p.get("units") and (not edge or abs(float(edge)) <= 20):
        lines.append(f"Play: Moneyline \u00b7 {p['units']}u")
    return "\n".join(lines)


def _fmt_odds(o):
    try:
        o = int(o)
    except (TypeError, ValueError):
        return str(o)
    return f"+{o}" if o > 0 else str(o)


@router.get("/api/promo/verdict")
def promo_verdict(sport: str = "tennis", n: int = 5, date: str | None = None,
                  token: str = "", authorization: str | None = Header(None)):
    """Long-form 'Model Verdict' posts for today's picks — the branded thread format.
    Every number is the model's own or the book's; nothing is invented."""
    ok = bool(_CRON_TOKEN) and (token or "").strip() == _CRON_TOKEN
    if not ok and not _is_admin(authorization):
        return {"error": "forbidden"}
    day = dt.date.fromisoformat(date) if date else dt.date.today()
    src = _locked_picks_for(day)
    if not src and day == dt.date.today():
        src = _top_plays(50)          # live board fallback (same as the daily post)
    picks = [p for p in src if not sport or p.get("sport") == sport]
    picks = _select_featured(picks, n)
    provider = None
    if sport == "tennis":
        try:
            import main as _m
            provider = getattr(_m, "provider", None)
        except Exception:
            provider = None
    posts = []
    for p in picks:
        try:
            posts.append({"pick": p.get("pick"), "text": verdict_post(p, provider)})
        except Exception as e:
            posts.append({"pick": p.get("pick"), "error": str(e)[:120]})
    # tail: the real tracked record
    rec = ""
    try:
        import reports
        from db import SessionLocal
        with SessionLocal() as db:
            r = reports.record(db) if hasattr(reports, "record") else None
        if r:
            rec = str(r)
    except Exception:
        rec = ""
    return {"date": day.isoformat(), "sport": sport, "count": len(posts),
            "posts": posts, "record_tail": rec,
            "note": "All figures are real: model probability, book odds, computed edge."}


def build_verdict_discord(day=None, per_sport=3):
    """The full 'Line Logic Projection' card for the day — Discord-ready. Used by
    the automatic daily post, so this format ships without anyone triggering it."""
    day = day or dt.date.today()
    src = _locked_picks_for(day)
    if not src and day == dt.date.today():
        src = _top_plays(50)
    if not src:
        return None
    provider = None
    try:
        import main as _m
        provider = getattr(_m, "provider", None)
    except Exception:
        provider = None
    by_sport = {}
    for p in src:
        by_sport.setdefault(p.get("sport") or "other", []).append(p)
    chunks = []
    for sport, picks in by_sport.items():
        top = _select_featured(picks, per_sport)
        for p in top:
            try:
                chunks.append(verdict_post(p, provider if sport == "tennis" else None))
            except Exception:
                continue
    if not chunks:
        return None
    sep = "\n\n" + ("\u2501" * 14) + "\n\n"
    try:
        label = day.strftime("%b %-d")
    except Exception:
        label = day.isoformat()
    head = f"\U0001F4C8 **Line Logic \u2014 {label} Model Card**\n"
    tail = "\n\n" + (_calib_hook() or "") + "\nEvery pick tracked \u2014 win or lose."
    return head + sep.join(chunks) + tail


def _send_verdict_discord():
    """Auto-post the branded projection card."""
    if not _DISCORD:
        return {"ok": False, "error": "no webhook"}
    body = build_verdict_discord()
    if not body:
        return {"ok": False, "error": "no picks"}
    try:
        import httpx
        content = ((_MENTION + "\n") if _MENTION else "") + body
        # Discord caps a message at 2000 chars — split into parts if needed
        parts, cur = [], ""
        for block in content.split("\n\n"):
            if len(cur) + len(block) + 2 > 1900:
                parts.append(cur)
                cur = block
            else:
                cur = (cur + "\n\n" + block) if cur else block
        if cur:
            parts.append(cur)
        ok = True
        for part in parts[:4]:
            r = httpx.post(_DISCORD, json={"content": part,
                                           "allowed_mentions": {"parse": ["everyone"]}},
                           timeout=15.0)
            ok = ok and r.status_code in (200, 204)
        return {"ok": ok, "parts": len(parts)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
