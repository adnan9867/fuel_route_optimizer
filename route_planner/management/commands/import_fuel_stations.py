from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from route_planner.models import FuelStation


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


def clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())
