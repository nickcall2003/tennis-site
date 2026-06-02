"""
live.py
-------
The heartbeat of the live site. A background loop that, every few seconds:

  1. asks the provider for the current score + stats of each live match
  2. compares to what's stored ("diffing")
  3. if anything changed, writes it to the database
  4. pushes the change to every connected browser over WebSocket

This is the pattern that keeps your costs sane: ONE process talks to the paid
feed on a fixed schedule; thousands of users read from your database and get
pushed updates. Users never trigger a provider call.

In production you'd run this as its own worker process (and swap the in-loop
sleep for the provider's push/WebSocket feed if they offer one). For the demo
it runs as an asyncio task inside the web server.
"""

from __future__ import annotations

import asyncio
import json

from db import SessionLocal
from models import LiveState, Match, StatSnapshot
from base import LiveScore, MatchStats, TennisProvider
from ws import manager

POLL_SECONDS = 1.5          # demo speed; real feeds update every ~5s
STATS_EVERY_N_TICKS = 5     # snapshot stats less often than score


def _score_to_dict(s: LiveScore) -> dict:
    return {
        "sets_a": s.sets_a, "sets_b": s.sets_b,
        "game_a": s.game_a, "game_b": s.game_b,
        "server": s.server, "status": s.status, "winner": s.winner,
    }


def _stats_to_dict(st: MatchStats) -> dict | None:
    if not st.available:
        return None

    def side(p):
        return None if p is None else {
            "aces": p.aces, "double_faults": p.double_faults,
            "first_serve_pct": p.first_serve_pct,
            "first_serve_won_pct": p.first_serve_won_pct,
            "second_serve_won_pct": p.second_serve_won_pct,
            "break_points_won": p.break_points_won,
            "break_points_faced": p.break_points_faced,
            "total_points_won": p.total_points_won,
        }

    return {"player_a": side(st.player_a), "player_b": side(st.player_b)}


class LiveEngine:
    def __init__(self, provider: TennisProvider):
        self.provider = provider
        self._last: dict[int, dict] = {}     # match_id -> last score dict
        self._tick = 0
        self.running = False

    async def run(self) -> None:
        self.running = True
        while self.running:
            try:
                await self._poll_once()
            except Exception as e:  # never let the loop die silently
                print(f"[live] poll error: {e}")
            self._tick += 1
            await asyncio.sleep(POLL_SECONDS)

    async def _poll_once(self) -> None:
        # Figure out which matches are not yet finished.
        with SessionLocal() as db:
            rows = db.query(Match).filter(Match.status != "finished").all()
            active = [(m.id, m.provider_match_id, m.player_a, m.player_b, m.tier) for m in rows]

        for match_id, pid, name_a, name_b, tier in active:
            score = self.provider.get_live_score(pid)
            score_d = _score_to_dict(score)

            if self._last.get(match_id) == score_d:
                continue  # nothing changed; skip the write + broadcast
            self._last[match_id] = score_d

            # Persist score + match status.
            with SessionLocal() as db:
                live = db.query(LiveState).filter_by(match_id=match_id).one_or_none()
                if live is None:
                    live = LiveState(match_id=match_id)
                    db.add(live)
                live.sets_a = ",".join(map(str, score.sets_a))
                live.sets_b = ",".join(map(str, score.sets_b))
                live.game_a, live.game_b = score.game_a, score.game_b
                live.server, live.status, live.winner = score.server, score.status, score.winner

                m = db.get(Match, match_id)
                m.status = score.status
                db.commit()

            payload = {
                "type": "score",
                "match_id": match_id,
                "name_a": name_a, "name_b": name_b,
                "score": score_d,
            }

            # Periodically attach a stats snapshot (or note it's unavailable).
            if self._tick % STATS_EVERY_N_TICKS == 0:
                stats = self.provider.get_match_stats(pid)
                stats_d = _stats_to_dict(stats)
                payload["stats"] = stats_d
                payload["stats_available"] = stats_d is not None
                with SessionLocal() as db:
                    db.add(StatSnapshot(match_id=match_id, payload=json.dumps(stats_d)))
                    db.commit()

            await manager.broadcast(payload)
