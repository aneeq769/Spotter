"""
Serializers for request validation and response shaping.
"""

from rest_framework import serializers


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

class RouteRequestSerializer(serializers.Serializer):
    """Validates the incoming route request body."""

    start = serializers.CharField(
        max_length=200,
        help_text="Starting location within the USA (e.g. 'Los Angeles, CA' or '350 5th Ave, New York, NY').",
    )
    end = serializers.CharField(
        max_length=200,
        help_text="Destination location within the USA.",
    )
    max_range_miles = serializers.IntegerField(
        required=False,
        default=500,
        min_value=50,
        max_value=1000,
        help_text="Vehicle maximum range on a full tank in miles (default 500).",
    )
    mpg = serializers.FloatField(
        required=False,
        default=10.0,
        min_value=1.0,
        max_value=200.0,
        help_text="Vehicle fuel efficiency in miles per gallon (default 10).",
    )


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------

class FuelStopSerializer(serializers.Serializer):
    station_id = serializers.IntegerField()
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    price_per_gallon = serializers.FloatField()
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    miles_from_start = serializers.FloatField()
    gallons_purchased = serializers.FloatField()
    cost_at_stop = serializers.FloatField()


class RouteResponseSerializer(serializers.Serializer):
    start = serializers.CharField()
    end = serializers.CharField()
    start_coords = serializers.ListField(child=serializers.FloatField())
    end_coords = serializers.ListField(child=serializers.FloatField())
    total_distance_miles = serializers.FloatField()
    estimated_duration_hours = serializers.FloatField()
    total_gallons_needed = serializers.FloatField()
    total_fuel_cost_usd = serializers.FloatField()
    average_price_per_gallon = serializers.FloatField()
    fuel_stops = FuelStopSerializer(many=True)
    route_map = serializers.DictField(
        help_text="GeoJSON FeatureCollection containing the route and fuel stop markers."
    )
    api_calls_to_mapbox = serializers.IntegerField()