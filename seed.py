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
    venue = VENUES.get(tournament)
    if venue is None:
        return WeatherReport(applicable=False, note="Venue location unknown.")
    lat, lon, indoor = venue
    if indoor:
        return WeatherReport(applicable=False, note="Indoor — weather not a factor.")

    data = _fetch_open_meteo(lat, lon, when)
    if data is None:
        return WeatherReport(applicable=False, note="Weather data unavailable.")
    return parse_open_meteo(data, when)
