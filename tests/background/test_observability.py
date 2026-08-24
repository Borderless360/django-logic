"""Per-transition observability helpers."""
from types import SimpleNamespace

from django.test import SimpleTestCase

from django_logic.background.observability import set_sentry_context


def _tm(app='orders', transition='fulfill'):
    return SimpleNamespace(
        app_label=app, model_name='order', transition_name=transition,
        instance_id='7', queue_name='django_logic.critical',
    )


class SentryContextTests(SimpleTestCase):
    def test_no_op_without_sentry_sdk(self):
        # sentry-sdk is not a dependency; the call must be a harmless no-op.
        set_sentry_context(_tm())  # must not raise
