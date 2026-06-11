from __future__ import annotations

from django.db.models import QuerySet
from rest_framework import status
from rest_framework.generics import CreateAPIView, ListAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import FuelStation
from .serializers import FuelStationSerializer, RoutePlanRequestSerializer
from .services import RoutePlannerError


class RoutePlanCreateAPIView(CreateAPIView):
    serializer_class = RoutePlanRequestSerializer

    def create(self, request: Request, *args, **kwargs) -> Response:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            result = serializer.save()
        except RoutePlannerError as exc:
            return Response(
                {"error": str(exc)},
                status=getattr(exc, "status_code", status.HTTP_500_INTERNAL_SERVER_ERROR),
            )

        return Response(result, status=status.HTTP_200_OK)


class FuelStationListAPIView(ListAPIView):
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


class HealthAPIView(APIView):
    def get(self, request: Request) -> Response:
        return Response({"status": "ok"})
