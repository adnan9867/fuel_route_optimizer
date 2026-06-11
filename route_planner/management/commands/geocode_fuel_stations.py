from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib import parse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from route_planner.models import FuelStation
from route_planner.services import _get_json


@dataclass(frozen=True, slots=True)
class Point:
    latitude: Decimal
    longitude: Decimal


class Command(BaseCommand):
    help = "Fill missing FuelStation latitude/longitude values once and store them in the database."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--provider",
            choices=["city-centroid", "nominatim"],
            default="city-centroid",
            help=(
                "city-centroid is fast and deterministic for the assignment dataset; "
                "nominatim geocodes full station addresses but is rate-limited."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of stations to geocode in this run.",
        )
        parser.add_argument(
            "--sleep",
            type=float,
            default=1.0,
            help="Delay between Nominatim calls.",
        )
        parser.add_argument(
            "--centroids",
            default=str(settings.BASE_DIR / "route_planner" / "data" / "us_city_centroids.csv"),
            help="Local city/state centroid CSV used by the city-centroid provider.",
        )

    def handle(self, *args, **options) -> None:
        queryset = FuelStation.objects.filter(latitude__isnull=True, longitude__isnull=True)
        if options["limit"]:
            queryset = queryset[: options["limit"]]

        if options["provider"] == "city-centroid":
            updated = self._from_city_centroids(queryset, Path(options["centroids"]))
        else:
            updated = self._from_nominatim(queryset, sleep_seconds=options["sleep"])

        self.stdout.write(self.style.SUCCESS(f"Geocoded {updated} fuel stations."))

    def _from_city_centroids(self, queryset, path: Path) -> int:
        if not path.exists():
            raise CommandError(f"Centroid file not found: {path}")

        centroids = load_centroids(path)
        updates: list[FuelStation] = []
        now = timezone.now()

        for station in queryset.iterator(chunk_size=1000):
            point = centroids.get((station.city.upper(), station.state.upper()))
            if point is None:
                continue
            station.latitude = point.latitude
            station.longitude = point.longitude
            station.geocode_source = "city-centroid"
            station.geocoded_at = now
            station.updated_at = now
            updates.append(station)

        FuelStation.objects.bulk_update(
            updates,
            ["latitude", "longitude", "geocode_source", "geocoded_at", "updated_at"],
            batch_size=1000,
        )
        return len(updates)

    def _from_nominatim(self, queryset, sleep_seconds: float) -> int:
        updated = 0
        for station in queryset.iterator(chunk_size=100):
            query = ", ".join(
                part
                for part in [station.address, station.city, station.state, "USA"]
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
            payload = _get_json(
                f"https://nominatim.openstreetmap.org/search?{params}",
                timeout=15,
            )
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
            time.sleep(sleep_seconds)
        return updated


def load_centroids(path: Path) -> dict[tuple[str, str], Point]:
    centroids: dict[tuple[str, str], Point] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            centroids[(row["city"].upper(), row["state"].upper())] = Point(
                latitude=Decimal(row["latitude"]).quantize(Decimal("0.0000001")),
                longitude=Decimal(row["longitude"]).quantize(Decimal("0.0000001")),
            )
    return centroids
