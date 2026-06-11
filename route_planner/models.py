from __future__ import annotations

from django.db import models

from common.model_mixins import TimestampMixin


class FuelStation(TimestampMixin):
    opis_truckstop_id = models.CharField(max_length=32, db_index=True)
    truckstop_name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=120, db_index=True)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.CharField(max_length=32, blank=True)
    retail_price = models.DecimalField(max_digits=7, decimal_places=4, db_index=True)
    latitude = models.DecimalField(
        max_digits=10,
        decimal_places=7,
        null=True,
        blank=True,
        db_index=True,
    )
    longitude = models.DecimalField(
        max_digits=10,
        decimal_places=7,
        null=True,
        blank=True,
        db_index=True,
    )
    geocode_source = models.CharField(max_length=50, blank=True)
    geocoded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["state", "retail_price"],
                name="route_plann_state_93e91e_idx",
            ),
            models.Index(
                fields=["latitude", "longitude"],
                name="route_plann_latitud_6f4d4e_idx",
            ),
        ]
        ordering = ["retail_price", "id"]

    def __str__(self) -> str:
        return f"{self.truckstop_name} ({self.city}, {self.state})"


class GeocodeCache(TimestampMixin):
    query = models.CharField(max_length=255, unique=True)
    latitude = models.DecimalField(max_digits=10, decimal_places=7)
    longitude = models.DecimalField(max_digits=10, decimal_places=7)
    display_name = models.CharField(max_length=500, blank=True)
    provider = models.CharField(max_length=50, default="nominatim")

    class Meta:
        ordering = ["query"]

    def __str__(self) -> str:
        return self.query


class RouteCache(TimestampMixin):
    cache_key = models.CharField(max_length=64, unique=True)
    start_latitude = models.DecimalField(max_digits=10, decimal_places=7)
    start_longitude = models.DecimalField(max_digits=10, decimal_places=7)
    finish_latitude = models.DecimalField(max_digits=10, decimal_places=7)
    finish_longitude = models.DecimalField(max_digits=10, decimal_places=7)
    distance_miles = models.FloatField()
    duration_minutes = models.FloatField()
    geometry = models.JSONField()
    provider = models.CharField(max_length=50, default="osrm")

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return self.cache_key
