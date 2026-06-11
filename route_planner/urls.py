from django.urls import path

from .views import RoutePlanCreateAPIView


urlpatterns = [
    path("routes/", RoutePlanCreateAPIView.as_view(), name="route-plan"),
]
