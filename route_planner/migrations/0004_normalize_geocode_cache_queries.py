# Generated for the route detection assessment.

from django.db import migrations


def normalize_geocode_cache_queries(apps, schema_editor):
    geocode_cache = apps.get_model("route_planner", "GeocodeCache")
    rows = list(geocode_cache.objects.order_by("-updated_at", "-id"))
    grouped = {}
    for row in rows:
        grouped.setdefault(row.query.casefold(), []).append(row)

    for normalized_query, caches in grouped.items():
        keep = caches[0]
        duplicate_ids = [cache.id for cache in caches[1:]]
        if duplicate_ids:
            geocode_cache.objects.filter(id__in=duplicate_ids).delete()
        if keep.query != normalized_query:
            keep.query = normalized_query
            keep.save(update_fields=["query"])


class Migration(migrations.Migration):
    dependencies = [
        ("route_planner", "0003_deduplicate_fuel_station_ids"),
    ]

    operations = [
        migrations.RunPython(
            normalize_geocode_cache_queries,
            migrations.RunPython.noop,
        ),
    ]
