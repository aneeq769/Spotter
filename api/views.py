"""
views.py
--------
Single endpoint: POST /api/route/

Accepts start & end location strings, returns:
  * Route map (GeoJSON FeatureCollection)
  * Optimal fuel stops along the route
  * Total fuel cost
"""

import logging
import time

from django.conf import settings
from drf_spectacular.utils import extend_schema, OpenApiExample
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from api.serializers import RouteRequestSerializer, RouteResponseSerializer
from utils.fuel_optimizer import FuelOptimizer
from utils.fuel_station_service import get_fuel_station_service
from utils.routing_service import RoutingService, RoutingError

logger = logging.getLogger(__name__)

PLACEHOLDER_ORS_API_KEY = "your-openrouteservice-api-key-here"


class HealthCheckView(APIView):
    """Simple health check endpoint — useful for verifying the server is running."""

    def get(self, request: Request) -> Response:
        from utils.fuel_station_service import get_fuel_station_service
        svc = get_fuel_station_service()
        api_key = settings.ORS_API_KEY
        return Response({
            "status": "ok",
            "fuel_stations_loaded": len(svc.dataframe),
            "ors_api_key_configured": bool(api_key and api_key != PLACEHOLDER_ORS_API_KEY),
        })


def _build_geojson(
    route_geometry: dict,
    fuel_stops,
    origin_coords: tuple[float, float],
    dest_coords: tuple[float, float],
    origin_label: str,
    dest_label: str,
) -> dict:
    """
    Build a GeoJSON FeatureCollection containing:
      1. The driving route as a LineString Feature
      2. A Point Feature for each fuel stop
      3. Origin and destination markers
    """
    features = []

    # Route line
    features.append({
        "type": "Feature",
        "geometry": route_geometry,
        "properties": {
            "type": "route",
            "description": f"{origin_label} → {dest_label}",
        },
    })

    # Origin marker
    features.append({
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [origin_coords[1], origin_coords[0]],  # GeoJSON: lon, lat
        },
        "properties": {
            "type": "origin",
            "label": origin_label,
            "marker_color": "#22c55e",
        },
    })

    # Destination marker
    features.append({
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [dest_coords[1], dest_coords[0]],
        },
        "properties": {
            "type": "destination",
            "label": dest_label,
            "marker_color": "#ef4444",
        },
    })

    # Fuel stop markers
    for i, stop in enumerate(fuel_stops, start=1):
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [stop.lon, stop.lat],
            },
            "properties": {
                "type": "fuel_stop",
                "stop_number": i,
                "name": stop.name,
                "address": f"{stop.address}, {stop.city}, {stop.state}",
                "price_per_gallon": stop.price_per_gallon,
                "gallons_purchased": stop.gallons_purchased,
                "cost_at_stop": stop.cost_at_stop,
                "miles_from_start": stop.miles_from_start,
                "marker_color": "#f97316",
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
    }


class RouteView(APIView):
    """
    Calculates an optimised fuel-stop plan for a US driving route.
    """

    @extend_schema(
        request=RouteRequestSerializer,
        responses={200: RouteResponseSerializer},
        summary="Plan optimal fuel stops for a US driving route",
        description=(
            "Provide start and end locations within the USA.  The API returns "
            "a GeoJSON map of the route, the optimal fuel stop locations "
            "(chosen to minimise cost), and the total money spent on fuel."
        ),
        examples=[
            OpenApiExample(
                "Los Angeles to New York",
                value={
                    "start": "Los Angeles, CA",
                    "end": "New York, NY",
                },
                request_only=True,
            ),
            OpenApiExample(
                "Chicago to Miami",
                value={
                    "start": "Chicago, IL",
                    "end": "Miami, FL",
                    "max_range_miles": 500,
                    "mpg": 10,
                },
                request_only=True,
            ),
        ],
    )
    def post(self, request: Request) -> Response:
        t_start = time.perf_counter()

        # ----------------------------------------------------------------
        # 1. Validate input
        # ----------------------------------------------------------------
        serializer = RouteRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        start = data["start"]
        end = data["end"]
        max_range = data["max_range_miles"]
        mpg = data["mpg"]

        logger.info("Route request: '%s' → '%s'  range=%d mpg=%.1f", start, end, max_range, mpg)

        # ----------------------------------------------------------------
        # 2. Fetch route from ORS (1–2 API calls)
        # ----------------------------------------------------------------
        api_key = settings.ORS_API_KEY
        if not api_key or api_key == PLACEHOLDER_ORS_API_KEY:
            return Response(
                {
                    "error": "ORS_API_KEY is not configured. "
                             "Please set it in your .env file. "
                             "Get a free key at https://openrouteservice.org/dev/#/signup"
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        routing = RoutingService(api_key)
        try:
            route = routing.get_route(start, end)
        except RoutingError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # ----------------------------------------------------------------
        # 3. Plan fuel stops (in-memory, no extra API calls)
        # ----------------------------------------------------------------
        station_service = get_fuel_station_service()
        optimizer = FuelOptimizer(
            station_service=station_service,
            max_range_miles=max_range,
            mpg=mpg,
        )

        fuel_plan = optimizer.plan(
            waypoints=route["waypoints"],
            total_distance_miles=route["distance_miles"],
            duration_seconds=route["duration_seconds"],
            start_location=start,
            end_location=end,
            origin_coords=route["origin_coords"],
            destination_coords=route["destination_coords"],
        )

        # ----------------------------------------------------------------
        # 4. Build GeoJSON map
        # ----------------------------------------------------------------
        geojson_map = _build_geojson(
            route_geometry=route["geojson_geometry"],
            fuel_stops=fuel_plan.stops,
            origin_coords=route["origin_coords"],
            dest_coords=route["destination_coords"],
            origin_label=start,
            dest_label=end,
        )

        # ----------------------------------------------------------------
        # 5. Compute summary figures
        # ----------------------------------------------------------------
        avg_price = (
            fuel_plan.total_fuel_cost / fuel_plan.total_gallons
            if fuel_plan.total_gallons > 0
            else 0.0
        )

        elapsed = time.perf_counter() - t_start
        logger.info(
            "Route planned in %.2fs | %.0f mi | %d stops | $%.2f total fuel | %d ORS calls",
            elapsed,
            fuel_plan.total_distance_miles,
            len(fuel_plan.stops),
            fuel_plan.total_fuel_cost,
            route["api_calls_made"],
        )

        # ----------------------------------------------------------------
        # 6. Serialize and return
        # ----------------------------------------------------------------
        response_data = {
            "start": start,
            "end": end,
            "start_coords": list(route["origin_coords"]),
            "end_coords": list(route["destination_coords"]),
            "total_distance_miles": round(route["distance_miles"], 2),
            "estimated_duration_hours": round(route["duration_seconds"] / 3600, 2),
            "total_gallons_needed": fuel_plan.total_gallons,
            "total_fuel_cost_usd": fuel_plan.total_fuel_cost,
            "average_price_per_gallon": round(avg_price, 4),
            "fuel_stops": [
                {
                    "station_id": s.station_id,
                    "name": s.name,
                    "address": s.address,
                    "city": s.city,
                    "state": s.state,
                    "price_per_gallon": s.price_per_gallon,
                    "lat": s.lat,
                    "lon": s.lon,
                    "miles_from_start": round(s.miles_from_start, 2),
                    "gallons_purchased": s.gallons_purchased,
                    "cost_at_stop": s.cost_at_stop,
                }
                for s in fuel_plan.stops
            ],
            "route_map": geojson_map,
            "api_calls_to_ors": route["api_calls_made"],
        }

        return Response(response_data, status=status.HTTP_200_OK)
