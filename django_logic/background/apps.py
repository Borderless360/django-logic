from django.apps import AppConfig


class BackgroundConfig(AppConfig):
    name = 'django_logic.background'
    label = 'django_logic_background'
    verbose_name = 'Django Logic — Background Transitions'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self) -> None:
        from django_logic.background.settings import validate_on_ready
        validate_on_ready()
        from django_logic import checks  # noqa: F401 — registers system checks
