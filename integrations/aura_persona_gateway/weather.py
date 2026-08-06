from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

from .city_names import normalize_city_name
from .runtime import AuraRuntimeConfig, cached_weather_snapshot, save_aura_runtime_config


OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_QUERY_WEATHER_CACHE: dict[str, dict[str, Any]] = {}

CITY_COORDS: dict[str, tuple[float, float, str]] = {
    "北京": (39.9042, 116.4074, "北京市"),
    "上海": (31.2304, 121.4737, "上海市"),
    "广州": (23.1291, 113.2644, "广东"),
    "深圳": (22.5431, 114.0579, "广东"),
    "杭州": (30.2741, 120.1551, "浙江"),
    "南京": (32.0603, 118.7969, "江苏"),
    "成都": (30.5728, 104.0668, "四川"),
    "重庆": (29.5630, 106.5516, "重庆市"),
    "武汉": (30.5928, 114.3055, "湖北"),
    "西安": (34.3416, 108.9398, "陕西"),
    "苏州": (31.2989, 120.5853, "江苏"),
    "天津": (39.3434, 117.3616, "天津市"),
    "青岛": (36.0671, 120.3826, "山东"),
    "厦门": (24.4798, 118.0894, "福建"),
}


def weather_snapshot_for_query(
    config: AuraRuntimeConfig,
    *,
    city: str,
    latitude: str = "",
    longitude: str = "",
    force: bool = False,
) -> dict[str, Any]:
    _updated, snapshot = _weather_snapshot_for_query(config, city=city, latitude=latitude, longitude=longitude, force=force)
    return snapshot


def cached_user_weather_snapshot(
    config: AuraRuntimeConfig,
    *,
    city: str,
    latitude: str = "",
    longitude: str = "",
) -> dict[str, Any]:
    city = normalize_city_name(city)
    latitude = str(latitude or "").strip()
    longitude = str(longitude or "").strip()
    if not config.weather_auto_refresh_enabled:
        return {}
    cache_key = _query_cache_key(config, city=city, latitude=latitude, longitude=longitude)
    return _cached_query_weather(config, cache_key) or _persisted_query_weather(config, cache_key) or {}


def refresh_user_weather_if_needed(
    config: AuraRuntimeConfig,
    *,
    city: str,
    latitude: str = "",
    longitude: str = "",
    force: bool = False,
) -> tuple[AuraRuntimeConfig, dict[str, Any]]:
    return _weather_snapshot_for_query(config, city=city, latitude=latitude, longitude=longitude, force=force, persist=True)


def _weather_snapshot_for_query(
    config: AuraRuntimeConfig,
    *,
    city: str,
    latitude: str = "",
    longitude: str = "",
    force: bool = False,
    persist: bool = False,
) -> tuple[AuraRuntimeConfig, dict[str, Any]]:
    city = normalize_city_name(city)
    latitude = str(latitude or "").strip()
    longitude = str(longitude or "").strip()
    if not config.weather_auto_refresh_enabled:
        return config, _disabled_snapshot(city=city)
    cache_key = _query_cache_key(config, city=city, latitude=latitude, longitude=longitude)
    current = _cached_query_weather(config, cache_key) or _persisted_query_weather(config, cache_key)
    if current and not force and not _needs_query_refresh(config, current):
        _QUERY_WEATHER_CACHE[cache_key] = current
        return config, current
    result = fetch_current_weather(
        city=city,
        provider=config.weather_provider,
        latitude=latitude,
        longitude=longitude,
        timeout_seconds=float(config.weather_request_timeout_seconds or 8),
    )
    if result.get("ok"):
        weather = _query_weather_with_cache_metadata(config, result["weather"])
        weather["key"] = cache_key
        _QUERY_WEATHER_CACHE[cache_key] = weather
        if persist:
            updated = save_aura_runtime_config(config, {"user_weather_cache": _upsert_user_weather_cache(config, cache_key, weather)})
            return updated, _cached_query_weather(updated, cache_key) or weather
        return config, _cached_query_weather(config, cache_key) or weather
    if current and _query_weather_within_ttl(config, current):
        return config, current
    return config, _error_snapshot(city=city, error=str(result.get("error") or "weather unavailable"))


