from __future__ import annotations

from rest_framework import serializers

from .models import FuelStation
from .services import plan_route


class RoutePlanRequestSerializer(serializers.Serializer):
    start_location = serializers.CharField(max_length=255, trim_whitespace=True)
    finish_location = serializers.CharField(max_length=255, trim_whitespace=True)

    def create(self, validated_data):
        return plan_route(
            validated_data["start_location"],
            validated_data["finish_location"],
        )


class FuelStationSerializer(serializers.ModelSerializer):
    class Meta:
        model = FuelStation
        fields = [
            "id",
            "opis_truckstop_id",
            "truckstop_name",
            "address",
            "city",
            "state",
            "rack_id",
            "retail_price",
            "latitude",
            "longitude",
            "geocode_source",
            "is_active",
        ]
