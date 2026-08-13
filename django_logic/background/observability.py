"""Per-transition monitoring identity for background transitions.

All background transitions run under the single Celery task
``django_logic.run_background_transition``, so monitoring tools group them
together by default — a failing export transition can't be told apart from a
failing client transition. These helpers restore per-transition identity:

* :func:`task_label` — a human-readable label used as the Celery ``shadow`` on
  dispatch, so Flower / RabbitMQ management / Celery events show a distinct
  name per transition (vendor-neutral; no dependency).
* :func:`set_sentry_context` — if ``sentry-sdk`` is installed, name the Sentry
  transaction and tag it per transition, so each transition is its own Sentry
  issue. Sentry groups by the Celery task *name* (not ``shadow``), so this
  explicit naming is what splits the issues.

Both are best-effort and never affect transition execution.
"""
from __future__ import annotations


def task_label(transition_message) -> str:
    """Stable, readable per-transition label, e.g. ``django_logic.orders.fulfill``."""
    return (
        f'django_logic.{transition_message.app_label}.'
        f'{transition_message.transition_name}'
    )


def set_sentry_context(transition_message) -> None:
    """Name + tag the current Sentry scope per transition. No-op if sentry-sdk
    is absent. Never raises."""
    try:
        import sentry_sdk

        scope = sentry_sdk.get_current_scope()
        scope.set_transaction_name(
            task_label(transition_message), source='custom')
        scope.set_tag('dl.app', transition_message.app_label)
        scope.set_tag('dl.model', transition_message.model_name)
        scope.set_tag('dl.transition', transition_message.transition_name)
        scope.set_tag('dl.instance_id', transition_message.instance_id)
        scope.set_tag('dl.queue', transition_message.queue_name)
    except Exception:
        pass
