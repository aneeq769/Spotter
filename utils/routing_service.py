"""
routing_service.py
------------------
Thin wrapper around the Mapbox APIs.

Uses:
  - Mapbox Geocoding API  (geocode city/address -> lat/lon)
  - Mapbox Directions API (driving route geometry + distance)
"""

import logging
import math
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

MAPBOX_BASE = "https://api.mapbox.com"
_TIMEOUT = (15, 60)


class RoutingError(Exception):
    """Raised when the routing service cannot fulfil the request."""


class RoutingService:
    def __init__(self, token: str) -> None:
        self._token = token
        self._session = requests.Session()
        self._session.trust_env = False
        self._api_calls_made = 0

    def get_route(self, origin: str, destination: str) -> dict[str, Any]:
        origin_coords = self._geocode(origin)
        destination_coords = self._geocode(destination)
        route_data = self._directions(origin_coords, destination_coords)

        waypoints = self._sample_waypoints(
            route_data["coordinates"],
            route_data["distance_miles"],
            interval_miles=10,
        )

        return {
            "origin_coords": origin_coords,
            "destination_coords": destination_coords,
            "distance_miles": route_data["distance_miles"],
            "duration_seconds": route_data["duration_seconds"],
            "waypoints": waypoints,
            "geojson_geometry": route_data["geojson_geometry"],
            "api_calls_made": self._api_calls_made,
        }

    def _geocode(self, location: str) -> tuple[float, float]:
        url = f"{MAPBOX_BASE}/geocoding/v5/mapbox.places/{requests.utils.quote(location)}.json"
        params = {
            "access_token": self._token,
            "country": "us",
            "limit": 1,
        }
        self._api_calls_made += 1
        resp = self._get(url, params=params)
        features = resp.get("features", [])
        if not features:
            raise RoutingError(
                f"Could not geocode location: '{location}'. "
                "Please provide a more specific US city/address."
            )
        lon, lat = features[0]["geometry"]["coordinates"]
        return float(lat), float(lon)

    def _directions(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
    ) -> dict[str, Any]:
        coords = f"{origin[1]},{origin[0]};{destination[1]},{destination[0]}"
        url = f"{MAPBOX_BASE}/directions/v5/mapbox/driving/{coords}"
        params = {
            "access_token": self._token,
            "geometries": "geojson",
            "overview": "full",
            "steps": "false",
        }
        self._api_calls_made += 1
        resp = self._get(url, params=params)
        routes = resp.get("routes", [])
        if not routes:
            raise RoutingError("Mapbox returned no route between those locations.")

        route = routes[0]
        geometry = route["geometry"]
        return {
            "distance_miles": route["distance"] / 1609.344,
            "duration_seconds": route["duration"],
            "coordinates": geometry["coordinates"],
            "geojson_geometry": geometry,
        }

    @staticmethod
    def _sample_waypoints(
        coords: list[list[float]],
        total_miles: float,
        interval_miles: float = 10,
    ) -> list[tuple[float, float]]:
        if not coords:
            return []

        sampled: list[tuple[float, float]] = [(coords[0][1], coords[0][0])]
        accumulated = 0.0
        prev_lon, prev_lat = coords[0]

        for lon, lat in coords[1:]:
            seg = _haversine_miles(prev_lat, prev_lon, lat, lon)
            accumulated += seg
            if accumulated >= interval_miles:
                sampled.append((lat, lon))
                accumulated = 0.0
            prev_lat, prev_lon = lat, lon

        last = (coords[-1][1], coords[-1][0])
        if sampled[-1] != last:
            sampled.append(last)

        return sampled

    def _get(self, url: str, params: dict | None = None, retries: int = 2) -> dict:
        for attempt in range(retries + 1):
            try:
                r = self._session.get(url, params=params, timeout=_TIMEOUT)
                if r.status_code == 429 and attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException as exc:
                if attempt == retries:
                    raise RoutingError(f"Mapbox GET request failed: {exc}") from exc
                time.sleep(1)
        return {}


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + (
        math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(a))