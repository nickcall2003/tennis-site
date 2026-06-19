"""
weather.py
----------
Tournament weather via Open-Meteo (https://open-meteo.com) — free, no API key.

Two calls: geocode the venue city -> lat/lon, then a daily forecast across the
tournament dates. Wind is the golf-relevant driver (high wind => higher scoring,
shuffles the leaderboard), so we surface max wind + gusts alongside temp/precip.

Everything is cached and fails soft: any problem returns {"ready": False, ...}
so the UI can simply hide the segment rather than error.
"""

import time

_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_cache = {}        # key -> (ts, payload)
_geo_cache = {}    # "city|region" -> (ts, geo|None)
_TTL = 1800        # 30 min
_GEO_TTL = 86400   # a venue's coordinates don't move


def _geocode(city, region=None):
    key = f"{(city or '').lower()}|{(region or '').lower()}"
    hit = _geo_cache.get(key)
    if hit and time.time() - hit[0] < _GEO_TTL:
        return hit[1]
    geo = None
    try:
        import httpx
        r = httpx.get(_GEO_URL, params={"name": city, "count": 5, "language": "en",
                                        "format": "json"}, timeout=12.0)
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
        if results:
            geo = results[0]
            if region:                      # prefer a hit in the right state/region
                rl = region.strip().lower()
                for res in results:
                    a1 = (res.get("admin1") or "").lower()
                    if rl == a1 or (len(rl) <= 3 and rl in a1):
                        geo = res
                        break
    except Exception:
        geo = None
    _geo_cache[key] = (time.time(), geo)
    return geo


def _col(daily, name, i):
    arr = daily.get(name) or []
    return arr[i] if i < len(arr) else None


def tournament_weather(city, region=None, start=None, end=None, venue=None):
    if not city:
        return {"ready": False, "reason": "no_location"}
    key = f"{city}|{region}|{start}|{end}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]

    geo = _geocode(city, region)
    if not geo or geo.get("latitude") is None:
        out = {"ready": False, "reason": "geocode_failed", "city": city}
        _cache[key] = (time.time(), out)
        return out

    lat, lon = geo.get("latitude"), geo.get("longitude")
    try:
        import httpx
        params = {
            "latitude": lat, "longitude": lon,
            "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                      "wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant,"
                      "precipitation_probability_max,precipitation_sum"),
            "wind_speed_unit": "mph", "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch", "timezone": "auto", "forecast_days": 16,
        }
        if start:
            params["start_date"] = start
        if end:
            params["end_date"] = end
        r = httpx.get(_FORECAST_URL, params=params, timeout=12.0)
        r.raise_for_status()
        d = (r.json() or {}).get("daily") or {}
        times = d.get("time") or []
        days = []
        for i, date in enumerate(times):
            days.append({
                "date": date,
                "code": _col(d, "weather_code", i),
                "tmax": _col(d, "temperature_2m_max", i),
                "tmin": _col(d, "temperature_2m_min", i),
                "wind": _col(d, "wind_speed_10m_max", i),
                "gust": _col(d, "wind_gusts_10m_max", i),
                "wdir": _col(d, "wind_direction_10m_dominant", i),
                "pop": _col(d, "precipitation_probability_max", i),
                "precip": _col(d, "precipitation_sum", i),
            })
        out = {"ready": True, "city": geo.get("name"), "region": geo.get("admin1"),
               "country": geo.get("country_code"), "venue": venue,
               "lat": lat, "lon": lon, "days": days}
    except Exception as e:
        out = {"ready": False, "reason": "forecast_failed", "error": str(e)}
    _cache[key] = (time.time(), out)
    return out
