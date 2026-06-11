from __future__ import annotations

from django.db.models import QuerySet
from rest_framework import status
from rest_framework.generics import CreateAPIView, ListAPIView
from rest_framework.request import Request

from common.response_mixins import BaseAPIView
from .models import FuelStation
from .serializers import FuelStationSerializer, RoutePlanRequestSerializer
from .services import RoutePlannerError


class RoutePlanCreateAPIView(BaseAPIView, CreateAPIView):
    serializer_class = RoutePlanRequestSerializer

    def create(self, request: Request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return self.send_bad_request_response(message=serializer.errors)

        try:
            result = serializer.save()
        except RoutePlannerError as exc:
            return self.send_response(
                success=False,
                status_code=getattr(exc, "status_code", status.HTTP_500_INTERNAL_SERVER_ERROR),
                message=str(exc),
                data=None,
            )

        return self.send_success_response(
            message="Route plan created successfully",
            data=result,
        )


class FuelStationListAPIView(BaseAPIView, ListAPIView):
    serializer_class = FuelStationSerializer

    def get_queryset(self) -> QuerySet[FuelStation]:
        queryset = FuelStation.objects.all()
        state = self.request.query_params.get("state")
        geocoded = self.request.query_params.get("geocoded")

        if state:
            queryset = queryset.filter(state=state.upper())
        if geocoded == "true":
            queryset = queryset.filter(latitude__isnull=False, longitude__isnull=False)
        elif geocoded == "false":
            queryset = queryset.filter(latitude__isnull=True, longitude__isnull=True)

        return queryset.order_by("retail_price", "id")

    def list(self, request: Request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.serializer_class(queryset, many=True)
        return self.send_success_response(data=serializer.data)


class HealthAPIView(BaseAPIView):
    def get(self, request: Request):
        return self.send_success_response(data={"status": "ok"})
