"""
fuel_station_service.py
-----------------------
Loads the CSV of US truckstop fuel prices once at startup (into a module-level
singleton) and provides fast nearest-station lookups using a KD-tree.

Key design choices
------------------
* The CSV has duplicate OPIS IDs with different prices (same physical station,
  different pump/grade entries).  We keep only the *cheapest* price per
  station so downstream logic always works with one record per location.
* We geocode **by state centroid** (not individual addresses) so we never need
  an external geocoding API for the station list.  Accuracy is good enough for
  "stations within 50 miles of a route waypoint" queries.
* The KD-tree is built once and reused for every request.
"""

import logging
import math
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.spatial import KDTree  # type: ignore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State centroid coordinates  (lat, lon)
# ---------------------------------------------------------------------------
STATE_CENTROIDS: dict[str, tuple[float, float]] = {
    "AL": (32.806671, -86.791130),
    "AK": (61.370716, -152.404419),
    "AZ": (33.729759, -111.431221),
    "AR": (34.969704, -92.373123),
    "CA": (36.116203, -119.681564),
    "CO": (39.059811, -105.311104),
    "CT": (41.597782, -72.755371),
    "DE": (39.318523, -75.507141),
    "FL": (27.766279, -81.686783),
    "GA": (33.040619, -83.643074),
    "HI": (21.094318, -157.498337),
    "ID": (44.240459, -114.478828),
    "IL": (40.349457, -88.986137),
    "IN": (39.849426, -86.258278),
    "IA": (42.011539, -93.210526),
    "KS": (38.526600, -96.726486),
    "KY": (37.668140, -84.670067),
    "LA": (31.169960, -91.867805),
    "ME": (44.693947, -69.381927),
    "MD": (39.063946, -76.802101),
    "MA": (42.230171, -71.530106),
    "MI": (43.326618, -84.536095),
    "MN": (45.694454, -93.900192),
    "MS": (32.741646, -89.678696),
    "MO": (38.456085, -92.288368),
    "MT": (46.921925, -110.454353),
    "NE": (41.125370, -98.268082),
    "NV": (38.313515, -117.055374),
    "NH": (43.452492, -71.563896),
    "NJ": (40.298904, -74.521011),
    "NM": (34.840515, -106.248482),
    "NY": (42.165726, -74.948051),
    "NC": (35.630066, -79.806419),
    "ND": (47.528912, -99.784012),
    "OH": (40.388783, -82.764915),
    "OK": (35.565342, -96.928917),
    "OR": (44.572021, -122.070938),
    "PA": (40.590752, -77.209755),
    "RI": (41.680893, -71.511780),
    "SC": (33.856892, -80.945007),
    "SD": (44.299782, -99.438828),
    "TN": (35.747845, -86.692345),
    "TX": (31.054487, -97.563461),
    "UT": (40.150032, -111.862434),
    "VT": (44.045876, -72.710686),
    "VA": (37.769337, -78.169968),
    "WA": (47.400902, -121.490494),
    "WV": (38.491226, -80.954453),
    "WI": (44.268543, -89.616508),
    "WY": (42.755966, -107.302490),
    "DC": (38.897438, -77.026817),
}

