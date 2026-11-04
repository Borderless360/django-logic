from django.core.exceptions import ImproperlyConfigured


def validate_on_ready() -> None:
    """The app's boot gate — fail fast on a misconfigured settings block."""
    from django_logic import conf

    mode = conf.background_execution()
    # Surface value errors now rather than on first use.
    conf.default_queue()
    conf.job_process_finish()
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
    # (pull_mode_needs_postgresql, pull_mode_needs_a_shared_cache):
    # they apply only when a background
    # transition is bound, and bindings happen in consumer apps'
    # ready() hooks, after this one runs.


def _reject_sync_without_opt_in() -> None:
    """Sync runs the worker path inline, in the caller's own thread.

    Require an explicit opt-in before a settings value makes all background work run inline.
    A scoped ``sync_execution()`` block remains available to business code without this deployment-wide opt-in.
    """
    from django_logic import conf

    if conf.sync_enabled():
        return
    raise ImproperlyConfigured(
        "DJANGO_LOGIC['BACKGROUND_EXECUTION']='sync' runs background "
        "side-effects inline, in the caller's own thread, and nothing "
        "retries what fails. Use 'pull' as the deployment default. "
        "For a test runtime, call "
        "django_logic.conf.enable_sync() in it before Django boots. To run "
        "one block inline elsewhere, use django_logic.conf.sync_execution()."
    )
