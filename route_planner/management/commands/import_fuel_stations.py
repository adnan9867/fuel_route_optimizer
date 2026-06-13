from __future__ import annotations

import csv
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib import parse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from route_planner.models import FuelStation
from route_planner.services import UpstreamServiceError, _get_json


PRICE_PRECISION = Decimal("0.0001")
COORDINATE_PRECISION = Decimal("0.0000001")
NOMINATIM_SLEEP_SECONDS = 1.0
GEOCODE_PROGRESS_INTERVAL = 25
NOMINATIM_SOURCE = "nominatim"
NOMINATIM_FALLBACK_SOURCE = "nominatim-fallback"
GEOCODIO_SOURCE = "geocodio-fallback"
NOMINATIM_NO_RESULT_SOURCE = "nominatim-no-result"
NOMINATIM_FALLBACK_NO_RESULT_SOURCE = "nominatim-no-result-with-fallback"
GEOCODIO_MIN_ACCURACY = Decimal("0.8")
GEOCODIO_HIGHWAY_EXIT_MIN_ACCURACY = Decimal("0.75")
GEOCODIO_IMPRECISE_ACCURACY_TYPES = {"county", "place", "state"}
GEOCODIO_HIGHWAY_EXIT_ACCURACY_TYPES = {"intersection", "street_center"}
HIGHWAY_EXIT_PATTERN = re.compile(r"\bexits?\b", re.IGNORECASE)
HIGHWAY_ROUTE_PATTERN = re.compile(
    r"\b(?:i-\d+[a-z]?|us-\d+[a-z]?|sr-\d+[a-z]?|hwy|highway|interstate)\b",
    re.IGNORECASE,
)
PROCESSED_GEOCODE_SOURCES = (
    NOMINATIM_SOURCE,
    NOMINATIM_FALLBACK_SOURCE,
    GEOCODIO_SOURCE,
    NOMINATIM_NO_RESULT_SOURCE,
    NOMINATIM_FALLBACK_NO_RESULT_SOURCE,
)


class Command(BaseCommand):
    help = "Import fuel prices and fill in missing station coordinates."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--csv",
            default=str(settings.BASE_DIR / "fuel-prices-for-be-assessment.csv"),
            help="Path to the provided assessment CSV.",
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help="Delete existing fuel stations before importing.",
        )

    def handle(self, *args, **options) -> None:
        csv_path = Path(options["csv"])
        if not csv_path.exists():
            raise CommandError(f"CSV file not found: {csv_path}")

        if options["replace"]:
            deleted, _ = FuelStation.objects.all().delete()
            self.stdout.write(f"Deleted {deleted} existing fuel station rows.")

        created, updated, skipped = import_fuel_station_csv(csv_path)
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {created} and updated {updated} fuel stations from "
                f"{csv_path.name}; skipped {skipped} rows."
            )
        )

        geocoded = geocode_missing_stations(
            stdout=self.stdout,
            success_style=self.style.SUCCESS,
            warning_style=self.style.WARNING,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Geocoded {geocoded} fuel stations with Nominatim/Geocodio."
            )
        )


def import_fuel_station_csv(csv_path: Path) -> tuple[int, int, int]:
    rows, skipped = read_station_rows(csv_path)

    created = 0
    updated = 0
    for truckstop_id, defaults in rows.items():
        _, was_created = FuelStation.objects.update_or_create(
            opis_truckstop_id=truckstop_id,
            defaults=defaults,
        )
        if was_created:
            created += 1
        else:
            updated += 1

    return created, updated, skipped


def read_station_rows(
    csv_path: Path,
) -> tuple[dict[str, dict[str, str | Decimal | bool]], int]:
    rows: dict[str, dict[str, str | Decimal | bool]] = {}
    skipped = 0

    with csv_path.open(newline="", encoding="utf-8") as handle:
        for row_number, raw_row in enumerate(csv.DictReader(handle), start=1):
            try:
                truckstop_id, row = station_row_from_csv(raw_row, row_number)
            except (KeyError, InvalidOperation):
                skipped += 1
                continue

            current = rows.get(truckstop_id)
            if current is None or row["retail_price"] < current["retail_price"]:
                rows[truckstop_id] = row

    return rows, skipped


