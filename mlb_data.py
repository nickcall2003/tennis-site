"""
mlb_data.py
-----------
Static reference data for the 30 MLB teams:
  - team_id (MLB Stats API)         -> used for logos and stat lookups
  - abbreviation, display name
  - ballpark latitude/longitude     -> for the weather lookup
  - park_factor (1.00 = neutral)    -> multiplies the run environment
  - dome flag                       -> skip outdoor weather effects

Logos come from MLB's static CDN: https://www.mlbstatic.com/team-logos/{id}.svg
Park factors are approximate, publicly-known multi-year values; good enough for
a v1 and easy to refine later.
"""

from __future__ import annotations

TEAMS = {
    108: dict(abbr="LAA", name="Angels",       lat=33.800, lon=-117.883, park=0.98, dome=False),
    109: dict(abbr="ARI", name="Diamondbacks", lat=33.445, lon=-112.067, park=1.03, dome=True),
    110: dict(abbr="BAL", name="Orioles",      lat=39.284, lon=-76.622,  park=1.00, dome=False),
    111: dict(abbr="BOS", name="Red Sox",      lat=42.346, lon=-71.097,  park=1.05, dome=False),
    112: dict(abbr="CHC", name="Cubs",         lat=41.948, lon=-87.656,  park=1.01, dome=False),
    113: dict(abbr="CIN", name="Reds",         lat=39.097, lon=-84.507,  park=1.06, dome=False),
    114: dict(abbr="CLE", name="Guardians",    lat=41.496, lon=-81.685,  park=0.98, dome=False),
    115: dict(abbr="COL", name="Rockies",      lat=39.756, lon=-104.994, park=1.12, dome=False),
    116: dict(abbr="DET", name="Tigers",       lat=42.339, lon=-83.049,  park=0.97, dome=False),
    117: dict(abbr="HOU", name="Astros",       lat=29.757, lon=-95.355,  park=1.01, dome=True),
    118: dict(abbr="KC",  name="Royals",       lat=39.051, lon=-94.480,  park=1.00, dome=False),
    119: dict(abbr="LAD", name="Dodgers",      lat=34.073, lon=-118.240, park=0.98, dome=False),
    120: dict(abbr="WSH", name="Nationals",    lat=38.873, lon=-77.007,  park=1.00, dome=False),
    121: dict(abbr="NYM", name="Mets",         lat=40.757, lon=-73.846,  park=0.97, dome=False),
    133: dict(abbr="ATH", name="Athletics",    lat=38.580, lon=-121.513, park=0.98, dome=False),
    134: dict(abbr="PIT", name="Pirates",      lat=40.447, lon=-80.006,  park=0.98, dome=False),
    135: dict(abbr="SD",  name="Padres",       lat=32.707, lon=-117.157, park=0.95, dome=False),
    136: dict(abbr="SEA", name="Mariners",     lat=47.591, lon=-122.333, park=0.94, dome=False),
    137: dict(abbr="SF",  name="Giants",       lat=37.778, lon=-122.389, park=0.92, dome=False),
    138: dict(abbr="STL", name="Cardinals",    lat=38.623, lon=-90.193,  park=0.99, dome=False),
    139: dict(abbr="TB",  name="Rays",         lat=27.768, lon=-82.653,  park=0.96, dome=True),
    140: dict(abbr="TEX", name="Rangers",      lat=32.747, lon=-97.083,  park=1.01, dome=True),
    141: dict(abbr="TOR", name="Blue Jays",    lat=43.641, lon=-79.389,  park=1.00, dome=True),
    142: dict(abbr="MIN", name="Twins",        lat=44.982, lon=-93.278,  park=1.00, dome=False),
    143: dict(abbr="PHI", name="Phillies",     lat=39.906, lon=-75.166,  park=1.03, dome=False),
    144: dict(abbr="ATL", name="Braves",       lat=33.891, lon=-84.468,  park=1.00, dome=False),
    145: dict(abbr="CWS", name="White Sox",    lat=41.830, lon=-87.634,  park=1.02, dome=False),
    146: dict(abbr="MIA", name="Marlins",      lat=25.778, lon=-80.220,  park=0.97, dome=True),
    147: dict(abbr="NYY", name="Yankees",      lat=40.829, lon=-73.926,  park=1.02, dome=False),
    158: dict(abbr="MIL", name="Brewers",      lat=43.028, lon=-87.971,  park=1.00, dome=True),
}


def logo_url(team_id) -> str:
    return f"https://www.mlbstatic.com/team-logos/{team_id}.svg"


def team_meta(team_id):
    return TEAMS.get(int(team_id)) if team_id is not None else None


def park_factor(home_team_id) -> float:
    m = team_meta(home_team_id)
    return m["park"] if m else 1.0
