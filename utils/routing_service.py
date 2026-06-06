"""
routing_service.py
------------------
Thin wrapper around the OpenRouteService (ORS) Directions API.

Why ORS?
--------
* Free tier: 2 000 req/day, 40 req/min – well within our needs.
* Returns full route geometry (GeoJSON LineString) + total distance.
* Uses 2 geocode calls + 1 directions call for reliable routing.
"""

import logging
import math
import time
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

ORS_BASE = "https://api.openrouteservice.org"
ORS_GEOCODE = f"{ORS_BASE}/geocode/search"
ORS_DIRECTIONS = f"{ORS_BASE}/v2/directions/driving-car/geojson"

_TIMEOUT = (10, 40)


class RoutingError(Exception):
    """Raised when the routing service cannot fulfil the request."""


class RoutingService:
    """
    Wraps ORS API calls.  One instance per request.
    API call budget: 3 calls per request.
    """

    def __init__(self, api_key: str) -> None:
        self._key = api_key
        self._session = requests.Session()
        self._session.trust_env = False
        self._headers = {
            "Authorization": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json, application/geo+json",
        }
        self._api_calls_made = 0

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def get_route(self, origin: str, destination: str) -> dict[str, Any]:
        """
        Geocode both locations and retrieve the driving route.

        Strategy (minimise API calls):
          1. Try a SINGLE directions call with ORS inline text geocoding
             (resolve_locations=true).  → 1 API call if it works.
          2. On failure fall back to explicit geocode × 2 + directions.
             → 3 API calls maximum.

        Returns:
            origin_coords, destination_coords, distance_miles,
            duration_seconds, waypoints, geojson_geometry, api_calls_made
        """
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

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _directions_with_text(
        self,
        origin: str,
        destination: str,
    ) -> tuple[dict, tuple[float, float], tuple[float, float]] | None:
        """
        Try to get route + geocoding in ONE call by passing address strings
        directly into the ORS directions endpoint with resolve_locations=true.
        Returns (route_data, origin_coords, dest_coords) or None on failure.
        """
        payload = {
            "coordinates": [origin, destination],
            "resolve_locations": True,
            "format": "geojson",
            "instructions": False,
        }
        self._api_calls_made += 1
        try:
            r = self._session.post(
                ORS_DIRECTIONS,
                json=payload,
                headers=self._headers,
                timeout=_TIMEOUT,
            )
            if r.status_code not in (200, 201):
                logger.debug("Single-call directions returned %d; will fall back.", r.status_code)
                return None
            resp = r.json()
            feature = resp["features"][0]
            props = feature["properties"]["summary"]
            geometry = feature["geometry"]
            coords = geometry["coordinates"]

            # Extract resolved coordinates from waypoints if available
            waypoint_meta = feature["properties"].get("way_points", [])
            segments = feature["properties"].get("segments", [])

            # Grab resolved lat/lon from the first and last coordinate
            origin_coords = (float(coords[0][1]), float(coords[0][0]))
            dest_coords = (float(coords[-1][1]), float(coords[-1][0]))

            route_data = {
                "distance_miles": props["distance"] / 1609.344,
                "duration_seconds": props["duration"],
                "coordinates": coords,
                "geojson_geometry": geometry,
            }
            logger.info("Route fetched in 1 ORS API call.")
            return route_data, origin_coords, dest_coords

        except Exception as exc:  # noqa: BLE001
            logger.debug("Single-call attempt failed: %s", exc)
            return None

    def _geocode(self, location: str) -> tuple[float, float]:
        """Geocode a free-text US location. Returns (lat, lon)."""
        params = {
            "api_key": self._key,
            "text": location,
            "boundary.country": "US",
            "size": 1,
        }
        self._api_calls_made += 1
        resp = self._get(ORS_GEOCODE, params=params)
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
        """Fetch driving directions between two (lat, lon) coordinate pairs."""
        payload = {
            "coordinates": [
                [origin[1], origin[0]],
                [destination[1], destination[0]],
            ],
            "format": "geojson",
            "instructions": False,
        }
        self._api_calls_made += 1
        resp = self._post(ORS_DIRECTIONS, json=payload)
        try:
            feature = resp["features"][0]
            props = feature["properties"]["summary"]
            geometry = feature["geometry"]
            coords = geometry["coordinates"]
        except (KeyError, IndexError) as exc:
            raise RoutingError("Unexpected ORS response format.") from exc

        return {
            "distance_miles": props["distance"] / 1609.344,
            "duration_seconds": props["duration"],
            "coordinates": coords,
            "geojson_geometry": geometry,
        }

    @staticmethod
    def _sample_waypoints(
        coords: list[list[float]],
        total_miles: float,
        interval_miles: float = 10,
    ) -> list[tuple[float, float]]:
        """
        Downsample the dense route polyline to one point every ~interval_miles.
        Returns a list of (lat, lon) tuples including first and last points.
        """
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

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, url: str, params: dict | None = None, retries: int = 2) -> dict:
        for attempt in range(retries + 1):
            try:
                r = self._session.get(url, params=params, headers=self._headers, timeout=_TIMEOUT)
                if r.status_code == 429 and attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException as exc:
                if attempt == retries:
                    raise RoutingError(f"ORS GET request failed: {exc}") from exc
                time.sleep(1)
        return {}

    def _post(self, url: str, json: dict | None = None, retries: int = 2) -> dict:
        for attempt in range(retries + 1):
            try:
                r = self._session.post(url, json=json, headers=self._headers, timeout=_TIMEOUT)
                if r.status_code == 429 and attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException as exc:
                if attempt == retries:
                    raise RoutingError(f"ORS POST request failed: {exc}") from exc
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