def station_row_from_csv(
    row: dict[str, str],
    row_number: int,
) -> tuple[str, dict[str, str | Decimal | bool]]:
    truckstop_id = clean(row.get("OPIS Truckstop ID")) or f"row-{row_number}"
    price = Decimal(clean(row["Retail Price"])).quantize(PRICE_PRECISION)

    return truckstop_id, {
        "truckstop_name": clean(row.get("Truckstop Name")) or "Unknown Truckstop",
        "address": clean(row.get("Address")),
        "city": clean(row.get("City")),
        "state": clean(row.get("State")).upper(),
        "rack_id": clean(row.get("Rack ID")),
        "retail_price": price,
        "is_active": True,
    }


def clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def geocode_missing_stations(
    stdout,
    success_style,
    warning_style,
) -> int:
    already_geocoded = FuelStation.objects.filter(
        is_active=True,
        latitude__isnull=False,
        longitude__isnull=False,
    ).count()
    if already_geocoded:
        stdout.write(success_style(f"Skipped {already_geocoded} already geocoded stations."))

    missing_stations = FuelStation.objects.filter(
        is_active=True,
        latitude__isnull=True,
        longitude__isnull=True,
    )
    pending_stations = missing_stations.exclude(
        geocode_source__in=PROCESSED_GEOCODE_SOURCES,
    ).order_by("id")
    skipped_previously_processed = missing_stations.count() - pending_stations.count()
    total = pending_stations.count()
    if skipped_previously_processed:
        stdout.write(
            success_style(
                f"Skipped {skipped_previously_processed} previously processed stations."
            )
        )
    if total == 0:
        stdout.write(success_style("No fuel stations are missing coordinates."))
        return 0

    updated = 0
    no_result = 0
    stdout.write(
        f"Geocoding {total} fuel stations with Nominatim/Geocodio "
        f"(sleep={NOMINATIM_SLEEP_SECONDS}s after Nominatim requests)."
    )
    for index, station in enumerate(pending_stations.iterator(chunk_size=100), start=1):
        result = geocode_station(station)
        if result is not None:
            payload, geocode_source = result
            save_coordinates(station, payload, geocode_source)
            updated += 1
            status = "geocoded"
            if geocode_source in (NOMINATIM_FALLBACK_SOURCE, GEOCODIO_SOURCE):
                status = "geocoded via fallback"
        else:
            mark_without_coordinates(station)
            no_result += 1
            status = "no result"

        if should_report_progress(index, total):
            stdout.write(
                f"[{index}/{total}] {status}: "
                f"{station.truckstop_name} ({station.city}, {station.state})"
            )
        if result is None or geocode_source != GEOCODIO_SOURCE:
            time.sleep(NOMINATIM_SLEEP_SECONDS)

    if no_result:
        stdout.write(
            warning_style(
                f"Geocoding returned no precise result for {no_result} fuel stations. "
                "They are marked and will be skipped on the next import."
            )
        )
    return updated


def save_coordinates(
    station: FuelStation,
    payload: dict[str, object],
    geocode_source: str,
) -> None:
    station.latitude = Decimal(payload["lat"]).quantize(COORDINATE_PRECISION)
    station.longitude = Decimal(payload["lon"]).quantize(COORDINATE_PRECISION)
    station.geocode_source = geocode_source
    station.geocoded_at = timezone.now()
    station.save(
        update_fields=[
            "latitude",
            "longitude",
            "geocode_source",
            "geocoded_at",
            "updated_at",
        ]
    )


def mark_without_coordinates(station: FuelStation) -> None:
    station.geocode_source = NOMINATIM_FALLBACK_NO_RESULT_SOURCE
    station.geocoded_at = timezone.now()
    station.save(
        update_fields=[
            "geocode_source",
            "geocoded_at",
            "updated_at",
        ]
    )


def should_report_progress(index: int, total: int) -> bool:
    return index == 1 or index == total or index % GEOCODE_PROGRESS_INTERVAL == 0


def geocode_station(station: FuelStation) -> tuple[dict[str, object], str] | None:
    geocodio_payload = geocode_station_with_geocodio(station)
    if geocodio_payload is not None:
        return geocodio_payload, GEOCODIO_SOURCE

    for attempt, (geocode_source, url) in enumerate(nominatim_urls(station), start=1):
        if attempt > 1:
            time.sleep(NOMINATIM_SLEEP_SECONDS)
        payload = _get_json(url, timeout=station_geocode_timeout())
        if payload:
            return payload[0], geocode_source
    return None


