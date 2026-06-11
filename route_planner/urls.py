from django.urls import path

from .views import HealthAPIView, RoutePlanCreateAPIView


urlpatterns = [
    path("health/", HealthAPIView.as_view(), name="route-planner-health"),
    path("routes/", RoutePlanCreateAPIView.as_view(), name="route-plan"),
]