def refresh_cached_weather_if_needed(
    config: AuraRuntimeConfig,
    *,
    city: str = "",
    force: bool = False,
) -> tuple[AuraRuntimeConfig, dict[str, Any]]:
    target_city = normalize_city_name(city or config.cached_weather_city or "")
    current = cached_weather_snapshot(config)
    if not config.weather_auto_refresh_enabled:
        return config, {"ok": False, "status": "disabled", "weather": current}
    if not target_city and not (config.weather_latitude and config.weather_longitude):
        return config, {"ok": False, "status": "missing_city", "weather": current, "error": "weather city is required"}
    should_refresh = force or _needs_refresh(config, current, target_city)
    if not should_refresh:
        return config, {"ok": True, "status": "cached", "weather": current}

    result = fetch_current_weather(
        city=target_city,
        provider=config.weather_provider,
        latitude=config.weather_latitude,
        longitude=config.weather_longitude,
        timeout_seconds=float(config.weather_request_timeout_seconds or 8),
    )
    if not result.get("ok"):
        updated = save_aura_runtime_config(config, {"weather_last_error": str(result.get("error") or "")[:500]})
        return updated, {"ok": False, "status": "failed", "error": result.get("error"), "weather": cached_weather_snapshot(updated)}

    weather = result["weather"]
    updated = save_aura_runtime_config(config, {
        "cached_weather_enabled": True,
        "cached_weather_city": normalize_city_name(weather.get("city") or target_city),
        "cached_weather_temperature": weather.get("temperature") or "",
        "cached_weather_condition": weather.get("condition") or "",
        "cached_weather_icon": weather.get("weather_icon") or 0,
        "cached_weather_humidity": weather.get("humidity") or "",
        "cached_weather_source": weather.get("source") or "",
        "cached_weather_observed_at": weather.get("observed_at") or "",
        "cached_weather_updated_at": int(time.time()),
        "weather_last_error": "",
    })
    return updated, {"ok": True, "status": "refreshed", "weather": cached_weather_snapshot(updated)}


