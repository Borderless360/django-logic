"""The durable row keys on the concrete model, like the state lock.

A proxy model and the model it proxies are one physical row and one
state column. The lock already sees through the class name; these tests
pin that the ``TransitionMessage`` key does too, so two background
transitions cannot run on one row just because two class names address
it. The class the caller drove is recorded on ``proxy_model_label`` and
the worker restores that class.
"""
from django.test import TestCase, override_settings

from django_logic.background.exceptions import AlreadyInProgress
from django_logic.background.models import TransitionMessage

from django_logic.background.runner import run_background_transition
from django_logic.exceptions import TransitionTemporarilyUnavailable
from django_logic.testing import open_transition_message
from tests import dl_settings
from tests.background.models import Widget, WidgetProxyA, WidgetProxyB


_PULL_SETTINGS = dl_settings(BACKGROUND_EXECUTION='pull')


class ProxyRowKeyTests(TestCase):
    def setUp(self):
        self.widget = Widget.objects.create(status='draft')
        self.proxy_a = WidgetProxyA.objects.get(pk=self.widget.pk)
        self.proxy_b = WidgetProxyB.objects.get(pk=self.widget.pk)

    def test_row_keys_on_the_concrete_model_and_records_the_proxy(self):
        self.proxy_a.process.fulfil_via_proxy()
        row = TransitionMessage.objects.get()
        self.assertEqual(row.app_label, 'bg_tests')
        self.assertEqual(row.model_name, 'widget')
        self.assertEqual(row.proxy_model_label, 'bg_tests.widgetproxya')

    def test_a_concrete_driven_row_records_no_proxy(self):
        self.widget.process.fulfil()
        row = TransitionMessage.objects.get()
        self.assertEqual(row.model_name, 'widget')
        self.assertEqual(row.proxy_model_label, '')

    def test_the_worker_restores_the_proxy_class(self):
        self.proxy_a.process.fulfil_via_proxy()
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'proxy_fulfilled')
        self.assertEqual(self.widget.se_log, 'WidgetProxyA,')
        self.assertTrue(TransitionMessage.objects.get().is_completed)

    @override_settings(DJANGO_LOGIC=_PULL_SETTINGS)
    def test_sequential_enqueues_through_two_proxies_collide(self):
        self.proxy_a.process.audit_via_proxy()
        with self.assertRaises(AlreadyInProgress):
            self.proxy_b.process.audit_via_proxy()
        self.assertEqual(TransitionMessage.objects.count(), 1)

    def test_a_proxy_row_gates_the_concrete_model_too(self):
        open_transition_message(self.proxy_a, transition_name='audit_via_proxy')
        with self.assertRaises(TransitionTemporarilyUnavailable):
            self.proxy_b.process.touch()

    def test_probes_see_the_row_from_every_class(self):
        open_transition_message(self.proxy_a, transition_name='audit_via_proxy')
        for instance in (self.widget, self.proxy_a, self.proxy_b):
            with self.subTest(driving=type(instance).__name__):
                self.assertTrue(
                    TransitionMessage.in_flight_for(instance, 'process').exists()
                )

    def test_proxy_name_in_the_concrete_key_is_refused(self):
        row = TransitionMessage.objects.create(
            app_label='bg_tests', model_name='widgetproxya',
            instance_id=str(self.widget.pk), process_name='process', field_name='status',
            transition_name='audit_via_proxy', queue_name='django_logic',
            owning_process_class='tests.background.models.ProxyWidgetProcess',
            kwargs={'process_class': 'tests.background.models.ProxyWidgetProcess'},
        )
        run_background_transition(row.pk)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.se_log, '')
        row.refresh_from_db()
        self.assertTrue(row.ended_in_failure)
        self.assertIn('concrete model', row.last_error_message)
