"""National Weather Service client for location-based alerts and forecasts."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlencode

import requests

NWS_BASE_URL = "https://api.weather.gov"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
USER_AGENT = os.getenv(
    "NWS_USER_AGENT",
    "lakebase-weather-demo/1.0 (contact: weather-demo@example.com)",
)
DEFAULT_LOCATIONS = [
    "Chicago, IL",
    "Austin, TX",
    "New York, NY",
    "Atlanta, GA",
]
REQUEST_TIMEOUT = 20


class WeatherClientError(Exception):
    """Raised when weather data cannot be fetched or normalized."""


class WeatherClient:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/geo+json, application/ld+json, application/json",
            }
        )

    def _get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()

    def resolve_location(self, location: str) -> dict[str, Any]:
        location = (location or "").strip()
        if not location:
            raise WeatherClientError("Location cannot be empty")

        if "," in location:
            parts = [part.strip() for part in location.split(",")]
            if len(parts) == 2:
                try:
                    lat = float(parts[0])
                    lon = float(parts[1])
                    return {
                        "label": f"{lat},{lon}",
                        "latitude": lat,
                        "longitude": lon,
                    }
                except ValueError:
                    pass

        payload = self._get_json(GEOCODE_URL, params={"name": location, "count": 1, "language": "en", "format": "json"})
        results = payload.get("results") or []
        if not results:
            raise WeatherClientError(f"Could not resolve location: {location}")

        match = results[0]
        label_parts = [match.get("name"), match.get("admin1"), match.get("country_code")]
        label = ", ".join(part for part in label_parts if part)
        return {
            "label": label or location,
            "latitude": match["latitude"],
            "longitude": match["longitude"],
        }

    def get_point_metadata(self, latitude: float, longitude: float) -> dict[str, Any]:
        return self._get_json(f"{NWS_BASE_URL}/points/{latitude},{longitude}")

    def get_alerts_for_point(self, latitude: float, longitude: float) -> list[dict[str, Any]]:
        payload = self._get_json(f"{NWS_BASE_URL}/alerts/active", params={"point": f"{latitude},{longitude}"})
        return payload.get("features") or []

    def get_forecast(self, forecast_url: str) -> dict[str, Any]:
        return self._get_json(forecast_url)

    def fetch_documents_for_location(self, location: str) -> list[dict[str, Any]]:
        resolved = self.resolve_location(location)
        latitude = resolved["latitude"]
        longitude = resolved["longitude"]
        location_label = resolved["label"]

        point_payload = self.get_point_metadata(latitude, longitude)
        point_props = point_payload.get("properties") or {}

        documents: list[dict[str, Any]] = []
        documents.extend(self._normalize_alerts(location_label, self.get_alerts_for_point(latitude, longitude)))

        forecast_url = point_props.get("forecast")
        if forecast_url:
            forecast_payload = self.get_forecast(forecast_url)
            documents.extend(self._normalize_forecasts(location_label, forecast_payload))

        return documents

    def _normalize_alerts(self, location_label: str, features: list[dict[str, Any]]) -> list[dict[str, Any]]:
        documents = []
        for feature in features:
            props = feature.get("properties") or {}
            alert_id = feature.get("id") or props.get("id")
            if not alert_id:
                continue
            narrative_text = "\n\n".join(
                part.strip()
                for part in [props.get("headline"), props.get("description"), props.get("instruction")]
                if isinstance(part, str) and part.strip()
            ) or (props.get("description") or props.get("event") or "Weather alert")
            documents.append(
                {
                    "id": f"alert:{alert_id}",
                    "location": location_label,
                    "source_type": "alert",
                    "headline": props.get("headline") or props.get("event") or "Weather alert",
                    "narrative_text": narrative_text,
                    "severity": props.get("severity"),
                    "event_type": props.get("event"),
                    "issued_at": props.get("sent") or props.get("onset"),
                    "effective_at": props.get("effective"),
                    "expires": props.get("expires"),
                    "payload": feature,
                }
            )
        return documents

    def _normalize_forecasts(self, location_label: str, forecast_payload: dict[str, Any]) -> list[dict[str, Any]]:
        periods = ((forecast_payload.get("properties") or {}).get("periods")) or []
        documents = []
        for period in periods:
            period_number = period.get("number", 0)
            start_time = period.get("startTime") or "unknown"
            forecast_id = f"forecast:{location_label}:{period_number}:{start_time}"
            detailed = period.get("detailedForecast") or period.get("shortForecast") or "Forecast"
            documents.append(
                {
                    "id": forecast_id,
                    "location": location_label,
                    "source_type": "forecast",
                    "headline": period.get("name") or "Forecast",
                    "narrative_text": detailed,
                    "severity": None,
                    "event_type": "forecast",
                    "issued_at": (forecast_payload.get("properties") or {}).get("updateTime"),
                    "effective_at": period.get("startTime"),
                    "expires": period.get("endTime"),
                    "payload": period,
                }
            )
        return documents


def sync_locations(locations: list[str] | None = None, limit: int = 50) -> dict[str, Any]:
    client = WeatherClient()
    requested_locations = locations or DEFAULT_LOCATIONS
    documents: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for location in requested_locations:
        try:
            documents.extend(client.fetch_documents_for_location(location))
        except requests.RequestException as exc:
            errors.append({"location": location, "error": f"NWS request failed: {exc}"})
        except WeatherClientError as exc:
            errors.append({"location": location, "error": str(exc)})
        if len(documents) >= limit:
            break

    deduped: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for document in documents:
        doc_id = document["id"]
        if doc_id in seen_ids:
            continue
        seen_ids.add(doc_id)
        deduped.append(document)
        if len(deduped) >= limit:
            break

    return {"documents": deduped, "errors": errors, "requested_locations": requested_locations}
