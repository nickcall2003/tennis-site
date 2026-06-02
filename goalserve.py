"""
providers/goalserve.py
----------------------
A STUB showing how a real provider adapter is wired. It does not run without a
real API key and network access -- it exists to show that switching from the
mock to a paid feed means implementing these same three methods and mapping
the feed's fields onto our neutral dataclasses.

Goalserve returns tennis data as XML/JSON with fields like:
    <player name="..." serve="True" sets_won="2" set1="6" set2="3" game_score="40" .../>
You'd parse that and fill in LiveScore / MatchStats below. Other providers
(Matchstat, Data Sports Group, etc.) differ only in field names and transport;
the contract you expose to the rest of the app stays identical.

Replace MockTennisProvider with GoalserveProvider in app/main.py once you have
credentials, and nothing else in the codebase needs to change.
"""

from __future__ import annotations

import os
from datetime import datetime

from base import LiveScore, MatchInfo, MatchStats, TennisProvider


class GoalserveProvider(TennisProvider):
    name = "goalserve"

    BASE_URL = "https://www.goalserve.com/getfeed"  # example; see your docs

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("GOALSERVE_API_KEY")
        if not self.api_key:
            raise RuntimeError("Set GOALSERVE_API_KEY to use the live feed.")
        # import requests/httpx here in the real implementation

    def get_schedule(self, day: datetime) -> list[MatchInfo]:
        # 1. GET the day's tennis schedule feed for your covered tiers.
        # 2. For each match element, build a MatchInfo(...).
        # 3. Map provider tier labels -> your TIERS ("ATP"/"WTA"/...).
        raise NotImplementedError("Wire up the real Goalserve schedule feed here.")

    def get_live_score(self, provider_match_id: str) -> LiveScore:
        # 1. GET the live feed for this match id.
        # 2. Read sets_won / set1..set5 / game_score / serve flags.
        # 3. Return LiveScore(sets_a=..., game_a=..., server=..., status=...).
        raise NotImplementedError("Map the live score feed onto LiveScore here.")

    def get_match_stats(self, provider_match_id: str) -> MatchStats:
        # Map serve/return stat fields onto PlayerStats. Remember many ITF
        # matches will have none -- return MatchStats() (empty) in that case.
        raise NotImplementedError("Map the stats feed onto MatchStats here.")
