"""Per-transition monitoring identity for background transitions.

Every background transition runs through one shared execute path, so
monitoring tools group them together by default — a failing export
transition can't be told apart from a failing client transition.
:func:`set_sentry_context` restores per-transition identity: if
``sentry-sdk`` is installed, it names the Sentry transaction and tags it
per transition, so each transition is its own Sentry issue. Best-effort;
never affects transition execution.
"""
from __future__ import annotations


def set_sentry_context(transition_message) -> None:
    """Name + tag the current Sentry scope per transition. No-op if sentry-sdk
    is absent. Never raises."""
    try:
        import sentry_sdk

        # Tag the class the caller drove, not the concrete key: two
        # workflows behind two proxies of one model must stay two Sentry
        # issues.
        app_label, _, model_name = (
            transition_message.driving_model_label.partition('.'))
        scope = sentry_sdk.get_current_scope()
        scope.set_transaction_name(
            f'django_logic.{app_label}.'
            f'{transition_message.transition_name}',
            source='custom')
        scope.set_tag('dl.app', app_label)
        scope.set_tag('dl.model', model_name)
        scope.set_tag('dl.transition', transition_message.transition_name)
        scope.set_tag('dl.instance_id', transition_message.instance_id)
        scope.set_tag('dl.queue', transition_message.queue_name)
    except Exception:
        pass
