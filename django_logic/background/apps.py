from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured


def validate_on_ready() -> None:
    """The app's boot gate — fail fast on a misconfigured settings block."""
    from django_logic import conf

    mode = conf.background_execution()
    # Surface value errors now rather than on first use.
    conf.default_queue()
    # Safety settings: every numeric knob the retry/cleanup/lock
    # machinery depends on is validated at boot in EVERY mode — a bad
    # value must not wait for its first use (which may be a 3am retry
    # tick) to explode, or worse, silently misbehave.
    conf.max_errors()
    conf.retry_minutes()
    conf.cleanup_days()
    conf.validate_core_settings()
    if mode == conf.EXECUTION_SYNC:
        _reject_sync_without_opt_in()
    # The pull-mode database and cache rules are system checks
    # (django_logic.E004, E005): they apply only when a background
    # transition is bound, and bindings happen in consumer apps'
    # ready() hooks, after this one runs.


def _reject_sync_without_opt_in() -> None:
    """Sync runs the worker path inline, in the caller's own thread.

    In a web process that means every side-effect runs inside the
    request, and nothing retries what fails. It is a test runtime, so a
    deployment must not be able to choose it from a settings value or an
    environment variable. A test settings module opts in.
    """
    from django_logic import conf

    if conf.sync_enabled():
        return
    raise ImproperlyConfigured(
        "DJANGO_LOGIC['BACKGROUND_EXECUTION']='sync' runs background "
        "side-effects inline, in the caller's own thread, and nothing "
        "retries what fails. It is a test runtime. Production is always "
        "'pull'. If this is a test settings module, call "
        "django_logic.conf.enable_sync() in it before Django boots. To run "
        "one block inline elsewhere, use django_logic.conf.sync_execution()."
    )


class BackgroundConfig(AppConfig):
    """The retired second entry. It refuses to boot and names the fix."""
    name = 'django_logic.background'

    def __init__(self, *args, **kwargs):
        raise ImproperlyConfigured(
            "Since 1.1.0 'django_logic.background' is not a valid "
            "INSTALLED_APPS entry. Install 'django_logic' alone. It keeps "
            "the same app label, table and migration history, so nothing "
            "else changes: replace this entry (and a 'django_logic' "
            "duplicate, if present) with one 'django_logic' line."
        )
