"""
markets_routes.py — /api/markets, the per-game line board behind the
"log a bet from a game" picker. Kept off main.py to hold its size down.
"""
import time as _t
import datetime as dt

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import espn_markets

router = APIRouter()

_cache = {}          # (sport, ymd) -> (ts, games)
_TTL = 120


@router.get("/api/markets")
def markets(sport: str, date: str | None = None):
    sport = (sport or "").lower().strip()
    if sport not in espn_markets.LEAGUES:
        return JSONResponse(
            {"error": "unsupported sport", "supported": sorted(espn_markets.LEAGUES)},
            status_code=400)
    ymd = date.replace("-", "") if date else dt.date.today().strftime("%Y%m%d")
    key = (sport, ymd)
    hit = _cache.get(key)
    if hit and _t.time() - hit[0] < _TTL:
        games = hit[1]
    else:
        games = espn_markets.game_markets(sport, ymd)
        _cache[key] = (_t.time(), games)
    # hide finished games; keep upcoming + live ones that still have a line
    games = [g for g in games if g.get("state") != "post"]
    return {"sport": sport, "date": ymd, "count": len(games), "games": games}
