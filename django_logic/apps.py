from django.apps import AppConfig


class DjangoLogicConfig(AppConfig):
    """App-level bootstrap for the base ``django_logic`` app.

    The background app (``django_logic.background``) performs the same
    bootstrap in its own ``ready()`` — both are idempotent — but a
    sync-only consumer that installs just ``django_logic`` must still get
    the system checks and coverage recording, so they live here too.
    """
    name = 'django_logic'

    def ready(self) -> None:
        from django_logic import checks  # noqa: F401 — registers system checks
        from django_logic.conf import (
            install_legacy_exception_base,
            transition_coverage_log,
            validate_core_settings,
        )

        # Core knobs (LOCK_TIMEOUT, DEFER_UNLOCK_UNTIL_COMMIT) are used by
        # the engine with or without the background app installed — a
        # sync-only install must fail fast on misconfiguration too. The
        # background app's validate_on_ready() re-runs this as part of its
        # full gate; both are idempotent.
        validate_core_settings()
        install_legacy_exception_base()

        # Transition-coverage recording. Activated in ready() so
        # spawn-based parallel test workers, which re-run it, self-activate;
        # fork-based workers inherit the parent's recorder. The path is
        # type-validated by validate_core_settings above — open() accepts a
        # bool as a file descriptor, so True would append coverage lines to
        # stdout.
        coverage_log = transition_coverage_log()
        if coverage_log:
            from django_logic.coverage import start_file_recording
            start_file_recording(coverage_log)
