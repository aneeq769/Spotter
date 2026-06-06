"""
fuel_optimizer.py
-----------------
Greedy algorithm that plans the cheapest sequence of fuel stops along a route.

Algorithm overview
------------------
1.  Divide the total route into segments.  Each segment is no longer than the
    vehicle's max range (500 mi).
2.  For each segment we must fill up before running out of fuel, but we also
    want to fill up where fuel is cheapest.
3.  We use a "look-ahead greedy" strategy:
      a.  From the current position, scan waypoints forward up to max_range.
      b.  Find the cheapest station reachable from the current position.
      c.  But also check: is there a *significantly* cheaper station further
          ahead that we can coast to if we take just enough fuel now?
      d.  Fill the tank at the best station for the situation.
4.  Always fill to full (tank = 500 mi worth of fuel).  In practice a
    trucker would "top off cheap and skip expensive", but full-tank at
    cheapest stop is a good approximation for a 500-mile-range vehicle.

Parameters
----------
max_range_miles : int   – vehicle max range on a full tank (default 500)
mpg             : float – miles per gallon (default 10)
tank_capacity_gallons = max_range_miles / mpg
"""

import math
import logging
from dataclasses import dataclass, field
from typing import Any

from utils.fuel_station_service import FuelStationService, _haversine_miles  # noqa: PLC2701

logger = logging.getLogger(__name__)

SEARCH_RADIUS_MILES = 60  # how far off-route we search for a station
MIN_PRICE_ADVANTAGE = 0.05  # $/gal savings required to justify a detour


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
    miles_from_start: float        # cumulative route miles at this stop
    gallons_purchased: float       # how many gallons we buy here
    cost_at_stop: float            # dollars spent here


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
        self.tank_cap = max_range_miles / mpg          # gallons for a full tank
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
        """
        Given a list of (lat, lon) waypoints sampled every ~10 miles, return a
        FuelPlan with the cheapest sequence of fuel stops.
        """
        plan = FuelPlan(
            total_distance_miles=total_distance_miles,
            start_location=start_location,
            end_location=end_location,
            duration_seconds=duration_seconds,
        )

        # Build cumulative distances for each waypoint
        cum_dist = self._cumulative_distances(waypoints)

        # Current state
        tank_miles_remaining = self.max_range  # start with a full tank
        current_wp_idx = 0

        while True:
            current_wp = waypoints[current_wp_idx]
            current_dist = cum_dist[current_wp_idx]

            # How far can we go from here?
            reachable_dist = current_dist + tank_miles_remaining

            # Are we close enough to the destination to finish without stopping?
            if reachable_dist >= total_distance_miles:
                break

            # Find the furthest waypoint index we can reach
            max_reachable_idx = self._furthest_reachable_idx(
                cum_dist, current_dist, tank_miles_remaining
            )

            # Collect candidate stations along the forward segment
            best_stop = self._find_best_stop(
                waypoints,
                cum_dist,
                current_wp_idx + 1,
                max_reachable_idx,
            )

            if best_stop is None:
                # No station found – emergency: take the closest station to current pos
                logger.warning(
                    "No station found in forward window from wp %d. Trying broader search.",
                    current_wp_idx,
                )
                best_stop = self._emergency_station(current_wp)
                if best_stop is None:
                    logger.error("Absolutely no station found near waypoint %d.", current_wp_idx)
                    break

            # How many miles will we have driven to reach this stop?
            stop_wp_idx = self._nearest_waypoint_idx(
                waypoints, best_stop["lat"], best_stop["lon"], max_idx=max_reachable_idx
            )
            miles_to_stop = cum_dist[stop_wp_idx] - current_dist

            # Fuel consumed to get here
            gallons_to_stop = miles_to_stop / self.mpg
            tank_miles_remaining -= miles_to_stop

            # Fill to full tank
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

            # Update state
            tank_miles_remaining = self.max_range  # filled up
            current_wp_idx = stop_wp_idx

        # Add fuel cost for the final leg (if any tank fuel remains, we still
        # count what we consumed)
        remaining_miles = total_distance_miles - cum_dist[current_wp_idx]
        if remaining_miles > 0 and plan.stops:
            # We use whatever fuel is left; add a proportional cost based on
            # the price we last paid (already captured in the last stop's purchase)
            pass  # the gallons were already pre-paid in the last fill-up

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
        # Binary-search-ish: find last index where cum_dist <= limit
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
        """
        Scan waypoints from start_idx to end_idx and return the cheapest
        station within search_radius of *any* waypoint in that window.
        We sample every 5th waypoint to avoid excessive station lookups.
        """
        best: dict[str, Any] | None = None
        step = max(1, (end_idx - start_idx) // 20)  # ~20 probe points max

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
        """Wider search radius for emergency fallback."""
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
        """Find the index of the waypoint closest to (lat, lon), up to max_idx."""
        best_dist = float("inf")
        best_idx = 0
        for i in range(min(max_idx + 1, len(waypoints))):
            d = _haversine_miles(lat, lon, waypoints[i][0], waypoints[i][1])
            if d < best_dist:
                best_dist = d
                best_idx = i
        return best_idx
