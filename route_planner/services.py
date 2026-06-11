from __future__ import annotations

import csv
import hashlib
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from django.conf import settings
from django.core.cache import cache


EARTH_RADIUS_MILES = 3958.7613
MAX_RANGE_MILES = 500.0
MILES_PER_GALLON = 10.0
ROUTE_SAMPLE_INTERVAL_MILES = 5.0
STATION_GRID_DEGREES = 1.0
DEFAULT_CORRIDOR_RADII_MILES = (45.0, 75.0, 120.0, 180.0)
US_STATE_CODES = {
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "IA",
    "ID",
    "IL",
    "IN",
    "KS",
    "KY",
    "LA",
    "MA",
    "MD",
    "ME",
    "MI",
    "MN",
    "MO",
    "MS",
    "MT",
    "NC",
    "ND",
    "NE",
    "NH",
    "NJ",
    "NM",
    "NV",
    "NY",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VA",
    "VT",
    "WA",
    "WI",
    "WV",
    "WY",
    "DC",
}


class RoutePlannerError(Exception):
    status_code = 500


class BadRequest(RoutePlannerError):
    status_code = 400


class UpstreamServiceError(RoutePlannerError):
    status_code = 502


class PlanningError(RoutePlannerError):
    status_code = 422


@dataclass(frozen=True, slots=True)
class Coordinate:
    latitude: float
    longitude: float

    def as_geojson_position(self) -> list[float]:
        return [round(self.longitude, 6), round(self.latitude, 6)]


@dataclass(frozen=True, slots=True)
class Station:
    station_id: str
    name: str
    address: str
    city: str
    state: str
    rack_id: str
    price: float
    coordinate: Coordinate


@dataclass(frozen=True, slots=True)
class RouteSample:
    coordinate: Coordinate
    mile: float


@dataclass(slots=True)
class CandidateStation:
    station: Station
    route_mile: float
    distance_from_route_miles: float


@dataclass(frozen=True, slots=True)
class FuelNode:
    mile: float
    price: float
    station: Station | None
    label: str


