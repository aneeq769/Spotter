"""
tests.py
--------
Unit and integration tests for the Fuel Route Optimizer API.

Run with:  python manage.py test
Or:        pytest
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import TestCase
from rest_framework.test import APITestCase
from rest_framework import status

from utils.fuel_station_service import FuelStationService
from utils.fuel_optimizer import FuelOptimizer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CSV_PATH = Path(__file__).resolve().parent.parent / "fuel_prices.csv"


def _make_station_service() -> FuelStationService:
    return FuelStationService(CSV_PATH)


# ---------------------------------------------------------------------------
# FuelStationService tests
# ---------------------------------------------------------------------------

class FuelStationServiceTest(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.svc = _make_station_service()

    def test_loads_stations(self):
        self.assertGreater(len(self.svc.dataframe), 0)

    def test_all_rows_have_price(self):
        self.assertFalse(self.svc.dataframe["price"].isna().any())

    def test_stations_near_chicago(self):
        # Chicago IL: 41.85, -87.65
        results = self.svc.stations_near_point(41.85, -87.65, radius_miles=100)
        self.assertGreater(len(results), 0)
        # All returned prices should be floats > 0
        for _, row in results.iterrows():
            self.assertGreater(row["price"], 0)

    def test_cheapest_station_near_point_returns_dict(self):
        result = self.svc.cheapest_station_near_point(39.95, -82.99, radius_miles=100)
        self.assertIsNotNone(result)
        self.assertIn("price_per_gallon", result)
        self.assertIn("name", result)

    def test_no_station_far_out_to_sea(self):
        # Middle of the Atlantic
        result = self.svc.cheapest_station_near_point(35.0, -50.0, radius_miles=50)
        self.assertIsNone(result)

    def test_cheapest_is_first(self):
        results = self.svc.stations_near_point(33.75, -84.39, radius_miles=100)
        if len(results) > 1:
            prices = results["price"].tolist()
            self.assertEqual(prices, sorted(prices))


# ---------------------------------------------------------------------------
# FuelOptimizer tests
# ---------------------------------------------------------------------------

class FuelOptimizerTest(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.svc = _make_station_service()
        cls.optimizer = FuelOptimizer(cls.svc, max_range_miles=500, mpg=10.0)

    def _simple_waypoints(self, n=60):
        """Straight line from LA (34.05, -118.24) to NYC (40.71, -74.01)."""
        lats = [34.05 + (40.71 - 34.05) * i / (n - 1) for i in range(n)]
        lons = [-118.24 + (-74.01 - (-118.24)) * i / (n - 1) for i in range(n)]
        return list(zip(lats, lons))

    def test_plan_produces_stops(self):
        wps = self._simple_waypoints()
        plan = self.optimizer.plan(
            waypoints=wps,
            total_distance_miles=2800,
            duration_seconds=144000,
            start_location="Los Angeles, CA",
            end_location="New York, NY",
            origin_coords=(34.05, -118.24),
            destination_coords=(40.71, -74.01),
        )
        # ~2800 miles / 500 mile range = at least 5 stops
        self.assertGreaterEqual(len(plan.stops), 4)

    def test_total_gallons_correct(self):
        wps = self._simple_waypoints()
        plan = self.optimizer.plan(
            waypoints=wps,
            total_distance_miles=2800,
            duration_seconds=144000,
            start_location="Los Angeles, CA",
            end_location="New York, NY",
            origin_coords=(34.05, -118.24),
            destination_coords=(40.71, -74.01),
        )
        expected_gallons = 2800 / 10
        self.assertAlmostEqual(plan.total_gallons, expected_gallons, places=1)

    def test_total_cost_is_positive(self):
        wps = self._simple_waypoints()
        plan = self.optimizer.plan(
            waypoints=wps,
            total_distance_miles=2800,
            duration_seconds=144000,
            start_location="Los Angeles, CA",
            end_location="New York, NY",
            origin_coords=(34.05, -118.24),
            destination_coords=(40.71, -74.01),
        )
        self.assertGreater(plan.total_fuel_cost, 0)

    def test_short_trip_under_one_tank(self):
        """A 400-mile trip should need 0 stops (fits in one tank)."""
        # LA to Las Vegas
        wps = [(34.05 + (36.17 - 34.05) * i / 9, -118.24 + (-115.14 - (-118.24)) * i / 9)
               for i in range(10)]
        plan = self.optimizer.plan(
            waypoints=wps,
            total_distance_miles=270,
            duration_seconds=14400,
            start_location="Los Angeles, CA",
            end_location="Las Vegas, NV",
            origin_coords=(34.05, -118.24),
            destination_coords=(36.17, -115.14),
        )
        self.assertEqual(len(plan.stops), 0)


# ---------------------------------------------------------------------------
# API endpoint tests (integration, ORS mocked)
# ---------------------------------------------------------------------------

MOCK_GEOCODE_LA = {
    "features": [{"geometry": {"coordinates": [-118.2437, 34.0522]}}]
}
MOCK_GEOCODE_NYC = {
    "features": [{"geometry": {"coordinates": [-74.0060, 40.7128]}}]
}
MOCK_DIRECTIONS = {
    "features": [
        {
            "properties": {
                "summary": {
                    "distance": 4506163,   # ~2800 miles in metres
                    "duration": 144000,
                }
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [-118.2437 + i * ((-74.0060 - (-118.2437)) / 59), 34.0522 + i * ((40.7128 - 34.0522) / 59)]
                    for i in range(60)
                ],
            },
        }
    ]
}


class RouteAPITest(APITestCase):

    def _mock_get(self, url, **kwargs):
        """Mock requests.get to return geocode responses."""
        mock = MagicMock()
        mock.status_code = 200
        mock.raise_for_status = MagicMock()
        if "Los Angeles" in str(kwargs.get("params", {})):
            mock.json.return_value = MOCK_GEOCODE_LA
        else:
            mock.json.return_value = MOCK_GEOCODE_NYC
        return mock

    def _mock_post(self, url, **kwargs):
        """Mock requests.post to return the directions response."""
        mock = MagicMock()
        mock.status_code = 200
        mock.raise_for_status = MagicMock()
        mock.json.return_value = MOCK_DIRECTIONS
        return mock

    @patch("utils.routing_service.requests.post")
    @patch("utils.routing_service.requests.get")
    @patch("django.conf.settings.ORS_API_KEY", "fake-key")
    def test_route_endpoint_returns_200(self, mock_get, mock_post):
        mock_get.side_effect = self._mock_get
        mock_post.side_effect = self._mock_post

        response = self.client.post(
            "/api/route/",
            data={"start": "Los Angeles, CA", "end": "New York, NY"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch("utils.routing_service.requests.post")
    @patch("utils.routing_service.requests.get")
    @patch("django.conf.settings.ORS_API_KEY", "fake-key")
    def test_route_response_structure(self, mock_get, mock_post):
        mock_get.side_effect = self._mock_get
        mock_post.side_effect = self._mock_post

        response = self.client.post(
            "/api/route/",
            data={"start": "Los Angeles, CA", "end": "New York, NY"},
            format="json",
        )
        data = response.json()
        required_keys = [
            "start", "end", "total_distance_miles", "total_fuel_cost_usd",
            "total_gallons_needed", "fuel_stops", "route_map", "api_calls_to_ors",
        ]
        for key in required_keys:
            self.assertIn(key, data, f"Missing key: {key}")

    @patch("utils.routing_service.requests.post")
    @patch("utils.routing_service.requests.get")
    @patch("django.conf.settings.ORS_API_KEY", "fake-key")
    def test_route_map_is_geojson_feature_collection(self, mock_get, mock_post):
        mock_get.side_effect = self._mock_get
        mock_post.side_effect = self._mock_post

        response = self.client.post(
            "/api/route/",
            data={"start": "Los Angeles, CA", "end": "New York, NY"},
            format="json",
        )
        data = response.json()
        self.assertEqual(data["route_map"]["type"], "FeatureCollection")
        self.assertIn("features", data["route_map"])

    def test_missing_start_field_returns_400(self):
        response = self.client.post(
            "/api/route/",
            data={"end": "New York, NY"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_missing_end_field_returns_400(self):
        response = self.client.post(
            "/api/route/",
            data={"start": "Los Angeles, CA"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_no_api_key_returns_503(self):
        with self.settings(ORS_API_KEY=""):
            response = self.client.post(
                "/api/route/",
                data={"start": "Los Angeles, CA", "end": "New York, NY"},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
