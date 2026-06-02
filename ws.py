"""
seed.py
-------
The "daily job" in miniature. In production a scheduler runs something like
this every morning: pull the schedule, run predictions, de-vig the odds, write
everything to the database. Here it builds the demo day from the mock provider.
"""

from __future__ import annotations

from .analysis import MatchContext, generate_writeup
from .db import SessionLocal, init_db
from .models import Match, MatchAnalysis, Prediction
from .odds import devig_two_way, edge
from .predictions import PredictionEngine
from .providers.mock import MockTennisProvider, demo_schedule
from .weather import get_match_weather

# Optional: plug your LLM here to get natural-language writeups instead of the
# deterministic template. Leave as None to use the template (no API key needed).
#
#   from anthropic import Anthropic
#   _client = Anthropic()
#   def LLM_COMPLETE(prompt: str) -> str:
#       m = _client.messages.create(model="claude-haiku-4-5", max_tokens=200,
#                                   messages=[{"role": "user", "content": prompt}])
#       return m.content[0].text
LLM_COMPLETE = None

# Illustrative American odds for the demo matches, by provider_match_id.
# (player_a_odds, player_b_odds). ITF has no odds market here.
DEMO_ODDS = {
    "mock-0": (-210, 175),    # Alcaraz vs Ruud
    "mock-1": (-250, 205),    # Sinner vs Medvedev
    "mock-2": (-130, 110),    # Swiatek vs Sabalenka
    "mock-3": (-160, 135),    # Mensik vs Svrcina
    "mock-4": None,           # ITF: no market
}


# Demo-only flavour. In production this comes from real surface win-rates.
_CLAY_SPECIALISTS = {"Casper Ruud", "Carlos Alcaraz"}


def _surface_note(info) -> str | None:
    if info.surface == "Clay":
        specialists = sorted(_CLAY_SPECIALISTS & {info.player_a, info.player_b})
        if len(specialists) == 1:
            return f"{specialists[0]} rates as a strong clay-courter"
        if len(specialists) > 1:
            return f"{' and '.join(specialists)} both rate as strong clay-courters"
    return None


def build_today(provider: MockTennisProvider) -> None:
    """Populate the DB with today's matches + predictions, and seed the sims."""
    init_db()

    engine = PredictionEngine()
    engine.preset_demo_ratings()   # in production: engine.train_from_csv("matches.csv")

    schedule = demo_schedule()
    seeded_for_provider = []

    with SessionLocal() as db:
        # Clear any previous demo rows so re-running is clean.
        db.query(MatchAnalysis).delete()
        db.query(Prediction).delete()
        db.query(Match).delete()
        db.commit()

        for info in schedule:
            prob_a = engine.predict(info.player_a, info.player_b, info.surface)

            match = Match(
                provider_match_id=info.provider_match_id,
                tier=info.tier,
                tournament=info.tournament,
                surface=info.surface,
                player_a=info.player_a,
                player_b=info.player_b,
                scheduled=info.scheduled,
                best_of=info.best_of,
                status="scheduled",
            )
            db.add(match)
            db.flush()  # get match.id

            fair_a = edge_a = None
            odds = DEMO_ODDS.get(info.provider_match_id)
            if odds:
                fair_a, _ = devig_two_way(odds[0], odds[1])
                edge_a = edge(prob_a, fair_a)

            db.add(Prediction(match_id=match.id, prob_a=prob_a,
                              fair_prob_a=fair_a, edge_a=edge_a))

            # --- weather + auto-generated writeup -----------------------
            weather = get_match_weather(info.tournament, info.scheduled)
            ctx = MatchContext(
                player_a=info.player_a, player_b=info.player_b,
                tier=info.tier, surface=info.surface,
                prob_a=prob_a, fair_prob_a=fair_a, edge_a=edge_a,
                surface_note=_surface_note(info),
                weather=weather,
            )
            writeup = generate_writeup(ctx, LLM_COMPLETE)
            db.add(MatchAnalysis(
                match_id=match.id, writeup=writeup,
                weather=weather.summary() if weather.applicable else None,
            ))
            seeded_for_provider.append((info, prob_a))

        db.commit()

    provider.seed_matches(seeded_for_provider)


# ======================================================================
# Real-feed builder: pulls a given day's fixtures from any TennisProvider,
# attaches predictions (feed-name matched), and stores results so finished
# matches can be graded immediately. Used for API-Tennis.
# ======================================================================
from .models import LiveState  # noqa: E402


def build_day(provider, engine: PredictionEngine, day) -> int:
    """Build one calendar day from a real provider. Returns matches added."""
    init_db()
    schedule = provider.get_schedule(day)
    added = 0
    with SessionLocal() as db:
        for info in schedule:
            if db.query(Match).filter_by(provider_match_id=info.provider_match_id).first():
                continue  # already stored (idempotent)

            prob_a, confident = engine.predict_feed(info.player_a, info.player_b)

            match = Match(
                provider_match_id=info.provider_match_id, tier=info.tier,
                tournament=info.tournament, surface=info.surface,
                player_a=info.player_a, player_b=info.player_b,
                scheduled=info.scheduled, best_of=info.best_of, status=info.status,
            )
            db.add(match)
            db.flush()
            db.add(Prediction(match_id=match.id, prob_a=prob_a, confident=confident))

            score = provider.get_live_score(info.provider_match_id)
            db.add(LiveState(
                match_id=match.id,
                sets_a=",".join(map(str, score.sets_a)),
                sets_b=",".join(map(str, score.sets_b)),
                game_a=score.game_a, game_b=score.game_b,
                server=score.server, status=score.status, winner=score.winner,
            ))
            match.status = score.status
            added += 1
        db.commit()
    return added
