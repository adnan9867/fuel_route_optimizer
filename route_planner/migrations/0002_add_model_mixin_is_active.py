# Generated for the route detection assessment.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("route_planner", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="fuelstation",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="geocodecache",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="routecache",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
    ]
