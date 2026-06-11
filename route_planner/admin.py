from __future__ import annotations

from django.contrib import admin

from .models import FuelStation, GeocodeCache, RouteCache


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = (
        "opis_truckstop_id",
        "truckstop_name",
        "city",
        "state",
        "retail_price",
        "geocode_source",
        "is_active",
    )
    list_filter = ("state", "geocode_source", "is_active")
    search_fields = (
        "opis_truckstop_id",
        "truckstop_name",
        "address",
        "city",
        "state",
    )
    readonly_fields = ("created_at", "updated_at", "geocoded_at")
    ordering = ("retail_price", "state", "city")


@admin.register(GeocodeCache)
class GeocodeCacheAdmin(admin.ModelAdmin):
    list_display = ("query", "latitude", "longitude", "provider", "is_active")
    list_filter = ("provider", "is_active")
    search_fields = ("query", "display_name")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("query",)


@admin.register(RouteCache)
class RouteCacheAdmin(admin.ModelAdmin):
    list_display = (
        "cache_key",
        "distance_miles",
        "duration_minutes",
        "provider",
        "is_active",
        "updated_at",
    )
    list_filter = ("provider", "is_active")
    search_fields = ("cache_key",)
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-updated_at",)
