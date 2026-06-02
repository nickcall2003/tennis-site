"""
analysis.py
-----------
Auto-generated match writeups. This is the "AI response for each match" piece.

The trick to making LLM writeups reliable (not hallucinated nonsense) is to do
the thinking in code and let the model only do the *wording*:

  1. assemble_context() gathers FACTS into a MatchContext: model probability,
     value edge, surface, recent form, head-to-head, weather (if outdoor).
  2. generate_writeup() turns those facts into 2-3 sentences.

Two backends, same interface:
  - the TEMPLATE backend is deterministic, needs no API key, and can NEVER
    invent a number because it only formats the facts it's given. Great default.
  - the LLM backend sends the SAME facts to a model with a strict prompt
    ("use only these facts, no new numbers") for more natural prose. You pass
    in a `complete` function so this stays provider-agnostic (Anthropic,
    OpenAI, local model -- analysis.py doesn't care).

So "how hard is it to automate AI writeups?" -> the template version works
today; swapping in a real model is one function you provide.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Callable

from weather import WeatherReport


@dataclass
class MatchContext:
    player_a: str
    player_b: str
    tier: str
    surface: str
    prob_a: float                         # model P(player_a wins)
    fair_prob_a: float | None = None      # de-vigged book prob
    edge_a: float | None = None           # model - fair
    form_a: str | None = None             # e.g. "8-2 in last 10"
    form_b: str | None = None
    surface_note: str | None = None       # e.g. "Ruud is a strong clay-courter"
    h2h: str | None = None                # e.g. "Alcaraz leads H2H 4-2"
    weather: WeatherReport | None = None

    def facts_dict(self) -> dict:
        d = asdict(self)
        if self.weather is not None:
            d["weather"] = self.weather.summary()
        return {k: v for k, v in d.items() if v is not None}


def generate_writeup_template(ctx: MatchContext) -> str:
    """Deterministic writeup. Only uses facts present in the context."""
    fav, dog = (ctx.player_a, ctx.player_b) if ctx.prob_a >= 0.5 else (ctx.player_b, ctx.player_a)
    fav_prob = ctx.prob_a if ctx.prob_a >= 0.5 else 1 - ctx.prob_a

    s = [f"The model favors {fav} at {fav_prob:.0%} on {ctx.surface.lower()} "
         f"({ctx.tier})."]

    if ctx.h2h:
        s.append(f"{ctx.h2h}.")
    forms = []
    if ctx.form_a:
        forms.append(f"{ctx.player_a} is {ctx.form_a}")
    if ctx.form_b:
        forms.append(f"{ctx.player_b} is {ctx.form_b}")
    if forms:
        s.append("Recent form: " + "; ".join(forms) + ".")
    if ctx.surface_note:
        s.append(ctx.surface_note + ".")
    if ctx.weather is not None and ctx.weather.applicable:
        s.append(f"Conditions: {ctx.weather.summary()}.")

    if ctx.edge_a is not None and abs(ctx.edge_a) >= 0.02:
        side = ctx.player_a if ctx.edge_a > 0 else ctx.player_b
        s.append(f"Versus the fair line, the model sees value on {side} "
                 f"(edge {abs(ctx.edge_a):.1%}).")
    return " ".join(s)


# Strict prompt template for the LLM backend. The model gets ONLY these facts.
_LLM_PROMPT = """You are a tennis analyst writing a short match preview.
Use ONLY the facts in this JSON. Do not invent any statistic, score, or fact
not present here. Write 2-3 natural sentences. End with the value angle if one
is present. Plain text, no markdown.

FACTS:
{facts}
"""


def generate_writeup_llm(ctx: MatchContext, complete: Callable[[str], str] | None) -> str:
    """
    LLM-backed writeup. `complete` is a function you supply that takes a prompt
    string and returns the model's text. If it's None, falls back to template.

    Example `complete` using the Anthropic API:

        from anthropic import Anthropic
        client = Anthropic()
        def complete(prompt: str) -> str:
            msg = client.messages.create(
                model="claude-haiku-4-5",     # cheap + fast for short writeups
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content[0].text

    A 2-3 sentence writeup costs a fraction of a cent with a small model, so
    generating one per match in your daily job is negligible.
    """
    if complete is None:
        return generate_writeup_template(ctx)
    prompt = _LLM_PROMPT.format(facts=json.dumps(ctx.facts_dict(), indent=2))
    try:
        text = complete(prompt).strip()
        return text or generate_writeup_template(ctx)
    except Exception:
        return generate_writeup_template(ctx)   # never break the daily job


def generate_writeup(ctx: MatchContext, complete: Callable[[str], str] | None = None) -> str:
    """Single entry point. Pass a `complete` fn for LLM prose, or omit for template."""
    return generate_writeup_llm(ctx, complete)