# Degrees to radians conversion factor
_DEG2RAD = math.pi / 180.0
# Earth radius in miles
_EARTH_RADIUS_MILES = 3958.8


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in miles between two lat/lon points."""
    dlat = (lat2 - lat1) * _DEG2RAD
    dlon = (lon2 - lon1) * _DEG2RAD
    a = math.sin(dlat / 2) ** 2 + (
        math.cos(lat1 * _DEG2RAD) * math.cos(lat2 * _DEG2RAD) * math.sin(dlon / 2) ** 2
    )
    return _EARTH_RADIUS_MILES * 2 * math.asin(math.sqrt(a))


class FuelStationService:
    """
    Singleton service that holds all station data and a KD-tree for fast
    geographic lookups.
    """

    def __init__(self, csv_path: str | Path) -> None:
        self._df = self._load_and_clean(csv_path)
        self._tree, self._coords_rad = self._build_kdtree()
        logger.info("FuelStationService ready: %d stations loaded.", len(self._df))

    # ------------------------------------------------------------------
    # Initialisation helpers
    # ------------------------------------------------------------------

    def _load_and_clean(self, csv_path: str | Path) -> pd.DataFrame:
        """
        Read the CSV, assign coordinates from state centroids, and keep only
        the cheapest price per physical station address.
        """
        df = pd.read_csv(csv_path)

        # Normalise column names
        df.columns = [c.strip() for c in df.columns]
        df.rename(
            columns={
                "OPIS Truckstop ID": "station_id",
                "Truckstop Name": "name",
                "Address": "address",
                "City": "city",
                "State": "state",
                "Rack ID": "rack_id",
                "Retail Price": "price",
            },
            inplace=True,
        )

        # Strip whitespace from string columns
        for col in ("name", "city", "state", "address"):
            df[col] = df[col].astype(str).str.strip()

        # Drop rows with missing price or unknown state
        df = df.dropna(subset=["price", "state"])
        df = df[df["state"].isin(STATE_CENTROIDS)]

        # Assign lat/lon from state centroid
        df["lat"] = df["state"].map(lambda s: STATE_CENTROIDS[s][0])
        df["lon"] = df["state"].map(lambda s: STATE_CENTROIDS[s][1])

        # Keep cheapest price per (station_id, address) combo
        df = (
            df.sort_values("price")
            .drop_duplicates(subset=["station_id", "address"])
            .reset_index(drop=True)
        )

        return df

    def _build_kdtree(self) -> tuple[KDTree, np.ndarray]:
        """
        Build a KD-tree on unit-sphere Cartesian coords so that Euclidean
        distance in the tree approximates great-circle distance.
        """
        lats = np.radians(self._df["lat"].values)
        lons = np.radians(self._df["lon"].values)
        x = np.cos(lats) * np.cos(lons)
        y = np.cos(lats) * np.sin(lons)
        z = np.sin(lats)
        coords = np.column_stack([x, y, z])
        return KDTree(coords), coords

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stations_near_point(
        self,
        lat: float,
        lon: float,
        radius_miles: float = 50,
        max_results: int = 20,
    ) -> pd.DataFrame:
        """
        Return up to *max_results* stations within *radius_miles* of (lat, lon),
        sorted by price ascending.
        """
        # Convert radius to unit-sphere chord length
        chord = 2 * math.sin(math.radians(radius_miles / _EARTH_RADIUS_MILES * 180 / math.pi) / 2)

        qlat, qlon = math.radians(lat), math.radians(lon)
        qx = math.cos(qlat) * math.cos(qlon)
        qy = math.cos(qlat) * math.sin(qlon)
        qz = math.sin(qlat)

        idxs = self._tree.query_ball_point([qx, qy, qz], chord)

        if not idxs:
            return pd.DataFrame()

        subset = self._df.iloc[idxs].copy()
        subset["distance_miles"] = subset.apply(
            lambda r: _haversine_miles(lat, lon, r["lat"], r["lon"]), axis=1
        )
        return (
            subset.sort_values("price")
            .head(max_results)
            .reset_index(drop=True)
        )

    def cheapest_station_near_point(
        self,
        lat: float,
        lon: float,
        radius_miles: float = 50,
    ) -> dict | None:
        """
        Return the single cheapest station within *radius_miles* of (lat, lon),
        or None if no station is found.
        """
        nearby = self.stations_near_point(lat, lon, radius_miles=radius_miles, max_results=1)
        if nearby.empty:
            return None
        row = nearby.iloc[0]
        return {
            "station_id": int(row["station_id"]),
            "name": row["name"],
            "address": row["address"],
            "city": row["city"],
            "state": row["state"],
            "price_per_gallon": round(float(row["price"]), 4),
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
        }

    @property
    def dataframe(self) -> pd.DataFrame:
        return self._df


# ---------------------------------------------------------------------------
# Module-level singleton – loaded once when Django starts
# ---------------------------------------------------------------------------
_service_instance: FuelStationService | None = None


def get_fuel_station_service() -> FuelStationService:
    """Return (and lazily create) the module-level FuelStationService singleton."""
    global _service_instance
    if _service_instance is None:
        from django.conf import settings  # imported lazily to avoid circular imports

        _service_instance = FuelStationService(settings.FUEL_PRICES_CSV)
    return _service_instance
