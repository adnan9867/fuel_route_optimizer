# Generated for the route detection assessment.

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="FuelStation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("opis_truckstop_id", models.CharField(db_index=True, max_length=32)),
                ("truckstop_name", models.CharField(max_length=255)),
                ("address", models.CharField(blank=True, max_length=255)),
                ("city", models.CharField(db_index=True, max_length=120)),
                ("state", models.CharField(db_index=True, max_length=2)),
                ("rack_id", models.CharField(blank=True, max_length=32)),
                ("retail_price", models.DecimalField(db_index=True, decimal_places=4, max_digits=7)),
                ("latitude", models.DecimalField(blank=True, db_index=True, decimal_places=7, max_digits=10, null=True)),
                ("longitude", models.DecimalField(blank=True, db_index=True, decimal_places=7, max_digits=10, null=True)),
                ("geocode_source", models.CharField(blank=True, max_length=50)),
                ("geocoded_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["retail_price", "id"]},
        ),
        migrations.CreateModel(
            name="GeocodeCache",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("query", models.CharField(max_length=255, unique=True)),
                ("latitude", models.DecimalField(decimal_places=7, max_digits=10)),
                ("longitude", models.DecimalField(decimal_places=7, max_digits=10)),
                ("display_name", models.CharField(blank=True, max_length=500)),
                ("provider", models.CharField(default="nominatim", max_length=50)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["query"]},
        ),
        migrations.CreateModel(
            name="RouteCache",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("cache_key", models.CharField(max_length=64, unique=True)),
                ("start_latitude", models.DecimalField(decimal_places=7, max_digits=10)),
                ("start_longitude", models.DecimalField(decimal_places=7, max_digits=10)),
                ("finish_latitude", models.DecimalField(decimal_places=7, max_digits=10)),
                ("finish_longitude", models.DecimalField(decimal_places=7, max_digits=10)),
                ("distance_miles", models.FloatField()),
                ("duration_minutes", models.FloatField()),
                ("geometry", models.JSONField()),
                ("provider", models.CharField(default="osrm", max_length=50)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["-updated_at"]},
        ),
        migrations.AddIndex(
            model_name="fuelstation",
            index=models.Index(fields=["state", "retail_price"], name="route_plann_state_93e91e_idx"),
        ),
        migrations.AddIndex(
            model_name="fuelstation",
            index=models.Index(fields=["latitude", "longitude"], name="route_plann_latitud_6f4d4e_idx"),
        ),
    ]
