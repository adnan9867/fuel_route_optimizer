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


NOMINATIM_SLEEP_SECONDS = 1.0
GEOCODE_PROGRESS_INTERVAL = 25


class Command(BaseCommand):
    help = "Import the provided fuel price CSV into the FuelStation table."

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

        stations_by_id: dict[str, dict[str, str | Decimal]] = {}
        skipped = 0
        with csv_path.open(newline="", encoding="utf-8") as handle:
            for row_number, row in enumerate(csv.DictReader(handle), start=1):
                try:
                    price = Decimal(clean(row["Retail Price"])).quantize(Decimal("0.0001"))
                except (KeyError, InvalidOperation):
                    skipped += 1
                    continue

                truckstop_id = clean(row.get("OPIS Truckstop ID")) or f"row-{row_number}"
                record = {
                    "truckstop_name": clean(row.get("Truckstop Name")) or "Unknown Truckstop",
                    "address": clean(row.get("Address")),
                    "city": clean(row.get("City")),
                    "state": clean(row.get("State")).upper(),
                    "rack_id": clean(row.get("Rack ID")),
                    "retail_price": price,
                    "is_active": True,
                }
                existing = stations_by_id.get(truckstop_id)
                if existing is None or price < existing["retail_price"]:
                    stations_by_id[truckstop_id] = record

        created = 0
        updated = 0
        for truckstop_id, defaults in stations_by_id.items():
            _, was_created = FuelStation.objects.update_or_create(
                opis_truckstop_id=truckstop_id,
                defaults=defaults,
            )
            if was_created:
                created += 1
            else:
                updated += 1

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
                f"Geocoded {geocoded} fuel stations with Nominatim."
            )
        )


def clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def geocode_missing_stations(stdout, success_style, warning_style) -> int:
    queryset = FuelStation.objects.filter(
        is_active=True,
        latitude__isnull=True,
        longitude__isnull=True,
    ).order_by("id")
    total = queryset.count()
    if total == 0:
        stdout.write(success_style("No fuel stations are missing coordinates."))
        return 0

    updated = 0
    stdout.write(
        f"Geocoding {total} fuel stations with Nominatim "
        f"(sleep={NOMINATIM_SLEEP_SECONDS}s between requests)."
    )
    for index, station in enumerate(queryset.iterator(chunk_size=100), start=1):
        payload = _get_json(nominatim_url(station), timeout=15)
        if payload:
            station.latitude = Decimal(payload[0]["lat"]).quantize(Decimal("0.0000001"))
            station.longitude = Decimal(payload[0]["lon"]).quantize(Decimal("0.0000001"))
            station.geocode_source = "nominatim"
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
            updated += 1
            status = "geocoded"
        else:
            status = "no result"

        if index == 1 or index == total or index % GEOCODE_PROGRESS_INTERVAL == 0:
            stdout.write(
                f"[{index}/{total}] {status}: "
                f"{station.truckstop_name} ({station.city}, {station.state})"
            )
        time.sleep(NOMINATIM_SLEEP_SECONDS)

    if updated < total:
        stdout.write(
            warning_style(
                f"Nominatim returned no result for {total - updated} fuel stations."
            )
        )
    return updated


def nominatim_url(station: FuelStation) -> str:
    query = ", ".join(
        part
        for part in [
            station.address,
            station.city,
            station.state,
            "USA",
        ]
        if part
    )
    params = parse.urlencode(
        {
            "q": query,
            "format": "jsonv2",
            "limit": "1",
            "countrycodes": "us",
        }
    )
    return f"https://nominatim.openstreetmap.org/search?{params}"
