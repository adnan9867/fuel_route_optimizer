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

        stations: list[FuelStation] = []
        skipped = 0
        with csv_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    price = Decimal(clean(row["Retail Price"])).quantize(Decimal("0.0001"))
                except (KeyError, InvalidOperation):
                    skipped += 1
                    continue

                stations.append(
                    FuelStation(
                        opis_truckstop_id=clean(row.get("OPIS Truckstop ID")),
                        truckstop_name=clean(row.get("Truckstop Name")) or "Unknown Truckstop",
                        address=clean(row.get("Address")),
                        city=clean(row.get("City")),
                        state=clean(row.get("State")).upper(),
                        rack_id=clean(row.get("Rack ID")),
                        retail_price=price,
                    )
                )

        FuelStation.objects.bulk_create(stations, batch_size=1000)
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(stations)} fuel stations from {csv_path.name}; skipped {skipped} rows."
            )
        )


def clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())
