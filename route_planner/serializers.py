from __future__ import annotations

from rest_framework import serializers

from .services import plan_route


class RoutePlanRequestSerializer(serializers.Serializer):
    start_location = serializers.CharField(max_length=255, trim_whitespace=True)
    finish_location = serializers.CharField(max_length=255, trim_whitespace=True)

    def create(self, validated_data):
        return plan_route(
            validated_data["start_location"],
            validated_data["finish_location"],
        )
