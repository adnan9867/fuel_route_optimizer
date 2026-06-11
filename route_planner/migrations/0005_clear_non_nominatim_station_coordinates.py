# Generated for the route detection assessment.

from django.db import migrations


def clear_non_nominatim_station_coordinates(apps, schema_editor):
    fuel_station = apps.get_model("route_planner", "FuelStation")
    fuel_station.objects.exclude(geocode_source="nominatim").update(
        latitude=None,
        longitude=None,
        geocode_source="",
        geocoded_at=None,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("route_planner", "0004_normalize_geocode_cache_queries"),
    ]

    operations = [
        migrations.RunPython(
            clear_non_nominatim_station_coordinates,
            migrations.RunPython.noop,
        ),
    ]
