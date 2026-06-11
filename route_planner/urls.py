from django.urls import path

from .views import FuelStationListAPIView, HealthAPIView, RoutePlanCreateAPIView


urlpatterns = [
    path("health/", HealthAPIView.as_view(), name="route-planner-health"),
    path("routes/", RoutePlanCreateAPIView.as_view(), name="route-plan"),
    path("fuel-stations/", FuelStationListAPIView.as_view(), name="fuel-station-list"),
]