def fetch_current_weather(
    *,
    city: str,
    provider: str = "open_meteo",
    latitude: str = "",
    longitude: str = "",
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    provider_key = str(provider or "open_meteo").strip().lower()
    if provider_key not in {"open_meteo", "open-meteo", "openmeteo"}:
        return {"ok": False, "error": f"unsupported weather provider: {provider or ''}"}
    try:
        normalized_city = normalize_city_name(city)
        lat, lon, resolved_city = _resolve_location(city=normalized_city, latitude=latitude, longitude=longitude, timeout_seconds=timeout_seconds)
        payload = _fetch_open_meteo(lat, lon, timeout_seconds=timeout_seconds)
        current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
        temperature = current.get("temperature_2m")
        humidity = current.get("relative_humidity_2m")
        code = int(current.get("weather_code") or 0)
        condition, icon = _weather_condition(code)
        weather = {
            "enabled": True,
            "status": "fresh",
            "city": normalize_city_name(resolved_city or normalized_city),
            "temperature": _format_temperature(temperature),
            "condition": condition,
            "weather_icon": icon,
            "humidity": _format_humidity(humidity),
            "updated_at": int(time.time()),
            "ttl_seconds": 0,
            "age_seconds": 0,
            "has_content": bool(temperature is not None or condition),
            "display": "",
            "source": "open_meteo",
            "observed_at": str(current.get("time") or ""),
            "latitude": lat,
            "longitude": lon,
            "weather_code": code,
        }
        weather["display"] = _display(weather)
        return {"ok": True, "weather": weather}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _needs_refresh(config: AuraRuntimeConfig, snapshot: dict[str, Any], target_city: str) -> bool:
    if snapshot.get("status") != "fresh":
        return True
    cached_city = normalize_city_name(snapshot.get("city") or "")
    target_city = normalize_city_name(target_city)
    if target_city and cached_city and target_city != cached_city:
        return True
    interval = max(60, int(config.weather_refresh_interval_seconds or 1800))
    age = snapshot.get("age_seconds")
    return age is None or int(age) >= interval


def _query_cache_key(config: AuraRuntimeConfig, *, city: str, latitude: str, longitude: str) -> str:
    provider = str(config.weather_provider or "open_meteo").strip().lower()
    lat = str(latitude or "").strip()
    lon = str(longitude or "").strip()
    city_text = normalize_city_name(city)
    return "|".join((provider, city_text, lat, lon))


def _cached_query_weather(config: AuraRuntimeConfig, cache_key: str, *, now: float | None = None) -> dict[str, Any] | None:
    cached = _QUERY_WEATHER_CACHE.get(cache_key)
    if not cached:
        return None
    current = time.time() if now is None else float(now)
    updated_at = int(cached.get("updated_at") or 0)
    if not updated_at:
        return None
    age_seconds = max(0, int(current - updated_at))
    ttl_seconds = max(60, int(config.cached_weather_ttl_seconds or 3600))
    if age_seconds > ttl_seconds:
        return None
    snapshot = dict(cached)
    snapshot["city"] = normalize_city_name(snapshot.get("city") or "")
    snapshot["status"] = "fresh"
    snapshot["age_seconds"] = age_seconds
    snapshot["ttl_seconds"] = ttl_seconds
    snapshot["display"] = _display(snapshot)
    return snapshot


def _persisted_query_weather(config: AuraRuntimeConfig, cache_key: str, *, now: float | None = None) -> dict[str, Any] | None:
    current = time.time() if now is None else float(now)
    for item in config.user_weather_cache or ():
        if str(item.get("key") or "").strip() != cache_key:
            continue
        updated_at = int(item.get("updated_at") or 0)
        if not updated_at:
            return None
        ttl_seconds = max(60, int(item.get("ttl_seconds") or config.cached_weather_ttl_seconds or 3600))
        age_seconds = max(0, int(current - updated_at))
        if age_seconds > ttl_seconds:
            return None
        snapshot = dict(item)
        snapshot["city"] = normalize_city_name(snapshot.get("city") or "")
        snapshot["status"] = "fresh"
        snapshot["enabled"] = True
        snapshot["age_seconds"] = age_seconds
        snapshot["ttl_seconds"] = ttl_seconds
        snapshot["has_content"] = bool(snapshot.get("temperature") or snapshot.get("condition"))
        snapshot["display"] = _display(snapshot)
        _QUERY_WEATHER_CACHE[cache_key] = snapshot
        return snapshot
    return None


def _upsert_user_weather_cache(config: AuraRuntimeConfig, cache_key: str, weather: dict[str, Any]) -> list[dict[str, Any]]:
    entry = {
        "key": cache_key,
        "city": normalize_city_name(weather.get("city") or ""),
        "temperature": str(weather.get("temperature") or "").strip(),
        "condition": str(weather.get("condition") or "").strip(),
        "weather_icon": int(weather.get("weather_icon") or 0),
        "humidity": str(weather.get("humidity") or "").strip(),
        "source": str(weather.get("source") or "").strip(),
        "observed_at": str(weather.get("observed_at") or "").strip(),
        "updated_at": int(weather.get("updated_at") or time.time()),
        "ttl_seconds": max(60, int(config.cached_weather_ttl_seconds or 3600)),
        "display": _display(weather),
        "latitude": str(weather.get("latitude") or "").strip(),
        "longitude": str(weather.get("longitude") or "").strip(),
    }
    rows = [entry]
    for item in config.user_weather_cache or ():
        if str(item.get("key") or "").strip() == cache_key:
            continue
        rows.append(dict(item))
        if len(rows) >= 12:
            break
    return rows


def _query_weather_with_cache_metadata(config: AuraRuntimeConfig, weather: dict[str, Any]) -> dict[str, Any]:
    snapshot = dict(weather or {})
    snapshot["updated_at"] = int(time.time())
    snapshot["ttl_seconds"] = max(60, int(config.cached_weather_ttl_seconds or 3600))
    snapshot["age_seconds"] = 0
    snapshot["status"] = "fresh"
    return snapshot


def _needs_query_refresh(config: AuraRuntimeConfig, snapshot: dict[str, Any]) -> bool:
    interval = max(60, int(config.weather_refresh_interval_seconds or 1800))
    age = snapshot.get("age_seconds")
    return age is None or int(age) >= interval


def _query_weather_within_ttl(config: AuraRuntimeConfig, snapshot: dict[str, Any]) -> bool:
    age = snapshot.get("age_seconds")
    if age is None:
        return False
    ttl_seconds = max(60, int(config.cached_weather_ttl_seconds or 3600))
    return int(age) <= ttl_seconds


def _resolve_location(*, city: str, latitude: str, longitude: str, timeout_seconds: float) -> tuple[float, float, str]:
    lat_text = str(latitude or "").strip()
    lon_text = str(longitude or "").strip()
    if lat_text and lon_text:
        return float(lat_text), float(lon_text), str(city or "").strip()
    city_text = normalize_city_name(city)
    if city_text in CITY_COORDS:
        lat, lon, _admin = CITY_COORDS[city_text]
        return lat, lon, city_text
    params = urlencode({"name": city_text, "count": 10, "language": "zh", "format": "json"})
    data = _read_json(f"{OPEN_METEO_GEOCODE_URL}?{params}", timeout_seconds=timeout_seconds)
    results = data.get("results") if isinstance(data.get("results"), list) else []
    if not results:
        raise ValueError(f"weather city not found: {city_text}")
    best = _best_geocode_result(results)
    return float(best["latitude"]), float(best["longitude"]), normalize_city_name(best.get("name") or city_text)


def _best_geocode_result(results: list[dict[str, Any]]) -> dict[str, Any]:
    china = [item for item in results if str(item.get("country_code") or "").upper() == "CN"]
    candidates = china or results
    return sorted(candidates, key=lambda item: int(item.get("population") or 0), reverse=True)[0]


def _fetch_open_meteo(latitude: float, longitude: float, *, timeout_seconds: float) -> dict[str, Any]:
    params = urlencode({
        "latitude": f"{latitude:.6f}",
        "longitude": f"{longitude:.6f}",
        "current": "temperature_2m,relative_humidity_2m,weather_code,is_day",
        "timezone": "auto",
        "forecast_days": 1,
    })
    return _read_json(f"{OPEN_METEO_FORECAST_URL}?{params}", timeout_seconds=timeout_seconds)


def _read_json(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    with urlopen(url, timeout=max(1.0, float(timeout_seconds or 8))) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("weather service returned invalid JSON")
    return data


def _weather_condition(code: int) -> tuple[str, int]:
    if code == 0:
        return "晴", 0
    if code in {1, 2, 3}:
        return "多云", 1
    if code in {45, 48}:
        return "雾", 1
    if 51 <= code <= 67 or 80 <= code <= 82 or 95 <= code <= 99:
        return "雨", 2
    if 71 <= code <= 77 or 85 <= code <= 86:
        return "雪", 3
    return "多云", 1


def _format_temperature(value: Any) -> str:
    if value is None:
        return ""
    number = float(value)
    return f"{number:.1f}".rstrip("0").rstrip(".")


def _format_humidity(value: Any) -> str:
    if value is None:
        return ""
    return str(int(round(float(value))))


def _display(weather: dict[str, Any]) -> str:
    parts = []
    if weather.get("city"):
        parts.append(normalize_city_name(weather["city"]))
    if weather.get("temperature"):
        parts.append(f"{weather['temperature']}度")
    if weather.get("condition"):
        parts.append(str(weather["condition"]))
    if weather.get("humidity"):
        parts.append(f"湿度{weather['humidity']}%")
    return "，".join(parts)


def _disabled_snapshot(*, city: str) -> dict[str, Any]:
    return {
        "enabled": False,
        "status": "disabled",
        "city": normalize_city_name(city),
        "temperature": "",
        "condition": "",
        "weather_icon": 0,
        "humidity": "",
        "updated_at": 0,
        "ttl_seconds": 0,
        "age_seconds": None,
        "has_content": False,
        "display": "",
    }


def _error_snapshot(*, city: str, error: str) -> dict[str, Any]:
    snapshot = _disabled_snapshot(city=city)
    snapshot["enabled"] = True
    snapshot["status"] = "error"
    snapshot["error"] = error
    return snapshot
