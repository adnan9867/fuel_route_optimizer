# Fleet Fuel Optimizer API

Django REST-style API for the backend assessment. The API accepts two USA
location names, calculates a driving route, selects cost-effective fuel stops
from the provided CSV data, and returns route geometry plus fuel cost details.

## Setup

```bash
./venv/bin/python manage.py migrate
./venv/bin/python manage.py import_fuel_stations
./venv/bin/python manage.py geocode_fuel_stations
./venv/bin/python manage.py runserver
```

The CSV file is not edited. It is imported once into the `FuelStation` table.
The import command upserts by `opis_truckstop_id`, so running it again updates
existing rows instead of creating duplicates.

Latitude and longitude are stored in the database after the one-time station
geocoding step. `geocode_fuel_stations` uses Nominatim address geocoding by
default for better station matching. For a quick local demo, this faster fallback
is available:

```bash
./venv/bin/python manage.py geocode_fuel_stations --provider city-centroid
```

Route app models inherit the shared `common.model_mixins.TimestampMixin`, so
they consistently include `created_at`, `updated_at`, and `is_active`.

## API

```http
POST /api/routes/
Content-Type: application/json

{
  "start_location": "New York, NY",
  "finish_location": "Chicago, IL"
}
```

The route endpoint is implemented with DRF `CreateAPIView`.

## How It Works

1. `FuelStation` rows are imported from `fuel-prices-for-be-assessment.csv`.
2. Station coordinates are geocoded once and saved in the database.
3. Start and finish locations are geocoded with Nominatim.
4. Start/finish geocoding results are saved in `GeocodeCache` using a
   case-insensitive normalized key.
5. OSRM calculates the route from the cached coordinates.
6. OSRM route geometry, distance, and duration are saved in `RouteCache`.
7. The route is sampled and matched against geocoded CSV fuel stations.
8. The service searches a route corridor using fallback radii: 10, 25, 50 miles.
9. OSRM is requested with `overview=full` so route-to-station matching uses the
   full route geometry.
10. The vehicle is assumed to start with a full tank.
11. The vehicle maximum range is 500 miles, but stop planning uses a 480-mile
   safety range.
12. The planner tries the minimum required number of stops first. If station
   placement makes that infeasible, it retries with extra stops.
13. Each selected stop pays for the next leg, not the miles already driven:

```text
effective cost =
  next leg gallons * station price
  + (distance from route * 2 / 10 MPG) * station price
```

## Response Includes

Responses use the shared `BaseAPIView` envelope:

```json
{
  "success": true,
  "status_code": 200,
  "message": "Route plan created successfully",
  "data": {}
}
```

- start and finish coordinates
- total route distance
- estimated route duration
- GeoJSON route geometry
- selected fuel stops
- price per gallon
- gallons needed for each next leg
- fuel cost per stop
- estimated detour cost
- total fuel cost
- en-route fuel purchase cost
- selected stop purchase cost
- total route gallons consumed
- total gallons purchased after the initial full tank

## External APIs

- Nominatim/OpenStreetMap: start and finish geocoding, cached in DB
- OSRM: routing, cached in DB

The API does not read the CSV on each request and does not geocode fuel stations
during route requests.

## Assumptions

- Start and finish locations are inside the USA.
- The vehicle starts with a full tank.
- The initial full tank is not counted as an en-route purchase.
- `total_route_gallons` shows total fuel consumed for the whole route.
- `en_route_fuel_cost_usd` and `selected_stop_purchase_cost_usd` show fuel bought
  after the initial full tank.
- Routes up to 480 miles require no fuel stop.
- A 760 or 900 mile route generally requires one fuel stop.
- A 1200 mile route generally requires two fuel stops.

## Tests

```bash
./venv/bin/python manage.py test
```
