"""
analysis.py
-----------
Builds the written match preview shown on the detail page.

DESIGN: data-backed core + optional AI color.
  - Every factual claim (the edge, surface fit, form, H2H, weather) comes from
    structured facts computed in code. The template turns those into solid,
    multi-paragraph prose with NO invented numbers.
  - If an AI `complete` function is provided (e.g. Claude via the Anthropic
    API), it rewrites those same facts into richer narrative under a strict
    "use ONLY these facts" instruction, and we fall back to the template if it
    fails. The AI adds wording, never new data.
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
    facts: dict = field(default_factory=dict)      # from PredictionEngine.analysis_facts
    weather: str | None = None         # "72F, breezy, 18mph wind"
    weather_effect: str | None = None  # how conditions tilt play

    def fact_bundle(self):
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, [], "", {})}


def _favorite(ctx):
    if ctx.prob_a >= 0.5:
        return ctx.player_a, ctx.player_b, ctx.prob_a
    return ctx.player_b, ctx.player_a, 1 - ctx.prob_a


def _form_trend(recent):
    """Turn ['W vs X','L vs Y',...] into a short momentum phrase."""
    if not recent:
        return None
    seq = [r[0] for r in recent[:5]]
    wins = seq.count("W")
    if wins >= 4:
        return "in strong form"
    if wins <= 1:
        return "scuffling lately"
    if seq[:2] == ["W", "W"]:
        return "trending up"
    if seq[:2] == ["L", "L"]:
        return "trending down"
    return "up and down recently"


def generate_writeup_template(ctx: MatchContext) -> str:
    fav, dog, favp = _favorite(ctx)
    f = ctx.facts or {}
    surf = ctx.surface.lower() if ctx.surface and ctx.surface != "Unknown" else None
    paras = []

    # --- paragraph 1: the pick and how big the edge is ---
    edge = f.get("edge_size")
    lead = f"The model makes {fav} the pick at {favp:.0%}"
    if surf:
        lead += f" on {surf}"
    lead += "."
    if edge == "decisive":
        lead += (f" This is a decisive rating gap (about {f.get('rating_gap')} Elo points) "
                 f"\u2014 {fav} is a clear class above {dog} on paper.")
    elif edge == "clear":
        lead += (f" The rating gap (~{f.get('rating_gap')} points) gives {fav} a clear, "
                 f"if not overwhelming, advantage.")
    elif edge == "slight":
        lead += (f" The edge is slight (~{f.get('rating_gap')} points), so this is closer "
                 f"to a coin flip than the percentage suggests.")
    elif edge == "negligible":
        lead += " The two are nearly even on rating; treat this as a near coin-flip."
    paras.append(lead)

    # --- paragraph 2: surface read ---
    if f.get("surface_note"):
        sp = f.get("surface_prob_a")
        msg = f.get("surface_note") + "."
        if f.get("surface_aligned") is False:
            msg += (f" Notably, the surface math actually leans the other way from the "
                    f"overall pick \u2014 a reason this matchup is trickier than the headline number.")
        paras.append(msg.capitalize() if msg[0].islower() else msg)
    elif surf:
        paras.append(f"On {surf}, the surface-specific ratings track the overall picture, "
                     f"so {fav}'s edge holds up rather than being a product of one fast or slow court.")

    # --- paragraph 3: form + H2H ---
    bits = []
    fav_recent = ctx.recent_a if fav == ctx.player_a else ctx.recent_b
    dog_recent = ctx.recent_b if fav == ctx.player_a else ctx.recent_a
    ft = _form_trend(fav_recent)
    dt_ = _form_trend(dog_recent)
    if ft:
        bits.append(f"{fav} comes in {ft}")
    if dt_:
        bits.append(f"{dog} is {dt_}")
    if bits:
        paras.append("Form: " + "; ".join(bits) + ".")
    if ctx.h2h:
        paras.append(f"Head-to-head, {ctx.h2h} \u2014 "
                     + ("history backs the pick." if fav.split()[-1].lower() in ctx.h2h.lower()
                        else "which cuts against the model and is worth weighing."))

    # --- paragraph 4: weather ---
    if ctx.weather:
        w = f"Conditions: {ctx.weather}."
        if ctx.weather_effect:
            w += " " + ctx.weather_effect
        paras.append(w)

    # --- closing caveat on confidence ---
    if ctx.confidence == "low":
        paras.append("Confidence is low: one player isn't well covered by the rating data, "
                     "so this is a rough estimate, not a strong read.")
    elif ctx.confidence == "medium":
        paras.append("Confidence is moderate \u2014 the rating leans partly on current rankings "
                     "rather than a full match history.")

    return "\n\n".join(paras)


_LLM_PROMPT = """You are a sharp tennis analyst writing a match preview for a predictions site.
Write 2-3 tight paragraphs explaining WHY the model favors the pick. Cover, in your own words:
the size and source of the edge, how the surface affects this specific matchup, recent form and
momentum, the head-to-head, and any weather effect on play style.

STRICT RULES:
- Use ONLY the facts in the JSON below. Do NOT invent any statistic, score, ranking, or result.
- If a fact isn't present, don't claim it. No fabricated serve %, no made-up injuries.
- Natural prose, no markdown, no bullet points. Confident but honest about uncertainty.

FACTS:
{facts}
"""


def generate_writeup(ctx: MatchContext, complete=None) -> str:
    if complete is None:
        return generate_writeup_template(ctx)
    try:
        text = complete(_LLM_PROMPT.format(facts=json.dumps(ctx.fact_bundle(), indent=2))).strip()
        return text or generate_writeup_template(ctx)
    except Exception:
        return generate_writeup_template(ctx)
