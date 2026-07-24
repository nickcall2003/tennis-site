"""
ops_routes.py — one URL that tells you whether Line Logic is actually healthy.

main.py has always tried to import this; it just never existed, so the startup
log has been printing "ops router not loaded" every boot.

WHY THIS EXISTS
Running this solo from a phone, the failures that actually cost you are the
quiet ones: SGO silently in a 429 cooldown, the Odds API quota gone for the
month, a model file that didn't load so a sport is running on fallbacks. None
of those crash anything — the site keeps serving, just worse. This surfaces all
of it in a single request you can bookmark.

    /api/ops/health          everything, with an overall status
    /api/ops/health?quiet=1  only the things that are NOT ok

Deliberately read-only and side-effect free: it inspects module state that's
already in memory and never triggers a fetch. Checking your health should not
itself burn an API credit.
"""
from __future__ import annotations

import datetime as dt
import os
import time

from fastapi import APIRouter

router = APIRouter()

_BOOT = time.time()


def _ok(name, detail="", **extra):
    return dict(check=name, status="ok", detail=detail, **extra)


def _warn(name, detail="", **extra):
    return dict(check=name, status="warn", detail=detail, **extra)


def _fail(name, detail="", **extra):
    return dict(check=name, status="fail", detail=detail, **extra)


def _check_odds_api():
    try:
        import odds_api
    except Exception as e:
        return _fail("odds_api", f"module import failed: {e}")
    if not odds_api.enabled():
        return _warn("odds_api", "no ODDS_API_KEY set — team-sport lines rely on SGO alone")
    q = getattr(odds_api, "_quota", {}) or {}
    rem, used = q.get("remaining"), q.get("used")
    if rem is None:
        return _ok("odds_api", "enabled; no quota reading yet this boot")
    try:
        rem_i = int(rem)
    except (TypeError, ValueError):
        return _ok("odds_api", f"enabled; remaining={rem}")
    if rem_i <= 0:
        return _fail("odds_api", "quota exhausted — team-sport odds will be blank",
                     remaining=rem_i, used=used)
    if rem_i < 500:
        return _warn("odds_api", "quota running low", remaining=rem_i, used=used)
    return _ok("odds_api", "healthy", remaining=rem_i, used=used)


def _check_sgo():
    try:
        import sgo_api
    except Exception as e:
        return _fail("sgo", f"module import failed: {e}")
    if not sgo_api.enabled():
        return _warn("sgo", "no SPORTSGAMEODDS_KEY set")
    cooling = not sgo_api.available()
    if cooling:
        until = getattr(sgo_api, "_cooldown_until", 0)
        secs = max(0, int(until - time.time()))
        # not a failure: the Odds API fallback covers this, which is exactly
        # why the cooldown exists rather than hammering a rate-limited host
        return _warn("sgo", f"in 429 cooldown for another {secs}s — "
                            f"falling back to The Odds API", cooldown_s=secs)
    cached = len(getattr(sgo_api, "_events_cache", {}) or {})
    return _ok("sgo", "available", cached_leagues=cached)


def _check_model_files():
    """The failures that don't crash anything but quietly degrade every pick."""
    out = []
    try:
        import predictions  # noqa
        eng = None
        for attr in ("ENGINE", "engine", "_engine"):
            eng = getattr(predictions, attr, None)
            if eng is not None:
                break
        n = None
        for attr in ("ratings", "_ratings"):
            r = getattr(eng, attr, None) if eng is not None else None
            if isinstance(r, dict):
                n = len(r)
                break
        if n:
            out.append(_ok("tennis_ratings", f"{n:,} precomputed player ratings loaded"))
        else:
            out.append(_warn("tennis_ratings",
                             "no precomputed ratings — tennis is running "
                             "ranking-only (rebuild + commit ratings.json)"))
    except Exception as e:
        out.append(_warn("tennis_ratings", f"not inspectable: {e}"))

    try:
        import ncaab_provider as NP
        d = NP._load() or {}
        teams = len(d.get("teams") or {})
        if teams:
            out.append(_ok("ncaab_ratings", f"{teams} teams"))
        else:
            out.append(_warn("ncaab_ratings",
                             "no adjusted-efficiency file — NCAAB falls back to Elo"))
    except Exception:
        out.append(_ok("ncaab_ratings", "provider not loaded (out of season)"))
    return out


def _check_db():
    try:
        from db import SessionLocal
        with SessionLocal() as db:
            db.execute(__import__("sqlalchemy").text("SELECT 1"))
        return _ok("database", "reachable")
    except Exception as e:
        return _fail("database", f"unreachable: {e}")


def _check_ai():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return _warn("ai_writeups", "no ANTHROPIC_API_KEY — write-ups disabled")
    return _ok("ai_writeups", "key present (credit balance isn't visible from here)")


def _check_discord():
    if not os.environ.get("DISCORD_WEBHOOK_URL") and not os.environ.get("DISCORD_BOT_TOKEN"):
        return _warn("discord", "no webhook/bot credentials on the web service "
                                "(fine if the bot runs as its own service)")
    return _ok("discord", "credentials present")


@router.get("/api/ops/health")
def ops_health(quiet: int = 0):
    """Everything at once. `quiet=1` returns only what isn't ok — the version
    worth bookmarking, because a healthy system answers with an empty list."""
    checks = [_check_db(), _check_odds_api(), _check_sgo(), _check_ai(),
              _check_discord()]
    checks.extend(_check_model_files())

    order = {"fail": 0, "warn": 1, "ok": 2}
    checks.sort(key=lambda c: order.get(c["status"], 3))
    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    overall = "fail" if fails else ("warn" if warns else "ok")

    up = int(time.time() - _BOOT)
    body = {
        "status": overall,
        "summary": (f"{len(fails)} failing, {len(warns)} degraded, "
                    f"{len(checks) - len(fails) - len(warns)} healthy"),
        "uptime_s": up,
        "uptime_human": f"{up // 3600}h {(up % 3600) // 60}m",
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    body["checks"] = (fails + warns) if quiet else checks
    return body
