from __future__ import annotations

import csv
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib import parse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from route_planner.models import FuelStation
from route_planner.services import _get_json


PRICE_PRECISION = Decimal("0.0001")
COORDINATE_PRECISION = Decimal("0.0000001")
NOMINATIM_DELAY_SECONDS = 1.0
PROGRESS_INTERVAL = 25
NOMINATIM_SOURCE = "nominatim"
NOMINATIM_NO_RESULT_SOURCE = "nominatim-no-result"
PROCESSED_GEOCODE_SOURCES = (NOMINATIM_SOURCE, NOMINATIM_NO_RESULT_SOURCE)


class Command(BaseCommand):
    help = "Import the assessment fuel price CSV and geocode new fuel stations."

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
                f"Imported {created} new and updated {updated} fuel stations "
                f"from {csv_path.name}; skipped {skipped} invalid rows."
            )
        )

        geocoded = geocode_missing_stations(
            stdout=self.stdout,
            success_style=self.style.SUCCESS,
            warning_style=self.style.WARNING,
        )
        self.stdout.write(
            self.style.SUCCESS(f"Geocoded {geocoded} fuel stations with Nominatim.")
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


def read_station_rows(csv_path: Path) -> tuple[dict[str, dict[str, str | Decimal | bool]], int]:
    rows: dict[str, dict[str, str | Decimal | bool]] = {}
    skipped = 0

    with csv_path.open(newline="", encoding="utf-8") as file_obj:
        for row_number, raw_row in enumerate(csv.DictReader(file_obj), start=1):
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


def geocode_missing_stations(stdout, success_style, warning_style) -> int:
    already_geocoded = FuelStation.objects.filter(
        is_active=True,
        latitude__isnull=False,
        longitude__isnull=False,
    ).count()
    if already_geocoded:
        stdout.write(success_style(f"Skipped {already_geocoded} already geocoded stations."))

    missing = FuelStation.objects.filter(
        is_active=True,
        latitude__isnull=True,
        longitude__isnull=True,
    )
    pending = missing.exclude(geocode_source__in=PROCESSED_GEOCODE_SOURCES).order_by("id")

    skipped_processed = missing.count() - pending.count()
    if skipped_processed:
        stdout.write(success_style(f"Skipped {skipped_processed} previously processed stations."))

    total = pending.count()
    if total == 0:
        stdout.write(success_style("No fuel stations are missing coordinates."))
        return 0

    stdout.write(
        f"Geocoding {total} fuel stations with Nominatim "
        f"(delay={NOMINATIM_DELAY_SECONDS:g}s)."
    )

    geocoded = 0
    missing_result = 0
    for index, station in enumerate(pending.iterator(chunk_size=100), start=1):
        location = geocode_station(station)
        if location is None:
            mark_without_coordinates(station)
            missing_result += 1
            status = "no result"
        else:
            save_coordinates(station, location)
            geocoded += 1
            status = "geocoded"

        if should_report_progress(index, total):
            stdout.write(
                f"[{index}/{total}] {status}: "
                f"{station.truckstop_name} ({station.city}, {station.state})"
            )

        if index < total:
            time.sleep(NOMINATIM_DELAY_SECONDS)

    if missing_result:
        stdout.write(
            warning_style(
                f"Nominatim returned no result for {missing_result} fuel stations. "
                "They were marked and will be skipped on the next import."
            )
        )

    return geocoded


def geocode_station(station: FuelStation) -> dict[str, object] | None:
    queries = station_geocode_queries(station)
    for index, query in enumerate(queries):
        if index:
            time.sleep(NOMINATIM_DELAY_SECONDS)

        payload = _get_json(nominatim_url(query), timeout=station_geocode_timeout())
        if payload:
            return payload[0]

    return None


def station_geocode_queries(station: FuelStation) -> list[str]:
    queries = [
        geocode_query(station.address, station.city, station.state, "USA"),
        geocode_query(station.truckstop_name, station.city, station.state, "USA"),
    ]

    unique_queries = []
    seen = set()
    for query in queries:
        key = query.casefold()
        if query and key not in seen:
            unique_queries.append(query)
            seen.add(key)

    return unique_queries


def save_coordinates(station: FuelStation, payload: dict[str, object]) -> None:
    station.latitude = Decimal(str(payload["lat"])).quantize(COORDINATE_PRECISION)
    station.longitude = Decimal(str(payload["lon"])).quantize(COORDINATE_PRECISION)
    station.geocode_source = NOMINATIM_SOURCE
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
    station.geocode_source = NOMINATIM_NO_RESULT_SOURCE
    station.geocoded_at = timezone.now()
    station.save(update_fields=["geocode_source", "geocoded_at", "updated_at"])


def should_report_progress(index: int, total: int) -> bool:
    return index == 1 or index == total or index % PROGRESS_INTERVAL == 0


def station_geocode_timeout() -> float:
    return getattr(settings, "ROUTE_PLANNER_STATION_GEOCODE_TIMEOUT_SECONDS", 6)


def geocode_query(*parts: object) -> str:
    return ", ".join(part for part in (clean(part) for part in parts) if part)


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


def clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())
