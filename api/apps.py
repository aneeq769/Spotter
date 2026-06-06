from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "api"

    def ready(self) -> None:
        """
        Pre-load the fuel station CSV + KD-tree at startup so the first
        request isn't slow.
        """
        from utils.fuel_station_service import get_fuel_station_service  # noqa: PLC0415

        get_fuel_station_service()
