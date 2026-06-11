from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib import error, parse, request

from django.conf import settings
from django.db import transaction

from .models import FuelStation, GeocodeCache, RouteCache


EARTH_RADIUS_MILES = 3958.7613
MAX_RANGE_MILES = 500.0
MILES_PER_GALLON = 10.0
ROUTE_SAMPLE_INTERVAL_MILES = 5.0
STATION_GRID_DEGREES = 1.0
CORRIDOR_FALLBACK_MILES = (10.0, 25.0, 50.0)
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
class RouteSample:
    coordinate: Coordinate
    mile: float


@dataclass(slots=True)
class CandidateStation:
    station: FuelStation
    route_mile: float
    distance_from_route_miles: float


@dataclass(frozen=True, slots=True)
class RouteWindow:
    sequence: int
    start_mile: float
    end_mile: float

    @property
    def miles(self) -> float:
        return self.end_mile - self.start_mile


def plan_route(start_text: str, finish_text: str) -> dict[str, Any]:
    started_at = time.perf_counter()
    start = geocode_location(start_text)
    finish = geocode_location(finish_text)
    route = fetch_route(start, finish)

    distance_miles = route["distance_miles"]
    samples = sample_route(route["coordinates"], distance_miles)
    station_index = StationIndex.from_database()

    selected_radius = None
    candidates: list[CandidateStation] = []
    fuel_plan: dict[str, Any] | None = None
    for radius in CORRIDOR_FALLBACK_MILES:
        candidates = station_index.candidates_near_route(samples, distance_miles, radius)
        try:
            fuel_plan = choose_fuel_stops(distance_miles, candidates, radius)
        except PlanningError:
            continue
        selected_radius = radius
        break

    if fuel_plan is None or selected_radius is None:
        raise PlanningError("No fuel station from provided CSV found near this route.")

    route_coordinates = [point.as_geojson_position() for point in route["coordinates"]]
    return {
        "start_location": {
            "input": start_text,
            "latitude": round(start.latitude, 7),
            "longitude": round(start.longitude, 7),
        },
        "finish_location": {
            "input": finish_text,
            "latitude": round(finish.latitude, 7),
            "longitude": round(finish.longitude, 7),
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
        "fuel_plan": fuel_plan,
        "station_search": {
            "corridor_radius_miles": selected_radius,
            "candidate_count": len(candidates),
            "database_station_count": station_index.station_count,
        },
        "map": build_feature_collection(
            route_coordinates=route_coordinates,
            start=start,
            finish=finish,
            stops=fuel_plan["selected_fuel_stops"],
        ),
        "providers": {
            "geocoding": "OpenStreetMap Nominatim, cached in GeocodeCache",
            "routing": "OSRM public demo server, cached in RouteCache",
            "fuel_prices": "FuelStation table imported from fuel-prices-for-be-assessment.csv",
        },
        "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
    }


def geocode_location(location: str) -> Coordinate:
    query = normalize_location(location)
    cached = GeocodeCache.objects.filter(query=query).first()
    if cached is not None:
        return Coordinate(float(cached.latitude), float(cached.longitude))

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
        latitude = Decimal(payload[0]["lat"]).quantize(Decimal("0.0000001"))
        longitude = Decimal(payload[0]["lon"]).quantize(Decimal("0.0000001"))
        display_name = str(payload[0].get("display_name", ""))
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise UpstreamServiceError("Geocoder returned an unexpected response.") from exc

    with transaction.atomic():
        GeocodeCache.objects.update_or_create(
            query=query,
            defaults={
                "latitude": latitude,
                "longitude": longitude,
                "display_name": display_name,
                "provider": "nominatim",
            },
        )

    return Coordinate(float(latitude), float(longitude))


def fetch_route(start: Coordinate, finish: Coordinate) -> dict[str, Any]:
    cache_key = route_cache_key(start, finish)
    cached = RouteCache.objects.filter(cache_key=cache_key).first()
    if cached is not None:
        return {
            "distance_miles": cached.distance_miles,
            "duration_minutes": cached.duration_minutes,
            "coordinates": [
                Coordinate(latitude=float(lat), longitude=float(lon))
                for lon, lat in cached.geometry
            ],
        }

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

    RouteCache.objects.update_or_create(
        cache_key=cache_key,
        defaults={
            "start_latitude": Decimal(str(start.latitude)).quantize(Decimal("0.0000001")),
            "start_longitude": Decimal(str(start.longitude)).quantize(Decimal("0.0000001")),
            "finish_latitude": Decimal(str(finish.latitude)).quantize(Decimal("0.0000001")),
            "finish_longitude": Decimal(str(finish.longitude)).quantize(Decimal("0.0000001")),
            "distance_miles": distance_miles,
            "duration_minutes": duration_minutes,
            "geometry": geometry,
            "provider": "osrm",
        },
    )
    return {
        "distance_miles": distance_miles,
        "duration_minutes": duration_minutes,
        "coordinates": coordinates,
    }


class StationIndex:
    def __init__(self, stations: list[FuelStation]) -> None:
        self.stations = stations
        self.station_count = len(stations)
        self._grid: dict[tuple[int, int], list[FuelStation]] = {}
        for station in stations:
            key = self._grid_key(float(station.latitude), float(station.longitude))
            self._grid.setdefault(key, []).append(station)

    @classmethod
    def from_database(cls) -> "StationIndex":
        stations = list(
            FuelStation.objects.filter(
                latitude__isnull=False,
                longitude__isnull=False,
                state__in=US_STATE_CODES,
            ).only(
                "id",
                "opis_truckstop_id",
                "truckstop_name",
                "address",
                "city",
                "state",
                "rack_id",
                "retail_price",
                "latitude",
                "longitude",
            )
        )
        if not stations:
            raise PlanningError(
                "FuelStation table has no geocoded rows. Run import_fuel_stations and geocode_fuel_stations first."
            )
        return cls(stations)

    def candidates_near_route(
        self,
        samples: list[RouteSample],
        route_distance_miles: float,
        radius_miles: float,
    ) -> list[CandidateStation]:
        best_by_station: dict[int, CandidateStation] = {}

        for sample in samples:
            for station in self._stations_around(sample.coordinate, radius_miles):
                station_coordinate = Coordinate(
                    latitude=float(station.latitude),
                    longitude=float(station.longitude),
                )
                distance = haversine_miles(sample.coordinate, station_coordinate)
                if distance > radius_miles:
                    continue

                current = best_by_station.get(station.id)
                if current is None or distance < current.distance_from_route_miles:
                    best_by_station[station.id] = CandidateStation(
                        station=station,
                        route_mile=max(0.0, min(route_distance_miles, sample.mile)),
                        distance_from_route_miles=distance,
                    )

        return sorted(
            best_by_station.values(),
            key=lambda candidate: (
                candidate.route_mile,
                float(candidate.station.retail_price),
                candidate.distance_from_route_miles,
            ),
        )

    def _stations_around(
        self, coordinate: Coordinate, radius_miles: float
    ) -> list[FuelStation]:
        lat_span = max(1, math.ceil(radius_miles / 69.0))
        lon_miles = max(20.0, 69.0 * math.cos(math.radians(coordinate.latitude)))
        lon_span = max(1, math.ceil(radius_miles / lon_miles))
        center_lat, center_lon = self._grid_key(coordinate.latitude, coordinate.longitude)

        stations: list[FuelStation] = []
        for lat_key in range(center_lat - lat_span, center_lat + lat_span + 1):
            for lon_key in range(center_lon - lon_span, center_lon + lon_span + 1):
                stations.extend(self._grid.get((lat_key, lon_key), ()))
        return stations

    @staticmethod
    def _grid_key(latitude: float, longitude: float) -> tuple[int, int]:
        return (
            math.floor(latitude / STATION_GRID_DEGREES),
            math.floor(longitude / STATION_GRID_DEGREES),
        )


def choose_fuel_stops(
    distance_miles: float,
    candidates: list[CandidateStation],
    corridor_radius_miles: float,
) -> dict[str, Any]:
    windows = route_windows(distance_miles)
    candidates_by_window: list[tuple[RouteWindow, list[CandidateStation]]] = []

    for window in windows:
        window_candidates = [
            candidate
            for candidate in candidates
            if window.start_mile <= candidate.route_mile <= window.end_mile
        ]
        if not window_candidates:
            raise PlanningError(
                f"No fuel station found within {corridor_radius_miles:g} miles for route miles "
                f"{window.start_mile:.0f}-{window.end_mile:.0f}."
            )
        candidates_by_window.append((window, window_candidates))

    selected_stops = []
    total_cost = 0.0
    total_detour_cost = 0.0
    total_gallons = 0.0

    for window, window_candidates in candidates_by_window:
        selected = min(
            window_candidates,
            key=lambda candidate: effective_station_cost(window.miles, candidate),
        )
        stop = serialize_selected_stop(window, selected)
        selected_stops.append(stop)
        total_cost += stop["fuel_cost_usd"] + stop["detour_cost_usd"]
        total_detour_cost += stop["detour_cost_usd"]
        total_gallons += stop["gallons_needed"]

    return {
        "total_fuel_cost_usd": round(total_cost, 2),
        "total_route_gallons": round(total_gallons, 2),
        "total_detour_cost_usd": round(total_detour_cost, 2),
        "selected_fuel_stops": selected_stops,
        "assumptions": {
            "vehicle_range_miles": MAX_RANGE_MILES,
            "fuel_efficiency_mpg": MILES_PER_GALLON,
            "route_window_miles": MAX_RANGE_MILES,
            "corridor_fallback_miles": list(CORRIDOR_FALLBACK_MILES),
            "effective_cost_formula": (
                "route_window_gallons * station_price + "
                "(distance_from_route_miles * 2 / mpg) * station_price"
            ),
        },
    }


def serialize_selected_stop(
    window: RouteWindow,
    candidate: CandidateStation,
) -> dict[str, Any]:
    station = candidate.station
    price = float(station.retail_price)
    gallons = window.miles / MILES_PER_GALLON
    fuel_cost = gallons * price
    detour_miles = candidate.distance_from_route_miles * 2.0
    detour_gallons = detour_miles / MILES_PER_GALLON
    detour_cost = detour_gallons * price
    return {
        "sequence": window.sequence,
        "window_start_mile": round(window.start_mile, 1),
        "window_end_mile": round(window.end_mile, 1),
        "route_mile": round(candidate.route_mile, 1),
        "truckstop_id": station.opis_truckstop_id,
        "truckstop_name": station.truckstop_name,
        "address": station.address,
        "city": station.city,
        "state": station.state,
        "rack_id": station.rack_id,
        "latitude": round(float(station.latitude), 7),
        "longitude": round(float(station.longitude), 7),
        "price_per_gallon_usd": round(price, 4),
        "gallons_needed": round(gallons, 2),
        "fuel_cost_usd": round(fuel_cost, 2),
        "distance_from_route_miles": round(candidate.distance_from_route_miles, 2),
        "detour_miles": round(detour_miles, 2),
        "detour_cost_usd": round(detour_cost, 2),
        "effective_cost_usd": round(fuel_cost + detour_cost, 2),
    }


def effective_station_cost(window_miles: float, candidate: CandidateStation) -> float:
    price = float(candidate.station.retail_price)
    route_fuel_cost = (window_miles / MILES_PER_GALLON) * price
    detour_cost = ((candidate.distance_from_route_miles * 2.0) / MILES_PER_GALLON) * price
    return route_fuel_cost + detour_cost


def route_windows(distance_miles: float) -> list[RouteWindow]:
    windows = []
    start = 0.0
    sequence = 1
    while start < distance_miles:
        end = min(start + MAX_RANGE_MILES, distance_miles)
        windows.append(RouteWindow(sequence=sequence, start_mile=start, end_mile=end))
        start = end
        sequence += 1
    return windows


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
                    "name": stop["truckstop_name"],
                    "price": stop["price_per_gallon_usd"],
                    "route_mile": stop["route_mile"],
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [stop["longitude"], stop["latitude"]],
                },
            }
        )

    return {"type": "FeatureCollection", "features": features}


def normalize_location(location: str) -> str:
    query = " ".join(str(location or "").split())
    if not query:
        raise BadRequest("Both start_location and finish_location are required.")
    return query


def route_cache_key(start: Coordinate, finish: Coordinate) -> str:
    raw = (
        f"{start.latitude:.5f},{start.longitude:.5f}:"
        f"{finish.latitude:.5f},{finish.longitude:.5f}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
