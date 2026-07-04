"""
stocks_search.py — symbol search for the Markets view (the Robinhood-style
"search any ticker" box). Part of the removable stocks feature.

Search needs a symbol directory, which yfinance doesn't provide. Two modes:
  * If FINNHUB_API_KEY is set  -> live full-market search via Finnhub (free tier).
  * Otherwise                  -> substring search over a bundled popular list,
                                  plus an "exact ticker" fallback so typing a
                                  valid symbol (e.g. PLTR) still resolves.

Once a symbol is resolved, quotes/charts come from the existing yfinance layer,
which works for ANY valid symbol on demand.
"""
import os

_TD_KEY = os.environ.get("TWELVEDATA_API_KEY", "").strip()
_FINNHUB = os.environ.get("FINNHUB_API_KEY", "").strip()

# Compact popular universe for keyless search (name lookup). Not exhaustive —
# Finnhub covers the full market when a key is present.
POPULAR = [
    ("AAPL", "Apple", "stock"), ("MSFT", "Microsoft", "stock"), ("NVDA", "NVIDIA", "stock"),
    ("AMZN", "Amazon", "stock"), ("GOOGL", "Alphabet (Google)", "stock"), ("META", "Meta Platforms", "stock"),
    ("TSLA", "Tesla", "stock"), ("NFLX", "Netflix", "stock"), ("DIS", "Disney", "stock"),
    ("AMD", "AMD", "stock"), ("INTC", "Intel", "stock"), ("SNAP", "Snap", "stock"),
    ("SBUX", "Starbucks", "stock"), ("JPM", "JPMorgan Chase", "stock"), ("BAC", "Bank of America", "stock"),
    ("COST", "Costco", "stock"), ("WMT", "Walmart", "stock"), ("GPRO", "GoPro", "stock"),
    ("PLTR", "Palantir", "stock"), ("COIN", "Coinbase", "stock"), ("HOOD", "Robinhood", "stock"),
    ("UBER", "Uber", "stock"), ("ABNB", "Airbnb", "stock"), ("SHOP", "Shopify", "stock"),
    ("PYPL", "PayPal", "stock"), ("SOFI", "SoFi", "stock"), ("F", "Ford", "stock"),
    ("GM", "General Motors", "stock"), ("BA", "Boeing", "stock"), ("NKE", "Nike", "stock"),
    ("KO", "Coca-Cola", "stock"), ("PEP", "PepsiCo", "stock"), ("MCD", "McDonald's", "stock"),
    ("GME", "GameStop", "stock"), ("AMC", "AMC Entertainment", "stock"), ("RIVN", "Rivian", "stock"),
    ("LCID", "Lucid", "stock"), ("MARA", "Marathon Digital", "stock"), ("RIOT", "Riot Platforms", "stock"),
    ("SPY", "S&P 500 ETF", "etf"), ("QQQ", "Nasdaq 100 ETF", "etf"), ("VTI", "Total Market ETF", "etf"),
    ("DIA", "Dow Jones ETF", "etf"), ("IWM", "Russell 2000 ETF", "etf"), ("VOO", "Vanguard S&P 500", "etf"),
    ("ARKK", "ARK Innovation ETF", "etf"), ("XLK", "Technology ETF", "etf"), ("XLF", "Financials ETF", "etf"),
    ("XLE", "Energy ETF", "etf"), ("GLD", "Gold ETF", "etf"), ("SLV", "Silver ETF", "etf"),
    ("SCHD", "Schwab Dividend ETF", "etf"), ("VXUS", "Intl Stock ETF", "etf"), ("BND", "Bond ETF", "etf"),
    ("BTC-USD", "Bitcoin", "crypto"), ("ETH-USD", "Ethereum", "crypto"), ("SOL-USD", "Solana", "crypto"),
    ("XRP-USD", "XRP", "crypto"), ("DOGE-USD", "Dogecoin", "crypto"), ("ADA-USD", "Cardano", "crypto"),
    ("AVAX-USD", "Avalanche", "crypto"), ("LINK-USD", "Chainlink", "crypto"), ("MATIC-USD", "Polygon", "crypto"),
    ("LTC-USD", "Litecoin", "crypto"), ("BCH-USD", "Bitcoin Cash", "crypto"), ("SHIB-USD", "Shiba Inu", "crypto"),
]


def _td_search(q):
    try:
        import httpx
        r = httpx.get("https://api.twelvedata.com/symbol_search",
                      params={"symbol": q, "outputsize": 20, "apikey": _TD_KEY}, timeout=8.0)
        r.raise_for_status()
        rows, seen = [], set()
        for it in (r.json().get("data") or []):
            sym = (it.get("symbol") or "").upper()
            typ = (it.get("instrument_type") or "").lower()
            if not sym or sym in seen:
                continue
            # normalise crypto to our -USD convention
            cur = (it.get("currency") or "").upper()
            if "digital" in typ or "crypto" in typ:
                if cur and cur != "USD":
                    continue
                sym2 = sym + "-USD"
                t = "crypto"
            else:
                sym2 = sym
                t = "etf" if "etf" in typ or "fund" in typ else "stock"
            if "." in sym:  # skip most foreign listings
                continue
            seen.add(sym)
            rows.append({"symbol": sym2, "name": it.get("instrument_name", sym), "type": t})
            if len(rows) >= 15:
                break
        return rows
    except Exception:
        return None


def _finnhub_search(q):
    try:
        import httpx
        r = httpx.get("https://finnhub.io/api/v1/search",
                      params={"q": q, "token": _FINNHUB}, timeout=8.0)
        r.raise_for_status()
        rows = []
        for it in (r.json().get("result") or [])[:15]:
            sym = it.get("symbol", "")
            if not sym or "." in sym:  # skip most foreign-exchange suffixes
                continue
            rows.append({"symbol": sym, "name": it.get("description", sym),
                         "type": (it.get("type", "") or "").lower() or "stock"})
        return rows
    except Exception:
        return None


def search(q):
    """Return [{symbol, name, type}] for a query."""
    q = (q or "").strip()
    if not q:
        return []
    if _TD_KEY:
        live = _td_search(q)
        if live:
            return live
    if _FINNHUB:
        live = _finnhub_search(q)
        if live is not None:
            return live
    ql = q.lower()
    hits = [{"symbol": s, "name": n, "type": t} for (s, n, t) in POPULAR
            if ql in s.lower() or ql in n.lower()]
    # exact-ticker fallback: only when nothing matched, so typing an unknown but
    # valid symbol (e.g. NET) still resolves, without polluting name searches.
    if not hits and len(q) <= 6 and q.replace("-", "").isalnum():
        hits = [{"symbol": q.upper(), "name": q.upper(), "type": "stock"}]
    return hits[:15]
