"""
fuel_optimizer.py
-----------------
Greedy algorithm that plans the cheapest sequence of fuel stops along a route.
"""

import math
import logging
from dataclasses import dataclass, field
from typing import Any

from utils.fuel_station_service import FuelStationService, _haversine_miles  # noqa: PLC2701

logger = logging.getLogger(__name__)

SEARCH_RADIUS_MILES = 60
MIN_PRICE_ADVANTAGE = 0.05


@dataclass
class FuelStop:
    station_id: int
    name: str
    address: str
    city: str
    state: str
    price_per_gallon: float
    lat: float
    lon: float
    miles_from_start: float
    gallons_purchased: float
    cost_at_stop: float


@dataclass
class FuelPlan:
    stops: list[FuelStop] = field(default_factory=list)
    total_fuel_cost: float = 0.0
    total_gallons: float = 0.0
    total_distance_miles: float = 0.0
    start_location: str = ""
    end_location: str = ""
    duration_seconds: float = 0.0


class FuelOptimizer:
    """
    Plans the cheapest sequence of fuel stops for a given route.
    """

    def __init__(
        self,
        station_service: FuelStationService,
        max_range_miles: int = 500,
        mpg: float = 10.0,
        search_radius: float = SEARCH_RADIUS_MILES,
    ) -> None:
        self._svc = station_service
        self.max_range = max_range_miles
        self.mpg = mpg
        self.tank_cap = max_range_miles / mpg
        self.search_radius = search_radius

    def plan(
        self,
        waypoints: list[tuple[float, float]],
        total_distance_miles: float,
        duration_seconds: float,
        start_location: str,
        end_location: str,
        origin_coords: tuple[float, float],
        destination_coords: tuple[float, float],
    ) -> FuelPlan:
        plan = FuelPlan(
            total_distance_miles=total_distance_miles,
            start_location=start_location,
            end_location=end_location,
            duration_seconds=duration_seconds,
        )

        cum_dist = self._cumulative_distances(waypoints)
        tank_miles_remaining = self.max_range
        current_wp_idx = 0
        max_iterations = len(waypoints) + 10
        iteration = 0

        while iteration < max_iterations:
            iteration += 1
            current_dist = cum_dist[current_wp_idx]
            reachable_dist = current_dist + tank_miles_remaining

            if reachable_dist >= total_distance_miles:
                break

            max_reachable_idx = self._furthest_reachable_idx(
                cum_dist, current_dist, tank_miles_remaining
            )

            if max_reachable_idx <= current_wp_idx:
                max_reachable_idx = min(current_wp_idx + 1, len(waypoints) - 1)

            best_stop = self._find_best_stop(
                waypoints,
                cum_dist,
                current_wp_idx + 1,
                max_reachable_idx,
            )

            if best_stop is None:
                logger.warning("No station found in forward window from wp %d.", current_wp_idx)
                best_stop = self._emergency_station(waypoints[current_wp_idx])
                if best_stop is None:
                    logger.error("No station found near waypoint %d. Skipping ahead.", current_wp_idx)
                    current_wp_idx = min(current_wp_idx + max(1, (max_reachable_idx - current_wp_idx) // 2), len(waypoints) - 1)
                    continue

            stop_wp_idx = self._nearest_waypoint_idx(
                waypoints, best_stop["lat"], best_stop["lon"], max_idx=max_reachable_idx
            )

            if stop_wp_idx <= current_wp_idx:
                stop_wp_idx = min(current_wp_idx + 1, len(waypoints) - 1)

            miles_to_stop = cum_dist[stop_wp_idx] - current_dist
            if miles_to_stop <= 0:
                current_wp_idx = stop_wp_idx
                continue

            tank_miles_remaining -= miles_to_stop
            gallons_purchased = self.tank_cap - (tank_miles_remaining / self.mpg)
            gallons_purchased = max(0.0, gallons_purchased)
            cost = gallons_purchased * best_stop["price_per_gallon"]

            plan.stops.append(
                FuelStop(
                    station_id=best_stop["station_id"],
                    name=best_stop["name"],
                    address=best_stop["address"],
                    city=best_stop["city"],
                    state=best_stop["state"],
                    price_per_gallon=best_stop["price_per_gallon"],
                    lat=best_stop["lat"],
                    lon=best_stop["lon"],
                    miles_from_start=cum_dist[stop_wp_idx],
                    gallons_purchased=round(gallons_purchased, 4),
                    cost_at_stop=round(cost, 4),
                )
            )

            tank_miles_remaining = self.max_range
            current_wp_idx = stop_wp_idx

        plan.total_gallons = round(total_distance_miles / self.mpg, 4)
        plan.total_fuel_cost = round(sum(s.cost_at_stop for s in plan.stops), 4)

        return plan

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cumulative_distances(waypoints: list[tuple[float, float]]) -> list[float]:
        dist = [0.0]
        for i in range(1, len(waypoints)):
            d = _haversine_miles(
                waypoints[i - 1][0], waypoints[i - 1][1],
                waypoints[i][0], waypoints[i][1],
            )
            dist.append(dist[-1] + d)
        return dist

    @staticmethod
    def _furthest_reachable_idx(
        cum_dist: list[float],
        current_dist: float,
        tank_miles: float,
    ) -> int:
        limit = current_dist + tank_miles
        idx = 0
        for i, d in enumerate(cum_dist):
            if d <= limit:
                idx = i
            else:
                break
        return idx

    def _find_best_stop(
        self,
        waypoints: list[tuple[float, float]],
        cum_dist: list[float],
        start_idx: int,
        end_idx: int,
    ) -> dict[str, Any] | None:
        best: dict[str, Any] | None = None
        step = max(1, (end_idx - start_idx) // 10)

        for idx in range(start_idx, end_idx + 1, step):
            wp = waypoints[idx]
            station = self._svc.cheapest_station_near_point(
                wp[0], wp[1], radius_miles=self.search_radius
            )
            if station is None:
                continue
            if best is None or station["price_per_gallon"] < best["price_per_gallon"]:
                best = station

        return best

    def _emergency_station(self, waypoint: tuple[float, float]) -> dict[str, Any] | None:
        for radius in (100, 150, 200):
            station = self._svc.cheapest_station_near_point(
                waypoint[0], waypoint[1], radius_miles=radius
            )
            if station:
                return station
        return None

    @staticmethod
    def _nearest_waypoint_idx(
        waypoints: list[tuple[float, float]],
        lat: float,
        lon: float,
        max_idx: int,
    ) -> int:
        best_dist = float("inf")
        best_idx = 0
        for i in range(min(max_idx + 1, len(waypoints))):
            d = _haversine_miles(lat, lon, waypoints[i][0], waypoints[i][1])
            if d < best_dist:
                best_dist = d
                best_idx = i
        return best_idx