def nominatim_urls(station: FuelStation) -> list[tuple[str, str]]:
    queries = [
        (
            NOMINATIM_SOURCE,
            nominatim_query(
                station.address,
                station.city,
                station.state,
                "USA",
            ),
        ),
        (
            NOMINATIM_FALLBACK_SOURCE,
            nominatim_query(
                station.truckstop_name,
                station.city,
                station.state,
                "USA",
            ),
        ),
    ]
    urls: list[tuple[str, str]] = []
    seen_queries: set[str] = set()
    for geocode_source, query in queries:
        normalized_query = query.casefold()
        if not query or normalized_query in seen_queries:
            continue
        seen_queries.add(normalized_query)
        urls.append((geocode_source, nominatim_url(query)))
    return urls


def nominatim_query(*parts: object) -> str:
    return ", ".join(part for part in (clean(part) for part in parts) if part)


def geocode_station_with_geocodio(station: FuelStation) -> dict[str, object] | None:
    keys = geocodio_api_keys(station)
    if not keys:
        return None

    query = geocodio_query(station)
    if not query:
        return None

    for api_key in keys:
        try:
            payload = _get_json(
                geocodio_url(query, api_key),
                timeout=station_geocode_timeout(),
            )
        except UpstreamServiceError:
            continue
        return precise_geocodio_result(
            payload,
            allow_highway_exit_fallback=has_highway_exit_address(station),
        )
    return None


def station_geocode_timeout() -> float:
    return getattr(settings, "ROUTE_PLANNER_STATION_GEOCODE_TIMEOUT_SECONDS", 6)


def geocodio_api_keys(station: FuelStation) -> list[str]:
    keys = list(getattr(settings, "GEOCODIO_API_KEYS", []))
    if len(keys) <= 1:
        return keys

    start_index = (station.id or 0) % len(keys)
    return [*keys[start_index:], *keys[:start_index]]


def geocodio_query(station: FuelStation) -> str:
    return nominatim_query(
        station.address,
        station.city,
        station.state,
        "USA",
    )


def geocodio_url(query: str, api_key: str) -> str:
    params = parse.urlencode(
        {
            "q": query,
            "api_key": api_key,
        }
    )
    return f"https://api.geocod.io/v2/geocode?{params}"


def precise_geocodio_result(
    payload: object,
    allow_highway_exit_fallback: bool = False,
) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None

    results = payload.get("results")
    if not isinstance(results, list):
        return None

    for result in results:
        if not isinstance(result, dict) or not geocodio_result_is_precise(
            result,
            allow_highway_exit_fallback=allow_highway_exit_fallback,
        ):
            continue
        location = result.get("location")
        if not isinstance(location, dict):
            continue
        latitude = location.get("lat")
        longitude = location.get("lng")
        if latitude is None or longitude is None:
            continue
        return {"lat": str(latitude), "lon": str(longitude)}
    return None


def geocodio_result_is_precise(
    result: dict[str, object],
    allow_highway_exit_fallback: bool = False,
) -> bool:
    accuracy_type = str(result.get("accuracy_type", "")).casefold()
    if accuracy_type in GEOCODIO_IMPRECISE_ACCURACY_TYPES:
        return False

    try:
        accuracy = Decimal(str(result.get("accuracy", "0")))
    except InvalidOperation:
        return False
    if accuracy >= GEOCODIO_MIN_ACCURACY:
        return True
    return (
        allow_highway_exit_fallback
        and accuracy >= GEOCODIO_HIGHWAY_EXIT_MIN_ACCURACY
        and accuracy_type in GEOCODIO_HIGHWAY_EXIT_ACCURACY_TYPES
    )


def has_highway_exit_address(station: FuelStation) -> bool:
    address = clean(station.address)
    return bool(
        HIGHWAY_EXIT_PATTERN.search(address)
        and HIGHWAY_ROUTE_PATTERN.search(address)
    )


def nominatim_url(query: str) -> str:
    params = parse.urlencode(
        {
            "q": query,
            "format": "jsonv2",
            "limit": "1",
            "countrycodes": "us",
        }
    )
    return f"https://nominatim.openstreetmap.org/search?{params}"
