from django.urls import path

from .views import health_view, route_plan_view


urlpatterns = [
    path("health/", health_view, name="route-planner-health"),
    path("routes/", route_plan_view, name="route-plan"),
]
