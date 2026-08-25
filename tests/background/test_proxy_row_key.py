"""The durable row keys on the concrete model, like the state lock.

A proxy model and the model it proxies are one physical row and one
state column. The lock already sees through the class name; these tests
pin that the ``TransitionMessage`` key does too, so two background
transitions cannot run on one row just because two class names address
it. The class the caller drove is recorded on ``proxy_model_label`` and
the worker restores that class.
"""
import importlib

from django.test import TestCase, override_settings

from django_logic.background.exceptions import AlreadyInProgress
from django_logic.background.models import TransitionMessage

_migration_0010 = importlib.import_module(
    'django_logic.background.migrations.0010_proxy_model_label'
)
normalize_uncompleted_proxy_rows = _migration_0010.normalize_uncompleted_proxy_rows
restore_proxy_keys = _migration_0010.restore_proxy_keys
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

    def test_a_row_written_before_the_column_still_restores_the_proxy(self):
        """A pre-0.18 row carries the proxy's own name in the key columns
        and no ``proxy_model_label``. The worker must restore it exactly
        as before."""
        row = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widgetproxya',
            instance_id=str(self.widget.pk),
            process_name='process',
            field_name='status',
            transition_name='audit_via_proxy',
            queue_name='django_logic',
            kwargs={},
        )
        run_background_transition(row.pk)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.se_log, 'WidgetProxyA,')
        row.refresh_from_db()
        self.assertTrue(row.is_completed)


class NormalizeUncompletedProxyRowsTests(TestCase):
    """The forward data migration rewrites uncompleted proxy-keyed rows."""

    def setUp(self):
        self.widget = Widget.objects.create(status='draft')
        self.proxy_a = WidgetProxyA.objects.get(pk=self.widget.pk)

    def _proxy_keyed_row(self, **overrides):
        values = dict(
            app_label='bg_tests',
            model_name='widgetproxya',
            instance_id=str(self.widget.pk),
            process_name='process',
            field_name='status',
            transition_name='audit_via_proxy',
            queue_name='django_logic',
            kwargs={},
        )
        values.update(overrides)
        return TransitionMessage.objects.create(**values)

    def _run(self):
        from django.apps import apps
        normalize_uncompleted_proxy_rows(apps, None)

    def test_an_uncompleted_proxy_row_moves_to_the_concrete_key(self):
        row = self._proxy_keyed_row()
        self._run()
        row.refresh_from_db()
        self.assertEqual(row.model_name, 'widget')
        self.assertEqual(row.proxy_model_label, 'bg_tests.widgetproxya')

    def test_a_completed_row_is_left_alone(self):
        row = self._proxy_keyed_row(is_completed=True)
        self._run()
        row.refresh_from_db()
        self.assertEqual(row.model_name, 'widgetproxya')
        self.assertEqual(row.proxy_model_label, '')

    def test_a_name_the_registry_cannot_resolve_is_left_alone(self):
        row = self._proxy_keyed_row(model_name='goneproxy')
        self._run()
        row.refresh_from_db()
        self.assertEqual(row.model_name, 'goneproxy')

    def test_a_concrete_keyed_row_is_left_alone(self):
        row = self._proxy_keyed_row(model_name='widget')
        self._run()
        row.refresh_from_db()
        self.assertEqual(row.proxy_model_label, '')

    def test_a_clash_with_an_existing_concrete_row_is_skipped(self):
        """Two uncompleted rows for one physical row — rewriting both
        would violate the unique constraint, so the proxy row stays."""
        TransitionMessage.objects.create(
            **TransitionMessage.instance_key(self.widget, 'process'),
            transition_name='fulfil',
            queue_name='django_logic',
            kwargs={},
        )
        row = self._proxy_keyed_row()
        self._run()
        row.refresh_from_db()
        self.assertEqual(row.model_name, 'widgetproxya')
        self.assertEqual(row.proxy_model_label, '')

    def test_the_reverse_puts_the_proxy_key_back(self):
        row = self._proxy_keyed_row()
        self._run()
        from django.apps import apps
        restore_proxy_keys(apps, None)
        row.refresh_from_db()
        self.assertEqual(row.model_name, 'widgetproxya')
        self.assertEqual(row.proxy_model_label, '')

    def test_the_reverse_skips_a_clash_on_the_old_key(self):
        """An uncompleted row already holds the proxy key — the labeled
        row keeps its concrete key instead of violating the constraint."""
        self._proxy_keyed_row()
        labeled = TransitionMessage.objects.create(
            **TransitionMessage.instance_key(self.proxy_a, 'process'),
            proxy_model_label='bg_tests.widgetproxya',
            transition_name='fulfil_via_proxy',
            queue_name='django_logic',
            kwargs={},
        )
        from django.apps import apps
        restore_proxy_keys(apps, None)
        labeled.refresh_from_db()
        self.assertEqual(labeled.model_name, 'widget')
        self.assertEqual(labeled.proxy_model_label, 'bg_tests.widgetproxya')


class SnapshotProxyRoundTripTests(TestCase):
    """A snapshot replays the class production drove, no matter which
    class the snapshot was taken through."""

    def test_a_snapshot_through_the_concrete_class_keeps_the_proxy(self):
        from django_logic.testing.snapshot import from_snapshot, snapshot

        widget = Widget.objects.create(status='draft')
        proxy_a = WidgetProxyA.objects.get(pk=widget.pk)
        open_transition_message(
            proxy_a, transition_name='audit_via_proxy', field_name='status')
        data = snapshot(widget)
        TransitionMessage.objects.all().delete()
        widget.delete()

        from_snapshot(data, model=Widget)
        row = TransitionMessage.objects.get()
        self.assertEqual(row.model_name, 'widget')
        self.assertEqual(row.proxy_model_label, 'bg_tests.widgetproxya')

    def test_a_legacy_snapshot_derives_the_label_from_the_instance(self):
        from django_logic.testing.snapshot import from_snapshot, snapshot

        widget = Widget.objects.create(status='draft')
        proxy_a = WidgetProxyA.objects.get(pk=widget.pk)
        open_transition_message(
            proxy_a, transition_name='audit_via_proxy', field_name='status')
        data = snapshot(proxy_a)
        del data['transition_message']['proxy_model_label']
        TransitionMessage.objects.all().delete()
        widget.delete()

        from_snapshot(data, model=WidgetProxyA)
        row = TransitionMessage.objects.get()
        self.assertEqual(row.proxy_model_label, 'bg_tests.widgetproxya')
