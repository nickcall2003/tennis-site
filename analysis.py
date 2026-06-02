"""
analysis.py
-----------
Builds the written match preview shown on the detail page.

Reliable-by-design: the facts (probability, form, H2H, surface, ranking) are
computed in code; the words only ever restate those facts. The template
backend needs no API key and can't invent numbers. To get more natural prose,
pass a `complete` function that calls an LLM (an Anthropic example is in the
seed/main comments); the same facts go in with a strict "use only these facts"
prompt, and it falls back to the template if the call fails.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass
class MatchContext:
    player_a: str
    player_b: str
    tier: str
    surface: str = "Unknown"
    prob_a: float = 0.5
    confidence: str = "low"            # high | medium | low
    form_a: str | None = None          # "Won 7 of last 10"
    form_b: str | None = None
    h2h: str | None = None             # "Alcaraz leads 4-2"
    recent_a: list = field(default_factory=list)   # ["W vs X", "L vs Y", ...]
    recent_b: list = field(default_factory=list)

    def facts(self):
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, [], "")}


def generate_writeup_template(ctx: MatchContext) -> str:
    fav, fav_prob = (ctx.player_a, ctx.prob_a) if ctx.prob_a >= 0.5 else (ctx.player_b, 1 - ctx.prob_a)
    surf = ctx.surface.lower() if ctx.surface and ctx.surface != "Unknown" else None

    s = []
    lead = f"The model makes {fav} the favorite at {fav_prob:.0%}"
    if surf:
        lead += f" on {surf}"
    s.append(lead + ".")

    if ctx.confidence == "low":
        s.append("Confidence is low — one player isn't well covered by the rating "
                 "data yet, so treat this as a rough estimate.")
    elif ctx.confidence == "medium":
        s.append("Confidence is moderate; the rating leans partly on current "
                 "rankings rather than full match history.")

    if ctx.h2h:
        s.append(f"Head-to-head: {ctx.h2h}.")
    forms = []
    if ctx.form_a:
        forms.append(f"{ctx.player_a} {ctx.form_a.lower()}")
    if ctx.form_b:
        forms.append(f"{ctx.player_b} {ctx.form_b.lower()}")
    if forms:
        s.append("Recent form — " + "; ".join(forms) + ".")
    return " ".join(s)


_LLM_PROMPT = """You are a tennis analyst writing a short match preview.
Use ONLY the facts in this JSON. Do not invent any statistic, score, player, or
result not present. Write 3-4 natural sentences covering the pick, the surface
if given, head-to-head, and recent form. Plain text, no markdown.

FACTS:
{facts}
"""


def generate_writeup(ctx: MatchContext, complete=None) -> str:
    if complete is None:
        return generate_writeup_template(ctx)
    try:
        text = complete(_LLM_PROMPT.format(facts=json.dumps(ctx.facts(), indent=2))).strip()
        return text or generate_writeup_template(ctx)
    except Exception:
        return generate_writeup_template(ctx)
