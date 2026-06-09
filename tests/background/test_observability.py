"""Per-transition observability helpers (issue #78)."""
from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from django_logic.background.observability import set_sentry_context, task_label


def _tm(app='orders', transition='fulfill'):
    return SimpleNamespace(
        app_label=app, model_name='order', transition_name=transition,
        instance_id='7', queue_name='django_logic.critical',
    )


class TaskLabelTests(SimpleTestCase):
    def test_label_is_app_and_transition_scoped(self):
        self.assertEqual(task_label(_tm('orders', 'fulfill')), 'django_logic.orders.fulfill')
        self.assertEqual(task_label(_tm('exports', 'generate')), 'django_logic.exports.generate')
        # Distinct transitions → distinct labels (the whole point).
        self.assertNotEqual(
            task_label(_tm('exports', 'generate')),
            task_label(_tm('payments', 'charge')),
        )


class SentryContextTests(SimpleTestCase):
    def test_no_op_without_sentry_sdk(self):
        # sentry-sdk is not a dependency; the call must be a harmless no-op.
        set_sentry_context(_tm())  # must not raise

    @override_settings(DJANGO_LOGIC={'SENTRY_TRANSACTION_NAMING': False})
    def test_disabled_via_setting(self):
        set_sentry_context(_tm())  # must not raise; returns early
