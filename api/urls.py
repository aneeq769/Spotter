from django.urls import path
from api.views import RouteView, HealthCheckView

urlpatterns = [
    path("route/", RouteView.as_view(), name="route"),
    path("health/", HealthCheckView.as_view(), name="health"),
]
