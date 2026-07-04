"""
stocks_data.py — market data via Twelve Data (stocks, ETFs, crypto: quotes,
history, and charts). Part of the removable stocks feature.

Why not yfinance: Yahoo blocks datacenter IPs, so yfinance returns nothing on
Railway. Twelve Data is built to serve from servers with an API key.

Set TWELVEDATA_API_KEY in Railway. Free tier = ~8 requests/min, 800/day, so keep
default lists small and lean on search-on-demand. Degrades gracefully (never
crashes the app) if the key is missing or a request fails.
"""
import os
import time
import datetime as dt

_KEY = os.environ.get("TWELVEDATA_API_KEY", "").strip()
_BASE = "https://api.twelvedata.com"

_RANGE = {
    "1D": ("5min", 78), "1W": ("1h", 45), "1M": ("1day", 22),
    "3M": ("1day", 66), "1Y": ("1day", 252), "ALL": ("1week", 260),
}
_hist_cache = {"t": None, "closes": {}}
_qcache = {}  # rng -> (ts, {ticker: quote})


def _api_sym(tk):
    return (tk[:-4] + "/USD") if tk.endswith("-USD") else tk  # BTC-USD -> BTC/USD


def _downsample(vals, n=48):
    if len(vals) <= n:
        return vals
    step = len(vals) / n
    return [vals[min(len(vals) - 1, int(i * step))] for i in range(n)]


def _batches(lst, n=8):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _time_series(tickers, interval, outputsize, pace=1.0):
    """{ticker: [close oldest->newest]} via Twelve Data. Batched (<=8) + paced."""
    if not _KEY or not tickers:
        return {}
    out = {}
    try:
        import httpx
    except Exception:
        return {}
    first = True
    for grp in _batches(tickers, 8):
        if not first:
            time.sleep(pace)
        first = False
        asyms = [_api_sym(t) for t in grp]
        try:
            r = httpx.get(_BASE + "/time_series", params={
                "symbol": ",".join(asyms), "interval": interval, "outputsize": outputsize,
                "apikey": _KEY, "timezone": "America/New_York", "order": "ASC",
            }, timeout=20.0)
            data = r.json()
        except Exception:
            continue
        if len(grp) == 1:
            data = {asyms[0]: data}
        for t, a in zip(grp, asyms):
            d = data.get(a) or data.get(a.upper())
            if not isinstance(d, dict) or d.get("status") == "error":
                continue
            closes = []
            for v in (d.get("values") or []):
                try:
                    closes.append(float(v["close"]))
                except Exception:
                    pass
            if closes:
                out[t] = closes
    return out


def get_history(tickers, lookback_days=200):
    now = dt.datetime.utcnow()
    if _hist_cache["t"] and (now - _hist_cache["t"]).total_seconds() < 6 * 3600 \
            and all(t in _hist_cache["closes"] for t in tickers):
        return {t: _hist_cache["closes"][t] for t in tickers}
    ts = _time_series(tickers, "1day", min(5000, lookback_days + 5), pace=8.0)
    if ts:
        _hist_cache["t"] = now
        _hist_cache["closes"].update(ts)
    return {t: ts.get(t, _hist_cache["closes"].get(t, [])) for t in tickers}


def last_prices(tickers):
    h = get_history(tickers)
    return {t: (c[-1] if c else None) for t, c in h.items()}


def quotes(tickers, rng="1D"):
    rng = rng if rng in _RANGE else "1D"
    now = dt.datetime.utcnow()
    cached = _qcache.get(rng)
    if cached and (now - cached[0]).total_seconds() < 180 and all(t in cached[1] for t in tickers):
        return {t: cached[1][t] for t in tickers}
    interval, outsize = _RANGE[rng]
    ts = _time_series(tickers, interval, outsize)
    out = {}
    for t in tickers:
        vals = ts.get(t, [])
        if len(vals) < 2:
            continue
        first, last = vals[0], vals[-1]
        chg = (last - first) / first * 100 if first else 0.0
        dp = 4 if last < 5 else 2
        out[t] = {"price": round(last, dp), "change_pct": round(chg, 2),
                  "series": [round(v, dp) for v in _downsample(vals)]}
    if out:
        merged = dict(cached[1]) if cached else {}
        merged.update(out)
        _qcache[rng] = (now, merged)
    return out
