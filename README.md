# Fleet Fuel Optimizer API

This is a Django REST API for planning fuel stops on a US driving route. It
takes a start and finish location, gets the driving route, checks the provided
fuel-price dataset, and returns the route geometry with cost-effective fuel
stops for a truck that gets 10 MPG and can travel up to 500 miles on a tank.

The project uses these services for geocoding and routing:

- Geocodio for station geocoding when API keys are configured
- Nominatim/OpenStreetMap for route endpoint location lookup and station fallback geocoding
- OSRM for driving route geometry, distance, and duration

## Assignment Coverage

The API covers the main requirements from the backend exercise:

- accepts start and finish locations inside the USA
- returns route data that can be drawn on a map as GeoJSON
- selects fuel stops from the supplied CSV fuel-price data
- assumes 500 miles of range and 10 miles per gallon
- calculates the estimated money spent on fuel
- avoids repeated external calls by caching geocoding and routing results
- imports and geocodes station data before serving route requests

## Setup

Create a virtual environment and install dependencies if needed:

```bash
python -m venv venv
./venv/bin/pip install -r requirements.txt
```

Create the local environment file:

```bash
cp .env.example .env
```

Set `DJANGO_SECRET_KEY` in `.env`. The local `.env` file is intentionally
ignored by git.

Apply migrations:

```bash
./venv/bin/python manage.py migrate
```

Import the fuel station CSV:

```bash
./venv/bin/python manage.py import_fuel_stations
```

By default, the import command expects this file in the project root:

```text
fuel-prices-for-be-assessment.csv
```

You can also pass a custom path:

```bash
./venv/bin/python manage.py import_fuel_stations --csv /path/to/fuel-prices-for-be-assessment.csv
```

Run the API:

```bash
./venv/bin/python manage.py runserver
```

## Environment Variables

```env
DJANGO_SECRET_KEY=replace-with-a-local-secret-key
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,testserver
GEOCODIO_API_KEYS=replace-with-geocodio-key-one,replace-with-geocodio-key-two
```

`GEOCODIO_API_KEYS` is a comma-separated list. The import command rotates
through the keys for fuel station geocoding. If it is empty, station geocoding
falls back to Nominatim only. Nominatim and OSRM do not require keys in this
project.

## API

```http
POST /api/routes/
Content-Type: application/json

{
  "start_location": "New York, NY",
  "finish_location": "Chicago, IL"
}
```

Successful responses use the shared response envelope:

```json
{
  "success": true,
  "status_code": 200,
  "message": "Route plan created successfully",
  "data": {
    "start_location": {},
    "finish_location": {},
    "route": {},
    "fuel_plan": {}
  }
}
```

The response data includes:

- normalized start and finish coordinates
- total route distance in miles
- estimated route duration in minutes
- GeoJSON `LineString` route geometry
- selected fuel stops with station name, address, price, and coordinates
- gallons needed for each next leg
- fuel cost at each stop
- detour distance and detour fuel cost estimate
- total fuel consumed for the route
- total fuel cost after the initial full tank

## How It Works

Fuel stations are imported from the CSV into the `FuelStation` table. The import
command upserts by `opis_truckstop_id`, so it can be run again without creating
duplicate stations.

Station coordinates are geocoded during import and stored in the database. Route
requests do not read the CSV and do not geocode stations.

For a route request, the API:

1. normalizes the two location strings
2. geocodes start and finish locations with Nominatim
3. stores geocoding results in `GeocodeCache`
4. requests one full route from OSRM
5. stores route geometry, distance, and duration in `RouteCache`
6. samples the route and finds nearby geocoded fuel stations
7. tries route corridor radii of 10, 25, and 50 miles
8. plans the cheapest feasible stop chain within the vehicle range

Routes up to the 480-mile safety range do not need an en-route fuel stop. Longer
routes start with the minimum required number of stops and retry with extra stops
if station placement makes the minimum chain infeasible.

The cost calculation uses this effective stop cost:

```text
next leg gallons * station price
+ (distance from route * 2 / 10 MPG) * station price
```

The vehicle starts with a full tank, so the initial tank is not counted as an
en-route purchase.

## Notes

- `GEOCODIO_API_KEYS` is loaded from `.env` and used by the fuel station import command.
- `ROUTE_PLANNER_USER_AGENT` is set in Django settings for Nominatim requests.
- Nominatim station fallback geocoding uses a one-second delay between requests.
- OSRM is called with `overview=full` so route matching uses the full geometry.
- Cached route/geocode rows stay active through the shared timestamp mixin.

## Tests

```bash
./venv/bin/python manage.py test
```
