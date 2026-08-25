from django.apps import AppConfig


class DjangoLogicConfig(AppConfig):
    """App-level bootstrap for the base ``django_logic`` app.

    The background app (``django_logic.background``) performs the same
    bootstrap in its own ``ready()`` — both are idempotent — but a
    sync-only consumer that installs just ``django_logic`` must still get
    the system checks, so they live here too.
    """
    name = 'django_logic'

    def ready(self) -> None:
        from django_logic import checks  # noqa: F401 — registers system checks
        from django_logic.conf import validate_core_settings

        # Core knobs (LOCK_TIMEOUT) are used by the engine with or without
        # the background app installed — a sync-only install must fail
        # fast on misconfiguration too. The background app's
        # validate_on_ready() re-runs this as part of its full gate; both
        # are idempotent.
        validate_core_settings()
