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
import datetime as dt
import json
import os

from db import SessionLocal
from models import LiveState, Match, StatSnapshot
from base import LiveScore, MatchStats, TennisProvider
from ws import manager

POLL_SECONDS = int(os.environ.get("LIVE_POLL_SECONDS", "20"))  # lower for snappier updates if quota allows
STATS_EVERY_N_TICKS = 5     # snapshot stats less often than score

# Only matches scheduled within this window are polled. This is the key fix:
# the old query polled EVERY non-finished match across every day ever built,
# which on a full tennis slate meant hundreds of blocking provider calls every
# tick. The window keeps it to matches that could plausibly be in play right
# now, and the generous slack means a timezone mismatch can't make us miss a
# live match.
POLL_PAST_HOURS = 24        # a match could have started up to ~a day ago
POLL_FUTURE_HOURS = 3       # slack for timezone differences in stored start times
MAX_POLL_PER_TICK = int(os.environ.get("LIVE_MAX_POLL_PER_TICK", "60"))  # ceiling per tick
# A tennis match cannot credibly still be in play after this many hours. Any
# match still flagged "live" past it is a zombie (the feed never sent a final
# status). Zombies show as "live" forever AND eat the poll budget, so we retire
# them each tick.
ZOMBIE_HOURS = int(os.environ.get("LIVE_ZOMBIE_HOURS", "12"))


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
        # Figure out which matches are plausibly in play right now. We bound the
        # query by scheduled time so we never iterate the entire historical slate
        # (the old `status != "finished"` query was the load/cost problem). We
        # also always include anything already marked "live" so a match that's
        # underway is never dropped because of a timezone quirk.
        now = dt.datetime.utcnow()
        lo = now - dt.timedelta(hours=POLL_PAST_HOURS)
        hi = now + dt.timedelta(hours=POLL_FUTURE_HOURS)
        zlo = now - dt.timedelta(hours=ZOMBIE_HOURS)
        with SessionLocal() as db:
            # Retire zombies first: matches stuck "live" long past any real match
            # length. Unreaped, they show as "live" forever AND consume the
            # per-tick budget below, starving today's actual in-play matches.
            stale = (db.query(Match)
                       .filter(Match.status == "live", Match.scheduled < zlo)
                       .all())
            for m in stale:
                m.status = "finished"
            if stale:
                db.commit()
                print(f"[live] retired {len(stale)} stale 'live' match(es)")

            rows = (db.query(Match)
                      .filter(Match.status != "finished")
                      .filter((Match.status == "live") |
                              ((Match.scheduled >= lo) & (Match.scheduled <= hi)))
                      .all())
            # Prioritise the matches we most need fresh: anything actually "live"
            # first (most-recently-scheduled first), THEN upcoming by how soon it
            # starts. The old code ordered purely by scheduled-ascending, so on a
            # busy slate the cap below chopped off exactly the in-play matches and
            # kept stale/early ones.
            def _prio(m):
                ts = m.scheduled.timestamp() if m.scheduled else 0.0
                return (0, -ts) if m.status == "live" else (1, ts)
            rows.sort(key=_prio)
            active = [(m.id, m.provider_match_id, m.player_a, m.player_b, m.tier) for m in rows]

        # Nothing live to poll -> don't touch the network at all. This keeps the
        # single CPU free to serve page requests when there are no live matches.
        if not active:
            return

        # Safety ceiling: never make more than MAX_POLL_PER_TICK blocking calls
        # in a single tick. Live matches are ordered first (see _prio above), so
        # the cap can only ever drop not-yet-started matches, never in-play ones.
        if len(active) > MAX_POLL_PER_TICK:
            active = active[:MAX_POLL_PER_TICK]

        for match_id, pid, name_a, name_b, tier in active:
            # get_live_score is a BLOCKING network call. Run it in a thread so it
            # never freezes the event loop (which would make the whole site hang).
            try:
                score = await asyncio.to_thread(self.provider.get_live_score, pid)
            except Exception as e:
                print(f"[live] score fetch failed for {match_id}: {e}")
                continue
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
                if m is not None:
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
                try:
                    stats = self.provider.get_match_stats(pid)
                    stats_d = _stats_to_dict(stats)
                    payload["stats"] = stats_d
                    payload["stats_available"] = stats_d is not None
                    with SessionLocal() as db:
                        db.add(StatSnapshot(match_id=match_id, payload=json.dumps(stats_d)))
                        db.commit()
                except Exception as e:
                    print(f"[live] stats fetch failed for {match_id}: {e}")

            await manager.broadcast(payload)
