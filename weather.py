"""
weather.py
----------
Weather for a match, done the only way that actually works: there is no
"tennis weather API", so we compose one.

  1. VENUES maps a tournament to its venue's lat/long and whether play is
     indoors. You build this table once (geocode each venue) and reuse it.
  2. get_match_weather() calls Open-Meteo for the match's date/time...
  3. ...but ONLY for outdoor venues. Indoor/roofed events return a report
     flagged not-applicable, so the rest of the app never shows pointless
     "wind 12 km/h" on an indoor hard court.

Open-Meteo needs no API key for non-commercial use. For a commercial site you
must use their paid plan or self-host (the code is open-source). The HTTP call
is wrapped so the app degrades gracefully if the network or service is down --
weather is a nice-to-have, never a hard dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Tournament -> (latitude, longitude, indoor?). In production this is a DB
# table you populate once per venue. A few demo entries:
VENUES: dict[str, tuple[float, float, bool]] = {
    "Madrid Open":       (40.4378, -3.6795, False),  # outdoor clay (some roofed courts)
    "Prague Challenger": (50.0755, 14.4378, False),
    "ITF M25 Antalya":   (36.8969, 30.7133, False),
    "Australian Open":   (-37.8214, 144.9785, False),  # has roofs, but main draw mostly open
    "Paris Masters":     (48.8566, 2.3522, True),       # indoor hard
}

# Minimal WMO weather-code -> text. Open-Meteo returns these codes.
_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "fog", 51: "light drizzle", 61: "light rain",
    63: "rain", 65: "heavy rain", 80: "rain showers", 95: "thunderstorm",
}


@dataclass
class WeatherReport:
    applicable: bool                 # False for indoor/roofed venues
    temp_c: float | None = None
    wind_kph: float | None = None
    precip_mm: float | None = None
    precip_prob: int | None = None
    description: str | None = None
    note: str | None = None          # e.g. "indoor — weather not a factor"

    def summary(self) -> str:
        if not self.applicable:
            return self.note or "Indoor — weather not a factor."
        bits = []
        if self.temp_c is not None:
            bits.append(f"{self.temp_c:.0f}°C")
        if self.description:
            bits.append(self.description)
        if self.wind_kph is not None and self.wind_kph >= 20:
            bits.append(f"windy ({self.wind_kph:.0f} km/h)")
        if self.precip_prob is not None and self.precip_prob >= 40:
            bits.append(f"{self.precip_prob}% rain chance")
        return ", ".join(bits) if bits else "conditions look mild"


def _fetch_open_meteo(lat: float, lon: float, when: datetime) -> dict | None:
    """Call Open-Meteo's forecast API for the hour nearest `when`. Returns
    parsed dict or None on any failure. Requires `httpx` and network."""
    try:
        import httpx  # imported lazily so the rest works without it
    except ImportError:
        return None
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m,precipitation,precipitation_probability,"
                  "wind_speed_10m,weather_code",
        "wind_speed_unit": "kmh",
        "start_date": when.date().isoformat(),
        "end_date": when.date().isoformat(),
    }
    try:
        r = httpx.get(url, params=params, timeout=8.0)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def parse_open_meteo(data: dict, when: datetime) -> WeatherReport:
    """Pull the row for the match hour out of an Open-Meteo response."""
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    target = when.strftime("%Y-%m-%dT%H:00")
    idx = times.index(target) if target in times else (when.hour if when.hour < len(times) else 0)

    def at(key):
        arr = hourly.get(key)
        return arr[idx] if arr and idx < len(arr) else None

    code = at("weather_code")
    return WeatherReport(
        applicable=True,
        temp_c=at("temperature_2m"),
        wind_kph=at("wind_speed_10m"),
        precip_mm=at("precipitation"),
        precip_prob=at("precipitation_probability"),
        description=_WMO.get(int(code), "unknown") if code is not None else None,
    )


def get_match_weather(tournament: str, when: datetime) -> WeatherReport:
    """The function the rest of the app calls. Handles indoor gating + failures."""
    venue = resolve_venue(tournament)
    if venue is None:
        return WeatherReport(applicable=False, note="Venue location unknown.")
    lat, lon, indoor = venue
    if indoor:
        return WeatherReport(applicable=False, note="Indoor — weather not a factor.")

    data = _fetch_open_meteo(lat, lon, when)
    if data is None:
        return WeatherReport(applicable=False, note="Weather data unavailable.")
    return parse_open_meteo(data, when)


# Broader city/keyword fallback so common tour stops resolve even without an
# exact VENUES entry. Matched as a substring of the tournament name (lowercased).
CITY_HINTS: dict[str, tuple[float, float, bool]] = {
    "melbourne": (-37.82, 144.98, False), "paris": (48.86, 2.35, False),
    "london": (51.51, -0.13, False), "wimbledon": (51.43, -0.21, False),
    "new york": (40.75, -73.85, False), "us open": (40.75, -73.85, False),
    "madrid": (40.44, -3.68, False), "rome": (41.93, 12.45, False),
    "monte": (43.74, 7.43, False), "indian wells": (33.72, -116.31, False),
    "miami": (25.78, -80.13, False), "cincinnati": (39.10, -84.51, False),
    "toronto": (43.65, -79.38, False), "montreal": (45.50, -73.57, False),
    "barcelona": (41.39, 2.16, False), "dubai": (25.20, 55.27, False),
    "acapulco": (16.86, -99.88, False), "rio": (-22.91, -43.17, False),
    "buenos aires": (-34.60, -58.38, False), "shanghai": (31.23, 121.47, False),
    "beijing": (39.90, 116.40, False), "tokyo": (35.68, 139.69, False),
    "stuttgart": (48.78, 9.18, True), "vienna": (48.21, 16.37, True),
    "basel": (47.56, 7.59, True), "halle": (52.06, 8.36, False),
    "queens": (51.48, -0.21, False), "hamburg": (53.55, 9.99, False),
    "estoril": (38.70, -9.40, False), "munich": (48.14, 11.58, False),
    "geneva": (46.20, 6.14, False), "eastbourne": (50.77, 0.28, False),
    "washington": (38.91, -77.01, False), "winston": (36.10, -80.24, False),
    "adelaide": (-34.93, 138.60, False), "brisbane": (-27.47, 153.03, False),
    "doha": (25.29, 51.53, False), "marseille": (43.30, 5.37, True),
    "rotterdam": (51.92, 4.48, True), "metz": (49.12, 6.18, True),
    "antwerp": (51.22, 4.40, True),
}


def resolve_venue(tournament: str):
    """Find (lat, lon, indoor) for a tournament by exact match then city hint."""
    if tournament in VENUES:
        return VENUES[tournament]
    name = (tournament or "").lower()
    for key, loc in CITY_HINTS.items():
        if key in name:
            return loc
    return None


def play_style_effect(report: "WeatherReport") -> str | None:
    """A grounded sentence on how the conditions tend to affect play."""
    if not report or not report.applicable:
        return None
    bits = []
    t = report.temp_c
    wind = report.wind_kph or 0
    desc = (report.description or "").lower()
    if t is not None:
        if t >= 30:
            bits.append("Hot air is thinner and lively, so the ball flies \u2014 helping big servers and flat hitters.")
        elif t <= 12:
            bits.append("Cold, dense air slows the ball and deadens bounce, favoring patient baseliners and heavy topspin.")
    if "overcast" in desc or "fog" in desc or "rain" in desc:
        bits.append("Damp, heavy air makes the court play slower, rewarding grinders over shotmakers.")
    if wind >= 20:
        bits.append("Strong wind disrupts ball toss and timing \u2014 a leveler that can hurt the cleaner ball-striker.")
    return " ".join(bits) if bits else None
