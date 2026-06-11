# Route Detection API

Django REST-style API for the backend assessment. The API accepts two USA
location names, calculates a driving route, selects cost-effective fuel stops
from the provided CSV data, and returns route geometry plus fuel cost details.

## Setup

```bash
./venv/bin/python manage.py migrate
./venv/bin/python manage.py import_fuel_stations --replace
./venv/bin/python manage.py geocode_fuel_stations
./venv/bin/python manage.py runserver
```

The CSV file is not edited. It is imported once into the `FuelStation` table.
Latitude and longitude are stored in the database after the one-time station
geocoding step.

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

Fuel stations can be inspected with the DRF `ListAPIView` endpoint:

```http
GET /api/fuel-stations/?state=OH&geocoded=true
```

## How It Works

1. `FuelStation` rows are imported from `fuel-prices-for-be-assessment.csv`.
2. Station coordinates are geocoded once and saved in the database.
3. Start and finish locations are geocoded with Nominatim.
4. Start/finish geocoding results are saved in `GeocodeCache`.
5. OSRM calculates the route from the cached coordinates.
6. OSRM route geometry, distance, and duration are saved in `RouteCache`.
7. The route is sampled and matched against geocoded CSV fuel stations.
8. The service searches a route corridor using fallback radii: 10, 25, 50 miles.
9. The route is divided into 500-mile windows.
10. Each window selects the station with the lowest effective cost:

```text
effective cost =
  route window gallons * station price
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
- gallons needed per 500-mile window
- fuel cost per stop
- estimated detour cost
- total fuel cost

## External APIs

- Nominatim/OpenStreetMap: start and finish geocoding, cached in DB
- OSRM: routing, cached in DB

The API does not read the CSV on each request and does not geocode fuel stations
during route requests.

## Tests

```bash
./venv/bin/python manage.py test
```
