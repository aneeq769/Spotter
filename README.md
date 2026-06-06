# Fuel Route Optimizer API

A Django REST API that calculates the **cheapest fuel stop plan** for any driving trip within the USA.

## What It Does

Given a start and end location, the API:
1. Geocodes both locations and fetches the driving route (via **OpenRouteService** — free)
2. Plans the optimal sequence of fuel stops using real truckstop price data (8,000+ stations)
3. Returns a **GeoJSON map** of the route + fuel stop markers, plus the **total fuel cost**

---

## Tech Stack

| Concern | Choice | Reason |
|---|---|---|
| Framework | Django 5.0 + DRF | Requirement |
| Routing / Map API | [OpenRouteService](https://openrouteservice.org/) (free) | Free tier 2 000 req/day; single call gives route + distance |
| Fuel data | Bundled CSV (8 000+ US truckstops) | No extra API call needed |
| Spatial index | `scipy.KDTree` | O(log n) nearest-station lookup, runs in memory |
| Docs | drf-spectacular (Swagger + ReDoc) | Auto-generated from code |

---

## Quick Start

### 1. Clone and set up environment

```bash
git clone <your-repo-url>
cd fuel_route_api

python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
# Edit .env and fill in:
#   SECRET_KEY  – any random string
#                 generate: python -c "import secrets; print(secrets.token_hex(32))"
#   ORS_API_KEY – free key from https://openrouteservice.org/dev/#/signup
```

### 3. Run database migrations and start server

```bash
python manage.py migrate
python manage.py runserver
```

The API is now live at `http://127.0.0.1:8000/`

---

## API Endpoints

### `GET /api/health/`

Verify the server is running and data is loaded.

```json
{
  "status": "ok",
  "fuel_stations_loaded": 6738,
  "ors_api_key_configured": true
}
```

---

### `POST /api/route/`

**Request body** (JSON):

```json
{
  "start": "Los Angeles, CA",
  "end": "New York, NY",
  "max_range_miles": 500,
  "mpg": 10
}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `start` | string | ✅ | — | US start location |
| `end` | string | ✅ | — | US end location |
| `max_range_miles` | int | ❌ | 500 | Vehicle range on a full tank |
| `mpg` | float | ❌ | 10.0 | Fuel efficiency in miles per gallon |

**Response** (abbreviated):

```json
{
  "start": "Los Angeles, CA",
  "end": "New York, NY",
  "start_coords": [34.0522, -118.2437],
  "end_coords": [40.7128, -74.006],
  "total_distance_miles": 2789.4,
  "estimated_duration_hours": 39.8,
  "total_gallons_needed": 278.94,
  "total_fuel_cost_usd": 871.23,
  "average_price_per_gallon": 3.123,
  "api_calls_to_ors": 1,
  "fuel_stops": [
    {
      "station_id": 421,
      "name": "PETRO STOPPING CENTER #316",
      "address": "I-40 & I-35, EXIT 127",
      "city": "Oklahoma City",
      "state": "OK",
      "price_per_gallon": 3.302,
      "lat": 35.57,
      "lon": -96.93,
      "miles_from_start": 487.3,
      "gallons_purchased": 50.0,
      "cost_at_stop": 165.1
    }
  ],
  "route_map": {
    "type": "FeatureCollection",
    "features": [...]
  }
}
```

### Interactive API Docs

| URL | UI |
|---|---|
| `http://localhost:8000/api/docs/` | Swagger UI |
| `http://localhost:8000/api/redoc/` | ReDoc |
| `http://localhost:8000/api/schema/` | Raw OpenAPI JSON |

---

## Postman Demo (for Loom recording)

A ready-made Postman collection is included: **`FuelRouteOptimizer.postman_collection.json`**

### Import into Postman

1. Open Postman → **Import** → drag in `FuelRouteOptimizer.postman_collection.json`
2. Set the `base_url` collection variable to `http://localhost:8000`
3. Make sure `python manage.py runserver` is running

### Recommended demo order

| Step | Request | What to show |
|---|---|---|
| 1 | `GET /api/health/` | Server is up, 6 738 stations loaded |
| 2 | `POST /api/route/` LA → NYC | Full response: stops, cost, GeoJSON |
| 3 | Copy `route_map` JSON → paste into [geojson.io](https://geojson.io) | Visual map in browser |
| 4 | `POST /api/route/` LA → Las Vegas | Short trip, zero stops |
| 5 | `POST /api/route/` missing `start` | 400 validation error |
| 6 | Show Swagger UI at `/api/docs/` | Self-documenting API |

### Visualising the map

Copy the `route_map` value from any response and paste it into **[geojson.io](https://geojson.io)** — the route line and all fuel stop pins will appear on the map instantly.

---

## Running Tests

```bash
python manage.py test
```

12 tests covering:
- CSV loading and deduplication
- KD-tree spatial queries
- Fuel optimizer algorithm (multi-stop, single-stop, no-stop trips)
- API endpoint validation (200, 400, 503 responses)

---

## How the Fuel Optimizer Works

1. The vehicle starts with a **full tank** (500 miles of range by default).
2. Route waypoints are sampled every ~10 miles.
3. A **greedy look-ahead** algorithm scans forward up to the max range:
   - Finds the **cheapest station** within 60 miles of any waypoint in the reachable window.
   - Drives to that station and **fills to full**.
4. Repeats until the destination is reachable on the current tank.
5. Total cost = Σ (gallons purchased × price at each stop).

---

## API Call Budget

The assessment requires ≤ 3 ORS API calls per request. Here's what happens:

| Attempt | Calls | When |
|---|---|---|
| Single-call (ideal) | **1** | ORS inline geocoding succeeds |
| Explicit geocode fallback | **3** | 2 geocode + 1 directions |

The `api_calls_to_ors` field in every response shows the actual count used.

---

## Project Structure

```
fuel_route_api/
├── .env.example                           ← copy to .env, fill in keys
├── .gitignore                             ← .env excluded
├── requirements.txt
├── manage.py
├── fuel_prices.csv                        ← bundled fuel price data
├── FuelRouteOptimizer.postman_collection.json  ← import into Postman
├── fuel_route/                            ← Django project
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── api/                                   ← Django app
│   ├── apps.py                            ← pre-loads stations at startup
│   ├── serializers.py
│   ├── views.py                           ← GET /api/health/ + POST /api/route/
│   ├── urls.py
│   └── tests.py                           ← 12 tests
└── utils/                                 ← pure business logic
    ├── fuel_station_service.py            ← CSV loader + KD-tree
    ├── fuel_optimizer.py                  ← greedy fuel-stop planner
    └── routing_service.py                 ← OpenRouteService wrapper
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | ✅ | Django secret key |
| `DEBUG` | ❌ | `True` / `False` (default `True`) |
| `ALLOWED_HOSTS` | ❌ | Comma-separated list |
| `ORS_API_KEY` | ✅ | OpenRouteService API key (free) |

**Never commit `.env` to git.** The `.gitignore` excludes it automatically.

---

## Getting a Free ORS API Key

1. Visit https://openrouteservice.org/dev/#/signup
2. Create a free account
3. Copy your API key from the dashboard
4. Add to `.env`: `ORS_API_KEY=<your-key>`

Free tier: 2 000 requests/day, 40 requests/minute.

---

## GitHub Setup

```bash
git init
git add .
git commit -m "Initial commit: Fuel Route Optimizer API"
git remote add origin https://github.com/<your-username>/fuel-route-api.git
git push -u origin main
```

The `.gitignore` ensures `.env` (containing your secret keys) is **never pushed**.
