"""
stocks_engine.py — rules-based signal + paper-trading engine for Line Logic.

SELF-CONTAINED and REMOVABLE: this file (plus stocks_data.py, stocks_routes.py,
ll-stocks.js) is the entire stocks feature. Delete them + the include line in
main.py and the sports app is untouched.

Everything here is EDUCATIONAL and PAPER-TRADED — no real money, no brokerage.
Signals are "what a transparent rules model did," benchmarked against simply
holding SPY, so the tracked record tells the truth. NOT financial advice.

State (positions, trades, equity curve) persists as JSON on the Railway volume
so there are no database schema changes to add or remove.
"""
import os
import json
import datetime as dt

STATE_PATH = os.environ.get("STOCKS_STATE_PATH", "/data/stocks_state.json")

# Educational watchlist (equal-weight paper portfolio). SPY is the benchmark.
WATCHLIST = [t.strip().upper() for t in os.environ.get(
    "STOCKS_WATCHLIST",
    "AAPL,NVDA,AMD,SOFI,PLTR,RIOT,IONQ,RKLB,HIMS,DKNG,ACHR,AFRM"
).split(",") if t.strip()]
BENCHMARK = "SPY"
START_CASH = float(os.environ.get("STOCKS_START_CASH", "100000"))
SLICE = float(os.environ.get("STOCKS_SLICE", "0.9")) / max(1, len(WATCHLIST))  # frac of start cash per name

DISCLAIMER = ("Educational, paper-traded model signals \u2014 not financial advice. "
              "No positions are real. Consult a licensed advisor before investing.")


# ---- indicators -------------------------------------------------------------
def _sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def _rsi(vals, n=14):
    if len(vals) < n + 1:
        return None
    gains, losses = [], []
    for i in range(-n, 0):
        ch = vals[i] - vals[i - 1]
        (gains if ch >= 0 else losses).append(abs(ch))
    ag = sum(gains) / n if gains else 0.0
    al = sum(losses) / n if losses else 0.0
    if al == 0:
        return 100.0
    rs = ag / al
    return round(100 - (100 / (1 + rs)), 1)


def compute_signal(closes):
    """Given a list of daily closes (oldest->newest), return the current signal.
    Transparent rule: 20/50-day SMA crossover, gated by RSI. Explainable in one line."""
    if not closes or len(closes) < 51:
        return {"signal": "hold", "reason": "not enough history", "price": (closes[-1] if closes else None)}
    price = closes[-1]
    s20, s50 = _sma(closes, 20), _sma(closes, 50)
    p20, p50 = _sma(closes[:-1], 20), _sma(closes[:-1], 50)
    rsi = _rsi(closes)
    crossed_up = p20 is not None and p50 is not None and p20 <= p50 and s20 > s50
    crossed_dn = p20 is not None and p50 is not None and p20 >= p50 and s20 < s50
    sig, reason = "hold", "trend intact"
    if crossed_up and (rsi is None or rsi < 75):
        sig, reason = "buy", "20-day SMA crossed above 50-day (uptrend forming)"
    elif crossed_dn:
        sig, reason = "sell", "20-day SMA crossed below 50-day (uptrend broke)"
    elif rsi is not None and rsi >= 80:
        sig, reason = "sell", f"overbought (RSI {rsi})"
    return {"signal": sig, "reason": reason, "price": round(price, 2),
            "sma20": round(s20, 2) if s20 else None, "sma50": round(s50, 2) if s50 else None, "rsi": rsi,
            "trend": "up" if (s20 and s50 and s20 >= s50) else "down"}


# ---- paper book (JSON-persisted) --------------------------------------------
def _load():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"cash": START_CASH, "positions": {}, "trades": [],
                "equity": [], "spy_shares": None, "started": None}


def _save(st):
    try:
        os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(st, f)
    except Exception:
        pass


def portfolio_value(st, prices):
    v = st.get("cash", 0.0)
    for tk, pos in st.get("positions", {}).items():
        px = prices.get(tk)
        if px:
            v += pos["shares"] * px
    return round(v, 2)


def run_day(signals, prices, today=None):
    """Apply today's signals to the paper book and snapshot equity vs SPY.
    `signals` = {ticker: signal_dict}; `prices` = {ticker: close, 'SPY': close}.
    Returns (new_buys, new_sells) for alerting."""
    st = _load()
    today = today or dt.date.today().isoformat()
    if not st.get("started"):
        st["started"] = today
    # initialise the SPY benchmark with the same starting cash
    if st.get("spy_shares") is None and prices.get(BENCHMARK):
        st["spy_shares"] = START_CASH / prices[BENCHMARK]

    buys, sells = [], []
    for tk in WATCHLIST:
        sig = (signals.get(tk) or {}).get("signal")
        px = prices.get(tk)
        if not px:
            continue
        holding = tk in st["positions"]
        if sig == "buy" and not holding:
            spend = START_CASH * SLICE
            if st["cash"] >= spend and spend > 0:
                sh = spend / px
                st["cash"] -= spend
                st["positions"][tk] = {"shares": sh, "entry": px, "entry_date": today}
                st["trades"].append({"ticker": tk, "action": "buy", "price": px, "date": today})
                buys.append({"ticker": tk, "price": px, "reason": signals[tk].get("reason")})
        elif sig == "sell" and holding:
            pos = st["positions"].pop(tk)
            proceeds = pos["shares"] * px
            st["cash"] += proceeds
            pnl = (px - pos["entry"]) / pos["entry"] * 100
            st["trades"].append({"ticker": tk, "action": "sell", "price": px, "date": today,
                                 "pnl_pct": round(pnl, 2), "entry": pos["entry"]})
            sells.append({"ticker": tk, "price": px, "pnl_pct": round(pnl, 2),
                          "reason": signals[tk].get("reason")})

    val = portfolio_value(st, prices)
    spy_val = round((st.get("spy_shares") or 0) * (prices.get(BENCHMARK) or 0), 2)
    # one equity point per day (replace if same day re-runs)
    st["equity"] = [e for e in st.get("equity", []) if e.get("date") != today]
    st["equity"].append({"date": today, "value": val, "spy": spy_val})
    st["equity"] = st["equity"][-400:]
    _save(st)
    return buys, sells


