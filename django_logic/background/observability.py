"""Per-transition monitoring identity for background transitions.

All background transitions run under the single Celery task
``django_logic.run_background_transition``, so monitoring tools group them
together by default — a failing export transition can't be told apart from a
failing client transition. These helpers restore per-transition identity:

* :func:`task_label` — a human-readable label used as the Celery ``shadow`` on
  dispatch, so Flower / RabbitMQ management / Celery events show a distinct
  name per transition (vendor-neutral; no dependency).
* :func:`set_sentry_context` — if ``sentry-sdk`` is installed (and not disabled
  via ``DJANGO_LOGIC['SENTRY_TRANSACTION_NAMING'] = False``), name the Sentry
  transaction and tag it per transition, so each transition is its own Sentry
  issue. Sentry groups by the Celery task *name* (not ``shadow``), so this
  explicit naming is what splits the issues.

Both are best-effort and never affect transition execution.
"""
from __future__ import annotations

from django_logic.background import settings as bg_settings


def task_label(tm) -> str:
    """Stable, readable per-transition label, e.g. ``django_logic.orders.fulfill``."""
    return f'django_logic.{tm.app_label}.{tm.transition_name}'


def set_sentry_context(tm) -> None:
    """Name + tag the current Sentry scope per transition. No-op if sentry-sdk
    is absent or disabled. Never raises."""
    if not bg_settings.sentry_transaction_naming():
        return
    try:
        import sentry_sdk

        scope = sentry_sdk.get_current_scope()
        scope.set_transaction_name(task_label(tm), source='custom')
        scope.set_tag('dl.app', tm.app_label)
        scope.set_tag('dl.model', tm.model_name)
        scope.set_tag('dl.transition', tm.transition_name)
        scope.set_tag('dl.instance_id', tm.instance_id)
        scope.set_tag('dl.queue', tm.queue_name)
    except Exception:
        pass
