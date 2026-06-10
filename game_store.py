"""
game_store.py — hybrid-ORM persistence for team-sport game boards.

Write-through layer: the providers stay the source of truth (live ESPN fetch +
in-memory cache); after a board is built we persist it here so game state and
predictions survive in the database for history and as a fallback foundation.

Best-effort by design: every call is wrapped so a DB hiccup can NEVER break the
board (it just logs and returns). Imports are lazy so this module is safe to
import even where the DB stack isn't initialized.
"""
from __future__ import annotations

import json
import datetime as dt


def _gd(game_date):
    return game_date.isoformat() if hasattr(game_date, "isoformat") else str(game_date)


def save_games(sport, game_date, games):
    """Upsert each game in a board into game_cache. Returns rows written."""
    if not games:
        return 0
    gd = _gd(game_date)
    n = 0
    try:
        from db import SessionLocal
        from models import GameCache
        with SessionLocal() as db:
            for g in games:
                ref = str(g.get("id") or "").strip()
                if not ref:
                    continue
                payload = json.dumps(g, default=str)
                now = dt.datetime.utcnow()
                row = db.query(GameCache).filter_by(sport=sport, ref=ref).first()
                if row is None:
                    db.add(GameCache(sport=sport, ref=ref, game_date=gd,
                                     status=g.get("status", "scheduled"),
                                     payload=payload, updated_at=now))
                else:
                    row.game_date = gd
                    row.status = g.get("status", "scheduled")
                    row.payload = payload
                    row.updated_at = now
                n += 1
            db.commit()
    except Exception as e:
        print(f"[game_store] save {sport} {gd} skipped: {e}")
    return n


def load_games(sport, game_date):
    """Return the persisted board (list of game dicts) for a (sport, date), or []."""
    gd = _gd(game_date)
    try:
        from db import SessionLocal
        from models import GameCache
        with SessionLocal() as db:
            rows = (db.query(GameCache)
                      .filter_by(sport=sport, game_date=gd)
                      .order_by(GameCache.ref).all())
            return [json.loads(r.payload) for r in rows]
    except Exception as e:
        print(f"[game_store] load {sport} {gd} skipped: {e}")
        return []
