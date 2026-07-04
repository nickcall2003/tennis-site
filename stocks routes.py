"""
stocks_routes.py — API + Discord alerts + daily scheduler for the paper-trading
stock signals. Part of the removable stocks feature (delete these files + the
include line in main.py to remove it entirely).

Everything is educational / paper-traded. Endpoints and alerts always carry the
disclaimer. Nothing here places a real trade.

Env:
  STOCKS_DISCORD_WEBHOOK_URL  (falls back to DISCORD_WEBHOOK_URL) for alerts
  STOCKS_AUTO=0               to disable the daily scheduler
  STOCKS_RUN_TIME=08:30       Central time to run signals + post the AM alert
  DISCORD_MENTION=@here       ping used on alerts (@here or @everyone)
"""
import os
import datetime as dt

from fastapi import APIRouter

router = APIRouter()

_WEBHOOK = os.environ.get("STOCKS_DISCORD_WEBHOOK_URL", "") or os.environ.get("DISCORD_WEBHOOK_URL", "")
_MENTION = os.environ.get("DISCORD_MENTION", "@here").strip()
_AUTO = os.environ.get("STOCKS_AUTO", "1").strip().lower() not in ("0", "false", "no", "off")
_SITE = os.environ.get("SITE_URL", "https://www.thelinelogic.com")


def _run_time():
    try:
        h, m = os.environ.get("STOCKS_RUN_TIME", "08:30").split(":")
        return int(h), int(m)
    except Exception:
        return (8, 30)


def _compute_all():
    """Fetch data, compute signals for the whole watchlist. Returns (signals, prices)."""
    import stocks_engine as E
    import stocks_data as D
    tickers = E.WATCHLIST + [E.BENCHMARK]
    hist = D.get_history(tickers)
    signals = {}
    for t in E.WATCHLIST:
        signals[t] = E.compute_signal(hist.get(t, []))
    prices = {t: (c[-1] if c else None) for t, c in hist.items()}
    return signals, prices


