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
        from django_logic.conf import (
            install_legacy_exception_base,
            transition_coverage_log,
        )

        # Idempotent — covers an install shape where only the background
        # app's ready() runs before denials are raised or caught (#190).
        install_legacy_exception_base()

        # Transition-coverage recording (#132). Activated here (not in the
        # recorder module) so spawn-based parallel test workers, which re-run
        # ready(), self-activate; fork-based workers inherit the parent's. The
        # path is type-validated by validate_on_ready above — open() accepts a
        # bool as a file descriptor, so True would append coverage lines to
        # stdout.
        coverage_log = transition_coverage_log()
        if coverage_log:
            from django_logic.coverage import start_file_recording
            start_file_recording(coverage_log)
