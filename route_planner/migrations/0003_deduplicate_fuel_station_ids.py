# Generated for the route detection assessment.

from django.db import migrations, models
from django.db.models import Count


def deduplicate_truckstop_ids(apps, schema_editor):
    fuel_station = apps.get_model("route_planner", "FuelStation")

    for station in fuel_station.objects.filter(opis_truckstop_id="").only(
        "id",
        "opis_truckstop_id",
    ):
        station.opis_truckstop_id = f"row-{station.id}"
        station.save(update_fields=["opis_truckstop_id"])

    duplicate_ids = (
        fuel_station.objects.values("opis_truckstop_id")
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
    )

    for item in duplicate_ids:
        truckstop_id = item["opis_truckstop_id"]
        stations = list(
            fuel_station.objects.filter(opis_truckstop_id=truckstop_id).order_by(
                "retail_price",
                "id",
            )
        )
        fuel_station.objects.filter(
            opis_truckstop_id=truckstop_id,
        ).exclude(id=stations[0].id).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("route_planner", "0002_add_model_mixin_is_active"),
    ]

    operations = [
        migrations.RunPython(deduplicate_truckstop_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="fuelstation",
            name="opis_truckstop_id",
            field=models.CharField(max_length=32, unique=True),
        ),
    ]