def _discord(text):
    if not _WEBHOOK:
        return {"ok": False, "error": "no webhook"}
    try:
        import httpx
        content = ((_MENTION + "\n") if _MENTION else "") + text
        r = httpx.post(_WEBHOOK, json={"content": content[:1900],
                                       "allowed_mentions": {"parse": ["everyone"]}}, timeout=15.0)
        return {"ok": r.status_code in (200, 204), "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _alert_text(buys, sells):
    import stocks_engine as E
    lines = ["\U0001F4C8 **Line Logic \u2014 Model Stock Signals**",
             "*" + E.DISCLAIMER + "*", ""]
    if buys:
        lines.append("**\U0001F7E2 BUY signals (paper) at today\u2019s open:**")
        for b in buys:
            lines.append(f"\u2022 **{b['ticker']}** @ ~${b['price']} \u2014 {b.get('reason','')}")
        lines.append("")
    if sells:
        lines.append("**\U0001F534 SELL signals (paper):**")
        for s in sells:
            pnl = s.get("pnl_pct")
            tag = f" ({'+' if (pnl or 0) >= 0 else ''}{pnl}%)" if pnl is not None else ""
            lines.append(f"\u2022 **{s['ticker']}** @ ~${s['price']}{tag} \u2014 {s.get('reason','')}")
        lines.append("")
    if not buys and not sells:
        lines.append("No new model signals today \u2014 holding current paper positions.")
    lines.append(f"Tracker \u2192 {_SITE}")
    return "\n".join(lines)


def run_and_alert(post=True):
    """Daily job: compute signals, update the paper book, and post the morning
    briefing (always posts when triggered so you get a message every trading day,
    even when it's 'hold, no new signals')."""
    import stocks_engine as E
    signals, prices = _compute_all()
    buys, sells = E.run_day(signals, prices)
    result = {"buys": buys, "sells": sells, "priced": sum(1 for v in prices.values() if v)}
    if post:
        result["discord"] = _discord(_alert_text(buys, sells))
    return result


# ---- API --------------------------------------------------------------------
@router.get("/api/stocks/search")
def stocks_search(q: str = ""):
    """Look up any symbol by ticker or company name. Never 500s."""
    try:
        import stocks_search as S
        return {"results": S.search(q)}
    except Exception as e:
        return {"results": [], "error": str(e)[:200]}


@router.get("/api/stocks/quote")
def stocks_quote(symbol: str = "", range: str = "1D"):
    """On-demand quote + chart series for a single symbol (any ticker)."""
    import stocks_data as D
    import stocks_engine as E
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return {"error": "no symbol"}
    q = D.quotes([symbol], range).get(symbol)
    if not q:
        return {"symbol": symbol, "error": "no data", "disclaimer": E.DISCLAIMER}
    return {"symbol": symbol, "name": E.display_name(symbol), "range": range,
            "price": q["price"], "change_pct": q["change_pct"], "series": q["series"],
            "disclaimer": E.DISCLAIMER}


@router.get("/api/stocks/quotes")
def stocks_quotes(range: str = "1D"):
    """Stocks / ETFs / crypto quotes with a sparkline series for the timeframe."""
    import stocks_engine as E
    import stocks_data as D
    groups = [("stocks", E.STOCKS), ("etfs", E.ETFS), ("crypto", E.CRYPTO)]
    allt = E.STOCKS + E.ETFS + E.CRYPTO
    q = D.quotes(allt, range)
    out = {}
    for key, tickers in groups:
        rows = []
        for t in tickers:
            d = q.get(t)
            if not d:
                continue
            rows.append({"ticker": t, "name": E.display_name(t),
                         "price": d["price"], "change_pct": d["change_pct"], "series": d["series"]})
        out[key] = rows
    return {"range": range, "groups": out, "disclaimer": E.DISCLAIMER}


@router.get("/api/stocks/movers")
def stocks_movers(range: str = "1D"):
    """Biggest market-wide gainers/losers. Uses Twelve Data's market_movers when
    available; otherwise falls back to ranking a curated volatile universe."""
    import stocks_engine as E
    import stocks_data as D
    md = D.market_movers()
    if md and (md.get("gainers") or md.get("losers")):
        return {"gainers": md["gainers"][:8], "losers": md["losers"][:8],
                "source": "market", "disclaimer": E.DISCLAIMER}
    # fallback: rank a curated volatile universe (NOT the watchlist grid)
    universe = ["NVDA", "TSLA", "AMD", "META", "NFLX", "COIN", "PLTR", "SNAP",
                "SOFI", "RIVN", "MARA", "GME"]
    q = D.quotes(universe, range)
    rows = [{"ticker": t, "name": E.display_name(t), "price": d["price"],
             "change_pct": d["change_pct"], "series": d["series"]} for t, d in q.items()]
    gainers = sorted([r for r in rows if r["change_pct"] >= 0],
                     key=lambda r: r["change_pct"], reverse=True)[:8]
    losers = sorted([r for r in rows if r["change_pct"] < 0],
                    key=lambda r: r["change_pct"])[:8]
    return {"gainers": gainers, "losers": losers, "source": "curated", "disclaimer": E.DISCLAIMER}


@router.get("/api/stocks/signals")
def stocks_signals():
    import stocks_engine as E
    signals, prices = _compute_all()
    out = []
    for t in E.WATCHLIST:
        s = signals.get(t, {})
        out.append({"ticker": t, **s})
    return {"signals": out, "disclaimer": E.DISCLAIMER}


@router.get("/api/stocks/portfolio")
def stocks_portfolio():
    import stocks_engine as E
    return E.snapshot()  # stored snapshot only — no live fetch, instant


@router.get("/api/stocks/equity")
def stocks_equity():
    import stocks_engine as E
    snap = E.snapshot()
    return {"equity": snap.get("equity", []), "disclaimer": E.DISCLAIMER}


@router.post("/api/stocks/run")
def stocks_run():
    """Manual trigger (also used to smoke-test). Posts alerts if configured."""
    return run_and_alert(post=True)


# ---- daily scheduler --------------------------------------------------------
_last_run = {"day": None}


def _loop():
    import time as _t
    while True:
        try:
            if _AUTO:
                try:
                    from zoneinfo import ZoneInfo
                    now = dt.datetime.now(ZoneInfo("America/Chicago"))
                except Exception:
                    now = dt.datetime.utcnow() - dt.timedelta(hours=5)
                today = now.date().isoformat()
                hh, mm = _run_time()
                # weekdays only, once per day, at/after the run time
                if (_last_run["day"] != today and now.weekday() < 5
                        and (now.hour, now.minute) >= (hh, mm)):
                    _last_run["day"] = today
                    run_and_alert(post=True)
        except Exception:
            pass
        _t.sleep(90)


def _start():
    if not _AUTO:
        return
    try:
        import threading
        threading.Thread(target=_loop, daemon=True).start()
    except Exception:
        pass


_start()