def geocode_location(location: str) -> Coordinate:
    query = " ".join((location or "").split())
    if not query:
        raise BadRequest("Both start and finish locations are required.")

    cache_key = _cache_key("geocode", query.lower())
    cached = cache.get(cache_key)
    if cached:
        return Coordinate(**cached)

    params = parse.urlencode(
        {
            "q": query,
            "format": "jsonv2",
            "limit": "1",
            "countrycodes": "us",
        }
    )
    payload = _get_json(
        f"https://nominatim.openstreetmap.org/search?{params}",
        timeout=10,
    )
    if not payload:
        raise BadRequest(f"Could not geocode a USA location for: {query}")

    try:
        coordinate = Coordinate(
            latitude=float(payload[0]["lat"]),
            longitude=float(payload[0]["lon"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise UpstreamServiceError("Geocoder returned an unexpected response.") from exc

    cache.set(
        cache_key,
        {"latitude": coordinate.latitude, "longitude": coordinate.longitude},
        settings.ROUTE_PLANNER_GEOCODE_CACHE_TTL,
    )
    return coordinate


def fetch_route(start: Coordinate, finish: Coordinate) -> dict[str, Any]:
    start_pair = f"{start.longitude:.6f},{start.latitude:.6f}"
    finish_pair = f"{finish.longitude:.6f},{finish.latitude:.6f}"
    params = parse.urlencode(
        {
            "overview": "simplified",
            "geometries": "geojson",
            "alternatives": "false",
            "steps": "false",
        }
    )
    url = f"https://router.project-osrm.org/route/v1/driving/{start_pair};{finish_pair}?{params}"
    cache_key = _cache_key("route", url)
    cached = cache.get(cache_key)
    if cached:
        return cached

    payload = _get_json(url, timeout=25)
    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise UpstreamServiceError("Routing service could not calculate that route.")

    route = payload["routes"][0]
    try:
        geometry = route["geometry"]["coordinates"]
        coordinates = [
            Coordinate(latitude=float(lat), longitude=float(lon)) for lon, lat in geometry
        ]
        distance_miles = float(route["distance"]) / 1609.344
        duration_minutes = float(route["duration"]) / 60.0
    except (KeyError, TypeError, ValueError) as exc:
        raise UpstreamServiceError("Router returned an unexpected response.") from exc

    if len(coordinates) < 2 or distance_miles <= 0:
        raise UpstreamServiceError("Router returned an empty route.")

    normalized = {
        "distance_miles": distance_miles,
        "duration_minutes": duration_minutes,
        "coordinates": coordinates,
    }
    cache.set(cache_key, normalized, settings.ROUTE_PLANNER_ROUTE_CACHE_TTL)
    return normalized


def plan_route(start_text: str, finish_text: str) -> dict[str, Any]:
    started_at = time.perf_counter()
    start = geocode_location(start_text)
    finish = geocode_location(finish_text)
    route = fetch_route(start, finish)

    distance_miles = route["distance_miles"]
    samples = sample_route(route["coordinates"], distance_miles)
    catalog = StationCatalog.instance()
    plan: dict[str, Any] | None = None
    selected_radius = None
    selected_candidates: list[CandidateStation] = []

    for radius in DEFAULT_CORRIDOR_RADII_MILES:
        candidates = catalog.candidates_near_route(samples, distance_miles, radius)
        if distance_miles <= MAX_RANGE_MILES or candidates:
            try:
                plan = build_fuel_plan(distance_miles, candidates)
            except PlanningError:
                continue
            selected_radius = radius
            selected_candidates = candidates
            break

    if plan is None:
        raise PlanningError(
            "No feasible fuel plan was found with the available station data."
        )

    route_coordinates = [point.as_geojson_position() for point in route["coordinates"]]
    response = {
        "start": {
            "input": start_text,
            "coordinates": start.as_geojson_position(),
        },
        "finish": {
            "input": finish_text,
            "coordinates": finish.as_geojson_position(),
        },
        "route": {
            "distance_miles": round(distance_miles, 2),
            "duration_minutes": round(route["duration_minutes"], 1),
            "geometry": {
                "type": "LineString",
                "coordinates": route_coordinates,
            },
            "bounds": route_bounds(route["coordinates"]),
        },
        "fuel": plan,
        "station_search": {
            "corridor_radius_miles": selected_radius,
            "candidate_count": len(selected_candidates),
            "catalog_station_count": catalog.station_count,
        },
        "map": build_feature_collection(
            route_coordinates=route_coordinates,
            start=start,
            finish=finish,
            stops=plan["stops"],
        ),
        "providers": {
            "geocoding": "OpenStreetMap Nominatim",
            "routing": "OSRM public demo server",
            "fuel_prices": "fuel-prices-for-be-assessment.csv",
            "station_coordinates": "GeoNames city centroids generated locally",
        },
        "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
    }
    return response


class StationCatalog:
    _instance: "StationCatalog | None" = None

    def __init__(self, stations: list[Station]) -> None:
        self.stations = stations
        self.station_count = len(stations)
        self._grid: dict[tuple[int, int], list[Station]] = {}
        for station in stations:
            key = self._grid_key(station.coordinate)
            self._grid.setdefault(key, []).append(station)

    @classmethod
    def instance(cls) -> "StationCatalog":
        if cls._instance is None:
            cls._instance = cls(load_stations())
        return cls._instance

    def candidates_near_route(
        self,
        samples: list[RouteSample],
        route_distance_miles: float,
        radius_miles: float,
    ) -> list[CandidateStation]:
        best_by_station: dict[str, CandidateStation] = {}

        for sample in samples:
            for station in self._stations_around(sample.coordinate, radius_miles):
                distance = haversine_miles(sample.coordinate, station.coordinate)
                if distance > radius_miles:
                    continue

                current = best_by_station.get(station.station_id)
                if current is None or distance < current.distance_from_route_miles:
                    best_by_station[station.station_id] = CandidateStation(
                        station=station,
                        route_mile=max(0.0, min(route_distance_miles, sample.mile)),
                        distance_from_route_miles=distance,
                    )

        candidates = [
            candidate
            for candidate in best_by_station.values()
            if 0.0 < candidate.route_mile < route_distance_miles
        ]
        return sorted(
            candidates,
            key=lambda item: (
                item.route_mile,
                item.station.price,
                item.distance_from_route_miles,
                item.station.station_id,
            ),
        )

    def _stations_around(
        self, coordinate: Coordinate, radius_miles: float
    ) -> list[Station]:
        lat_span = max(1, math.ceil(radius_miles / 69.0))
        lon_miles = max(20.0, 69.0 * math.cos(math.radians(coordinate.latitude)))
        lon_span = max(1, math.ceil(radius_miles / lon_miles))
        center_lat, center_lon = self._grid_key(coordinate)

        stations: list[Station] = []
        for lat_key in range(center_lat - lat_span, center_lat + lat_span + 1):
            for lon_key in range(center_lon - lon_span, center_lon + lon_span + 1):
                stations.extend(self._grid.get((lat_key, lon_key), ()))
        return stations

    @staticmethod
    def _grid_key(coordinate: Coordinate) -> tuple[int, int]:
        return (
            math.floor(coordinate.latitude / STATION_GRID_DEGREES),
            math.floor(coordinate.longitude / STATION_GRID_DEGREES),
        )


def build_fuel_plan(
    distance_miles: float,
    candidates: list[CandidateStation],
) -> dict[str, Any]:
    total_gallons = distance_miles / MILES_PER_GALLON
    initial_price = estimate_initial_price(candidates)

    if distance_miles <= MAX_RANGE_MILES and not candidates:
        cost = total_gallons * initial_price
        return {
            "total_cost_usd": round(cost, 2),
            "total_gallons": round(total_gallons, 2),
            "stops": [],
            "purchases": [
                {
                    "type": "initial",
                    "route_mile": 0.0,
                    "gallons": round(total_gallons, 2),
                    "unit_price_usd": round(initial_price, 3),
                    "cost_usd": round(cost, 2),
                    "note": "Initial fuel price estimated from the route corridor.",
                }
            ],
            "assumptions": fuel_assumptions(),
        }

    nodes = [FuelNode(0.0, initial_price, None, "Initial fuel")]
    nodes.extend(
        FuelNode(
            mile=candidate.route_mile,
            price=candidate.station.price,
            station=candidate.station,
            label=candidate.station.name,
        )
        for candidate in candidates
    )
    nodes.append(FuelNode(distance_miles, 0.0, None, "Destination"))

    best = [math.inf] * len(nodes)
    previous: list[int | None] = [None] * len(nodes)
    best[0] = 0.0

    for index, node in enumerate(nodes):
        if not math.isfinite(best[index]):
            continue
        for next_index in range(index + 1, len(nodes)):
            next_node = nodes[next_index]
            leg_miles = next_node.mile - node.mile
            if leg_miles <= 0:
                continue
            if leg_miles > MAX_RANGE_MILES:
                break
            cost = (leg_miles / MILES_PER_GALLON) * node.price
            candidate_cost = best[index] + cost
            if candidate_cost < best[next_index]:
                best[next_index] = candidate_cost
                previous[next_index] = index

    if not math.isfinite(best[-1]):
        raise PlanningError(
            "Route has a fuel gap longer than 500 miles in the available station data."
        )

    path_indices: list[int] = []
    cursor: int | None = len(nodes) - 1
    while cursor is not None:
        path_indices.append(cursor)
        cursor = previous[cursor]
    path_indices.reverse()

    purchases = []
    stops = []
    for position, node_index in enumerate(path_indices[:-1]):
        node = nodes[node_index]
        next_node = nodes[path_indices[position + 1]]
        leg_miles = next_node.mile - node.mile
        gallons = leg_miles / MILES_PER_GALLON
        cost = gallons * node.price
        purchase = {
            "type": "initial" if node.station is None else "fuel_stop",
            "name": node.label,
            "route_mile": round(node.mile, 1),
            "next_route_mile": round(next_node.mile, 1),
            "leg_miles": round(leg_miles, 1),
            "gallons": round(gallons, 2),
            "unit_price_usd": round(node.price, 3),
            "cost_usd": round(cost, 2),
        }
        if node.station is not None:
            purchase["station_id"] = node.station.station_id
            purchase["city"] = node.station.city
            purchase["state"] = node.station.state
        else:
            purchase["note"] = "Initial fuel price estimated from the route corridor."
        purchases.append(purchase)

    for stop_number, node_index in enumerate(path_indices[1:-1], start=1):
        node = nodes[node_index]
        if node.station is None:
            continue
        stops.append(serialize_stop(stop_number, node))

    return {
        "total_cost_usd": round(best[-1], 2),
        "total_gallons": round(total_gallons, 2),
        "stops": stops,
        "purchases": purchases,
        "assumptions": fuel_assumptions(),
    }


def estimate_initial_price(candidates: list[CandidateStation]) -> float:
    if not candidates:
        return 3.75

    early_candidates = [candidate for candidate in candidates if candidate.route_mile <= 50.0]
    if early_candidates:
        return min(candidate.station.price for candidate in early_candidates)

    return min(candidates, key=lambda candidate: candidate.route_mile).station.price


def fuel_assumptions() -> dict[str, Any]:
    return {
        "vehicle_range_miles": MAX_RANGE_MILES,
        "fuel_efficiency_mpg": MILES_PER_GALLON,
        "tank_capacity_gallons": round(MAX_RANGE_MILES / MILES_PER_GALLON, 2),
        "initial_fuel": (
            "The trip is costed from mile 0 using the lowest fuel price found near "
            "the beginning of the route, because the source CSV has station prices "
            "but no exact origin fuel price."
        ),
        "station_coordinates": (
            "CSV stations are positioned by matching their city/state to local "
            "GeoNames city centroids; selected stop addresses still come from the "
            "provided fuel-price CSV."
        ),
    }


def load_stations() -> list[Station]:
    base_dir = Path(settings.BASE_DIR)
    fuel_csv = Path(getattr(settings, "ROUTE_PLANNER_FUEL_CSV", base_dir / "fuel-prices-for-be-assessment.csv"))
    city_csv = Path(
        getattr(
            settings,
            "ROUTE_PLANNER_CITY_CENTROIDS_CSV",
            base_dir / "route_planner" / "data" / "us_city_centroids.csv",
        )
    )

    city_coordinates = load_city_coordinates(city_csv)
    by_station_id: dict[str, Station] = {}

    with fuel_csv.open(newline="", encoding="utf-8") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=1):
            state = clean_text(row.get("State", "")).upper()
            city = clean_text(row.get("City", "")).upper()
            if state not in US_STATE_CODES:
                continue

            coordinate = city_coordinates.get((city, state))
            if coordinate is None:
                continue

            try:
                price = float(row.get("Retail Price", ""))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue

            raw_id = clean_text(row.get("OPIS Truckstop ID", ""))
            station_id = raw_id or f"row-{row_number}"
            station = Station(
                station_id=station_id,
                name=clean_text(row.get("Truckstop Name", "")) or "Unknown Truckstop",
                address=clean_text(row.get("Address", "")),
                city=clean_text(row.get("City", "")),
                state=state,
                rack_id=clean_text(row.get("Rack ID", "")),
                price=price,
                coordinate=coordinate,
            )

            current = by_station_id.get(station_id)
            if current is None or station.price < current.price:
                by_station_id[station_id] = station

    return list(by_station_id.values())


def load_city_coordinates(path: Path) -> dict[tuple[str, str], Coordinate]:
    coordinates: dict[tuple[str, str], Coordinate] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            city = clean_text(row["city"]).upper()
            state = clean_text(row["state"]).upper()
            coordinates[(city, state)] = Coordinate(
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
            )
    return coordinates


def sample_route(
    coordinates: list[Coordinate],
    route_distance_miles: float,
) -> list[RouteSample]:
    cumulative = [0.0]
    for previous, current in zip(coordinates, coordinates[1:]):
        cumulative.append(cumulative[-1] + haversine_miles(previous, current))

    geometry_distance = cumulative[-1]
    if geometry_distance <= 0:
        return [RouteSample(coordinates[0], 0.0)]

    scale = route_distance_miles / geometry_distance
    sample_count = max(1, math.ceil(route_distance_miles / ROUTE_SAMPLE_INTERVAL_MILES))
    wanted_miles = [
        min(route_distance_miles, index * ROUTE_SAMPLE_INTERVAL_MILES)
        for index in range(sample_count + 1)
    ]
    if wanted_miles[-1] < route_distance_miles:
        wanted_miles.append(route_distance_miles)

    samples: list[RouteSample] = []
    segment_index = 1
    for wanted_route_mile in wanted_miles:
        wanted_geometry_mile = wanted_route_mile / scale
        while (
            segment_index < len(cumulative) - 1
            and cumulative[segment_index] < wanted_geometry_mile
        ):
            segment_index += 1

        previous_mile = cumulative[segment_index - 1]
        next_mile = cumulative[segment_index]
        ratio = 0.0
        if next_mile > previous_mile:
            ratio = (wanted_geometry_mile - previous_mile) / (next_mile - previous_mile)

        previous = coordinates[segment_index - 1]
        current = coordinates[segment_index]
        samples.append(
            RouteSample(
                coordinate=Coordinate(
                    latitude=previous.latitude
                    + (current.latitude - previous.latitude) * ratio,
                    longitude=previous.longitude
                    + (current.longitude - previous.longitude) * ratio,
                ),
                mile=wanted_route_mile,
            )
        )
    return samples


def haversine_miles(a: Coordinate, b: Coordinate) -> float:
    lat1 = math.radians(a.latitude)
    lat2 = math.radians(b.latitude)
    d_lat = lat2 - lat1
    d_lon = math.radians(b.longitude - a.longitude)
    hav = (
        math.sin(d_lat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(hav)))


def serialize_stop(stop_number: int, node: FuelNode) -> dict[str, Any]:
    station = node.station
    if station is None:
        raise ValueError("Fuel stop node must have a station.")

    return {
        "sequence": stop_number,
        "station_id": station.station_id,
        "name": station.name,
        "address": station.address,
        "city": station.city,
        "state": station.state,
        "rack_id": station.rack_id,
        "retail_price_usd_per_gallon": round(station.price, 3),
        "route_mile": round(node.mile, 1),
        "coordinates": station.coordinate.as_geojson_position(),
    }


def route_bounds(coordinates: list[Coordinate]) -> dict[str, float]:
    latitudes = [coordinate.latitude for coordinate in coordinates]
    longitudes = [coordinate.longitude for coordinate in coordinates]
    return {
        "min_latitude": round(min(latitudes), 6),
        "min_longitude": round(min(longitudes), 6),
        "max_latitude": round(max(latitudes), 6),
        "max_longitude": round(max(longitudes), 6),
    }


def build_feature_collection(
    route_coordinates: list[list[float]],
    start: Coordinate,
    finish: Coordinate,
    stops: list[dict[str, Any]],
) -> dict[str, Any]:
    features: list[dict[str, Any]] = [
        {
            "type": "Feature",
            "properties": {"kind": "route"},
            "geometry": {"type": "LineString", "coordinates": route_coordinates},
        },
        {
            "type": "Feature",
            "properties": {"kind": "start"},
            "geometry": {"type": "Point", "coordinates": start.as_geojson_position()},
        },
        {
            "type": "Feature",
            "properties": {"kind": "finish"},
            "geometry": {"type": "Point", "coordinates": finish.as_geojson_position()},
        },
    ]

    for stop in stops:
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "kind": "fuel_stop",
                    "sequence": stop["sequence"],
                    "name": stop["name"],
                    "price": stop["retail_price_usd_per_gallon"],
                    "route_mile": stop["route_mile"],
                },
                "geometry": {"type": "Point", "coordinates": stop["coordinates"]},
            }
        )

    return {"type": "FeatureCollection", "features": features}


def clean_text(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def _get_json(url: str, timeout: float) -> Any:
    headers = {
        "Accept": "application/json",
        "User-Agent": getattr(
            settings,
            "ROUTE_PLANNER_USER_AGENT",
            "route-detection-assessment/1.0",
        ),
    }
    try:
        req = request.Request(url, headers=headers)
        with request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise UpstreamServiceError(
            f"Upstream service returned HTTP {exc.code}."
        ) from exc
    except (error.URLError, TimeoutError) as exc:
        raise UpstreamServiceError("Upstream service is not reachable.") from exc
    except json.JSONDecodeError as exc:
        raise UpstreamServiceError("Upstream service returned invalid JSON.") from exc


def _cache_key(namespace: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"route_planner:{namespace}:{digest}"
