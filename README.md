# Route Detection API

Django API for the backend route/fuel assessment. It accepts a start and finish
location in the USA, fetches one driving route from OSRM, finds fuel stations
near the route from the provided CSV, and returns a GeoJSON map plus an
optimized fuel plan.

## Run

```bash
./venv/bin/python manage.py runserver
```

## Request

```bash
curl -X POST http://127.0.0.1:8000/api/routes/ \
  -H "Content-Type: application/json" \
  -d '{"start":"New York, NY","finish":"Chicago, IL"}'
```

`GET /api/routes/?start=New%20York,%20NY&finish=Chicago,%20IL` is also
supported for quick demos.

## Response Highlights

- `route.geometry`: GeoJSON `LineString` suitable for rendering on a map.
- `map`: GeoJSON `FeatureCollection` with route, start, finish, and fuel-stop
  markers.
- `fuel.stops`: selected fuel stops from `fuel-prices-for-be-assessment.csv`.
- `fuel.purchases`: gallons and cost for each fuel leg.
- `fuel.total_cost_usd`: total fuel spend at 10 MPG.

## Providers

- Geocoding: OpenStreetMap Nominatim, two calls per uncached request.
- Routing: OSRM public demo server, one call per uncached request.
- Fuel prices: the provided CSV.
- Station coordinates: local GeoNames city/state centroids generated once and
  stored in `route_planner/data/us_city_centroids.csv`, so station geocoding is
  not performed during API requests.

## Tests

```bash
./venv/bin/python manage.py test
```
