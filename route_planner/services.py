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
SAFE_RANGE_MILES = 480.0
MILES_PER_GALLON = 10.0
ROUTE_SAMPLE_INTERVAL_MILES = 5.0
STATION_GRID_DEGREES = 1.0
CORRIDOR_FALLBACK_MILES = (10.0, 25.0, 50.0)
EXTRA_STOP_ATTEMPTS = 2
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


def plan_route(start_text: str, finish_text: str) -> dict[str, Any]:
    started_at = time.perf_counter()
    start = geocode_location(start_text)
    finish = geocode_location(finish_text)
    route = fetch_route(start, finish)

    distance_miles = route["distance_miles"]
    candidates: list[CandidateStation] = []
    selected_radius = None
    database_station_count = None

    if distance_miles <= SAFE_RANGE_MILES:
        fuel_plan = choose_fuel_stops(
            distance_miles,
            candidates,
            CORRIDOR_FALLBACK_MILES[0],
        )
    else:
        samples = sample_route(route["coordinates"], distance_miles)
        station_index = StationIndex.from_database()
        database_station_count = station_index.station_count
        fuel_plan = None

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
            "database_station_count": database_station_count,
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
    cache_query = query.casefold()
    cached = GeocodeCache.objects.filter(query=cache_query, is_active=True).first()
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
            query=cache_query,
            defaults={
                "latitude": latitude,
                "longitude": longitude,
                "display_name": display_name,
                "provider": "nominatim",
                "is_active": True,
            },
        )

    return Coordinate(float(latitude), float(longitude))


def fetch_route(start: Coordinate, finish: Coordinate) -> dict[str, Any]:
    cache_key = route_cache_key(start, finish)
    cached = RouteCache.objects.filter(cache_key=cache_key, is_active=True).first()
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
            "overview": "full",
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
            "is_active": True,
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
                is_active=True,
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
    route_gallons = distance_miles / MILES_PER_GALLON
    required_stop_count = max(0, math.ceil(distance_miles / SAFE_RANGE_MILES) - 1)
    if required_stop_count == 0:
        return {
            "total_fuel_cost_usd": 0.0,
            "en_route_fuel_cost_usd": 0.0,
            "en_route_fuel_purchase_cost_usd": 0.0,
            "selected_stop_purchase_cost_usd": 0.0,
            "total_route_gallons": round(route_gallons, 2),
            "total_purchased_gallons": 0.0,
            "total_detour_cost_usd": 0.0,
            "detour_fuel_cost_usd": 0.0,
            "selected_fuel_stops": [],
            "assumptions": fuel_assumptions(),
        }

    path = None
    planned_stop_count = None
    last_error: PlanningError | None = None
    max_stop_count = min(len(candidates), required_stop_count + EXTRA_STOP_ATTEMPTS)
    for stop_count in range(required_stop_count, max_stop_count + 1):
        try:
            stages = candidate_stages(distance_miles, candidates, stop_count)
            path = cheapest_stop_path(distance_miles, stages, corridor_radius_miles)
        except PlanningError as exc:
            last_error = exc
            continue
        planned_stop_count = stop_count
        break

    if path is None or planned_stop_count is None:
        raise last_error or PlanningError(
            "No feasible fuel-stop chain found within the vehicle safety range."
        )

    selected_stops = []
    selected_stop_purchase_cost = 0.0
    total_detour_cost = 0.0
    total_purchased_gallons = 0.0

    for index, candidate in enumerate(path):
        next_mile = path[index + 1].route_mile if index + 1 < len(path) else distance_miles
        stop = serialize_selected_stop(index + 1, candidate, next_mile)
        selected_stops.append(stop)
        selected_stop_purchase_cost += stop["fuel_cost_usd"]
        total_detour_cost += stop["detour_cost_usd"]
        total_purchased_gallons += stop["gallons_needed"]

    en_route_fuel_cost = selected_stop_purchase_cost + total_detour_cost
    return {
        "total_fuel_cost_usd": round(en_route_fuel_cost, 2),
        "en_route_fuel_cost_usd": round(en_route_fuel_cost, 2),
        "en_route_fuel_purchase_cost_usd": round(selected_stop_purchase_cost, 2),
        "selected_stop_purchase_cost_usd": round(selected_stop_purchase_cost, 2),
        "total_route_gallons": round(route_gallons, 2),
        "total_purchased_gallons": round(total_purchased_gallons, 2),
        "total_detour_cost_usd": round(total_detour_cost, 2),
        "detour_fuel_cost_usd": round(total_detour_cost, 2),
        "minimum_required_stops": required_stop_count,
        "planned_stop_count": planned_stop_count,
        "selected_fuel_stops": selected_stops,
        "assumptions": fuel_assumptions(),
    }


