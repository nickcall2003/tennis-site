"""
stocks_data.py — end-of-day price data for the paper-trading engine.

Uses yfinance (add `yfinance` to requirements.txt). Degrades gracefully if the
library or network is unavailable, so it can never crash the app. Part of the
removable stocks feature.
"""
import datetime as dt

_cache = {"t": None, "closes": {}, "last": {}}


def _fresh(hours=6):
    if not _cache["t"]:
        return False
    return (dt.datetime.utcnow() - _cache["t"]).total_seconds() < hours * 3600


def get_history(tickers, lookback_days=200):
    """Return {ticker: [close, close, ...]} oldest->newest for the lookback window.
    Cached ~6h so we don't hammer the source. Best-effort."""
    if _fresh() and all(t in _cache["closes"] for t in tickers):
        return {t: _cache["closes"][t] for t in tickers}
    out = {}
    try:
        import yfinance as yf
        data = yf.download(tickers, period=f"{lookback_days + 40}d", interval="1d",
                           progress=False, group_by="ticker", threads=True)
        for t in tickers:
            try:
                if len(tickers) == 1:
                    series = data["Close"]
                else:
                    series = data[t]["Close"]
                closes = [float(x) for x in series.dropna().tolist()]
                if closes:
                    out[t] = closes[-lookback_days:]
            except Exception:
                continue
    except Exception:
        return {t: _cache["closes"].get(t, []) for t in tickers}
    if out:
        _cache["t"] = dt.datetime.utcnow()
        _cache["closes"].update(out)
        _cache["last"] = {t: c[-1] for t, c in out.items() if c}
    return out


def last_prices(tickers):
    """{ticker: latest close}. Uses cached history when available."""
    hist = get_history(tickers)
    return {t: (c[-1] if c else None) for t, c in hist.items()}


# ---- multi-timeframe quotes for the Markets view ----------------------------
_RANGE = {
    "1D": ("1d", "5m"), "1W": ("5d", "30m"), "1M": ("1mo", "1d"),
    "3M": ("3mo", "1d"), "1Y": ("1y", "1d"), "ALL": ("5y", "1wk"),
}
_qcache = {}  # rng -> (ts, {ticker: quote})


def _downsample(vals, n=48):
    if len(vals) <= n:
        return vals
    step = len(vals) / n
    return [vals[min(len(vals) - 1, int(i * step))] for i in range(n)]


def quotes(tickers, rng="1D"):
    """{ticker: {price, change_pct, series}} for a timeframe. Cached ~3 min.
    For 1D, pre/post-market data is included so after-hours moves show in the line."""
    rng = rng if rng in _RANGE else "1D"
    now = dt.datetime.utcnow()
    cached = _qcache.get(rng)
    if cached and (now - cached[0]).total_seconds() < 180 and all(t in cached[1] for t in tickers):
        return {t: cached[1][t] for t in tickers}
    period, interval = _RANGE[rng]
    out = {}
    try:
        import yfinance as yf
        data = yf.download(tickers, period=period, interval=interval, prepost=(rng == "1D"),
                           progress=False, group_by="ticker", threads=True)
    except Exception:
        return (cached[1] if cached else {})
    for t in tickers:
        try:
            s = (data["Close"] if len(tickers) == 1 else data[t]["Close"]).dropna()
            vals = [float(x) for x in s.tolist()]
            if len(vals) < 2:
                continue
            first, last = vals[0], vals[-1]
            chg = (last - first) / first * 100 if first else 0.0
            dp = 4 if last < 5 else 2
            out[t] = {"price": round(last, dp), "change_pct": round(chg, 2),
                      "series": [round(v, dp) for v in _downsample(vals)]}
        except Exception:
            continue
    if out:
        merged = dict(cached[1]) if cached else {}
        merged.update(out)
        _qcache[rng] = (now, merged)
    return out