def snapshot(prices=None):
    """Current paper-book summary for the API/UI."""
    st = _load()
    eq = st.get("equity", [])
    last = eq[-1] if eq else None
    start_v = START_CASH
    cur = last["value"] if last else start_v
    spy = last["spy"] if last else start_v
    ret = (cur / start_v - 1) * 100 if start_v else 0
    spy_ret = (spy / start_v - 1) * 100 if start_v and spy else 0
    holds = [{"ticker": tk, **pos} for tk, pos in st.get("positions", {}).items()]
    closed = [t for t in st.get("trades", []) if t.get("action") == "sell"]
    wins = sum(1 for t in closed if (t.get("pnl_pct") or 0) > 0)
    return {
        "started": st.get("started"), "value": round(cur, 2), "spy_value": round(spy, 2),
        "return_pct": round(ret, 2), "spy_return_pct": round(spy_ret, 2),
        "vs_spy_pct": round(ret - spy_ret, 2),
        "cash": round(st.get("cash", 0), 2), "positions": holds,
        "closed_trades": len(closed), "win_rate": round(100 * wins / len(closed), 1) if closed else None,
        "equity": eq, "disclaimer": DISCLAIMER,
    }


# ---- display universe (quotes only; paper engine still trades WATCHLIST) -----
STOCKS = [t.strip().upper() for t in os.environ.get(
    "STOCKS_STOCKS", "AAPL,MSFT,NVDA"
).split(",") if t.strip()]
ETFS = [t.strip().upper() for t in os.environ.get(
    "STOCKS_ETFS", "SPY,QQQ"
).split(",") if t.strip()]
CRYPTO = [t.strip().upper() for t in os.environ.get(
    "STOCKS_CRYPTO", "BTC-USD"
).split(",") if t.strip()]

_NAMES = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA", "AMZN": "Amazon",
    "GOOGL": "Alphabet", "META": "Meta Platforms", "TSLA": "Tesla", "NFLX": "Netflix",
    "DIS": "Disney", "AMD": "AMD", "SNAP": "Snap", "SBUX": "Starbucks", "JPM": "JPMorgan",
    "COST": "Costco", "GPRO": "GoPro", "SPY": "S&P 500 ETF", "QQQ": "Nasdaq 100 ETF",
    "VTI": "Total Market ETF", "DIA": "Dow Jones ETF", "IWM": "Russell 2000 ETF",
    "ARKK": "ARK Innovation", "XLK": "Technology ETF", "XLF": "Financials ETF",
    "GLD": "Gold ETF", "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "SOL-USD": "Solana",
    "XRP-USD": "XRP", "DOGE-USD": "Dogecoin", "ADA-USD": "Cardano", "SOFI": "SoFi", "PLTR": "Palantir", "RIOT": "Riot Platforms",
    "IONQ": "IonQ", "RKLB": "Rocket Lab", "HIMS": "Hims & Hers", "DKNG": "DraftKings",
    "ACHR": "Archer Aviation", "AFRM": "Affirm", "COIN": "Coinbase", "HOOD": "Robinhood",
}


def display_name(tk):
    return _NAMES.get(tk, tk.replace("-USD", ""))


def hot_pick_score(closes, days=10):
    """Score a 'steady climber': a stock rising consistently, day by day, without a
    wild spike. Rewards a positive multi-day gain weighted by how many of those days
    were up. Returns a dict or None (not rising)."""
    if not closes or len(closes) < days + 1:
        return None
    recent = closes[-(days + 1):]
    first, last = recent[0], recent[-1]
    if first <= 0:
        return None
    pct = (last - first) / first * 100.0
    if pct <= 0:
        return None
    up = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])
    n = len(recent) - 1
    consistency = up / n if n else 0
    # steady climb = decent gain AND most days up; penalise if it's one big spike
    daily = [(recent[i] - recent[i - 1]) / recent[i - 1] for i in range(1, len(recent))]
    biggest = max(daily) if daily else 0
    spike_pen = 0.6 if (biggest > 0 and pct > 0 and biggest > pct / 100 * 0.6) else 1.0
    score = pct * consistency * spike_pen
    return {"pct": round(pct, 2), "up_days": up, "days": n, "consistency": round(consistency, 2),
            "price": round(last, 2), "score": round(score, 3)}