def candidate_stages(
    distance_miles: float,
    candidates: list[CandidateStation],
    stop_count: int,
) -> list[list[CandidateStation]]:
    stages = []
    for stage_index in range(1, stop_count + 1):
        earliest = max(
            0.0,
            distance_miles - (stop_count - stage_index + 1) * SAFE_RANGE_MILES,
        )
        latest = min(distance_miles, stage_index * SAFE_RANGE_MILES)
        stage_candidates = [
            candidate
            for candidate in candidates
            if earliest <= candidate.route_mile <= latest
        ]
        if not stage_candidates:
            raise PlanningError(
                f"No fuel station found for required stop {stage_index} "
                f"between route miles {earliest:.0f}-{latest:.0f}."
            )
        stages.append(stage_candidates)
    return stages


def cheapest_stop_path(
    distance_miles: float,
    stages: list[list[CandidateStation]],
    corridor_radius_miles: float,
) -> list[CandidateStation]:
    previous_costs: dict[int, tuple[float, CandidateStation, list[CandidateStation]]] = {}
    for candidate in stages[0]:
        if candidate.route_mile <= SAFE_RANGE_MILES:
            previous_costs[id(candidate)] = (0.0, candidate, [candidate])

    if not previous_costs:
        raise PlanningError(
            f"No reachable first fuel stop found within {SAFE_RANGE_MILES:g} miles "
            f"and {corridor_radius_miles:g} miles of the route."
        )

    for stage in stages[1:]:
        current_costs: dict[int, tuple[float, CandidateStation, list[CandidateStation]]] = {}
        for candidate in stage:
            best: tuple[float, CandidateStation, list[CandidateStation]] | None = None
            for previous_cost, previous_candidate, previous_path in previous_costs.values():
                leg_miles = candidate.route_mile - previous_candidate.route_mile
                if leg_miles <= 0 or leg_miles > SAFE_RANGE_MILES:
                    continue
                cost = previous_cost + fuel_leg_cost(previous_candidate, leg_miles)
                path = [*previous_path, candidate]
                if best is None or cost < best[0]:
                    best = (cost, candidate, path)
            if best is not None:
                current_costs[id(candidate)] = best
        previous_costs = current_costs
        if not previous_costs:
            raise PlanningError(
                "No feasible fuel-stop chain found within the vehicle safety range."
            )

    best_final: tuple[float, CandidateStation, list[CandidateStation]] | None = None
    for previous_cost, previous_candidate, previous_path in previous_costs.values():
        final_leg_miles = distance_miles - previous_candidate.route_mile
        if final_leg_miles <= 0 or final_leg_miles > SAFE_RANGE_MILES:
            continue
        cost = previous_cost + fuel_leg_cost(previous_candidate, final_leg_miles)
        if best_final is None or cost < best_final[0]:
            best_final = (cost, previous_candidate, previous_path)

    if best_final is None:
        raise PlanningError(
            "No final fuel stop can reach the destination within the vehicle safety range."
        )
    return best_final[2]


def serialize_selected_stop(
    sequence: int,
    candidate: CandidateStation,
    next_route_mile: float,
) -> dict[str, Any]:
    station = candidate.station
    price = float(station.retail_price)
    leg_miles = next_route_mile - candidate.route_mile
    gallons = leg_miles / MILES_PER_GALLON
    fuel_cost = gallons * price
    detour_miles = candidate.distance_from_route_miles * 2.0
    detour_gallons = detour_miles / MILES_PER_GALLON
    detour_cost = detour_gallons * price
    return {
        "sequence": sequence,
        "route_mile": round(candidate.route_mile, 1),
        "next_route_mile": round(next_route_mile, 1),
        "leg_miles_fueled": round(leg_miles, 1),
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


def fuel_leg_cost(candidate: CandidateStation, leg_miles: float) -> float:
    price = float(candidate.station.retail_price)
    route_fuel_cost = (leg_miles / MILES_PER_GALLON) * price
    detour_cost = ((candidate.distance_from_route_miles * 2.0) / MILES_PER_GALLON) * price
    return route_fuel_cost + detour_cost


def fuel_assumptions() -> dict[str, Any]:
    return {
        "starts_with_full_tank": True,
        "vehicle_range_miles": MAX_RANGE_MILES,
        "safety_range_miles": SAFE_RANGE_MILES,
        "fuel_efficiency_mpg": MILES_PER_GALLON,
        "corridor_fallback_miles": list(CORRIDOR_FALLBACK_MILES),
        "cost_scope": (
            "Fuel cost is calculated for fuel purchased at selected stops along "
            "the route. The initial full tank is not counted as an en-route purchase."
        ),
        "extra_stop_strategy": (
            "The planner starts with the minimum required stop count and retries "
            "with extra stops when station placement makes the minimum chain infeasible."
        ),
        "effective_cost_formula": (
            "next_leg_gallons * station_price + "
            "(distance_from_route_miles * 2 / mpg) * station_price"
        ),
    }


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
        "osrm-full-v1:"
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
