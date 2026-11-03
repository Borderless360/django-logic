"""Provider waiting preserves the declared retry and fairness rules."""
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from django_logic import Process, ProcessManager, Transition
from django_logic.background import BackgroundTransition, Defer
from django_logic.background.models import TransitionMessage
from django_logic.background.runner import run_background_transition
from django_logic.background.safety_nets import _claimable
from django_logic.exceptions import RefusalReason, TransitionTemporarilyUnavailable
from tests import dl_settings
from tests.background.models import Widget, bg_ok, bg_callback


def wait_for_provider(instance, **kwargs):
    bg_ok(instance)
    raise Defer(until=datetime.now(datetime_timezone.utc) + timedelta(hours=1), reason='provider allowance')


class OperationProcess(Process):
    process_name = 'operations'
    transitions = [
        BackgroundTransition('start', sources=['draft'], target='ready',
                             side_effects=[bg_ok], callbacks=[bg_callback]),
        BackgroundTransition('finish', sources=['ready'], target='done', side_effects=[bg_ok]),
        BackgroundTransition('wait', sources=['draft'], target='done', side_effects=[wait_for_provider],
                             callbacks=[bg_callback], failed_state='failed'),
        BackgroundTransition('plain', sources=['draft'], target='done', side_effects=[bg_ok], fairness_key='client:1'),
        Transition('sync_only', sources=['draft'], target='done'),
        BackgroundTransition('refresh', sources=['draft'], side_effects=[bg_ok]),
    ]


@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class OperationContractTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        ProcessManager.bind_model_process(Widget, OperationProcess, state_field='status')
        self.addCleanup(ProcessManager.unbind_model_process, Widget, OperationProcess)
        self.widget = Widget.objects.create()

    def enqueue(self, action='plain', **kwargs):
        getattr(self.widget.operations, action)(**kwargs)
        return TransitionMessage.in_flight_for(self.widget, 'operations').get()

    def test_completed_refresh_accepts_a_new_job(self):
        first = self.enqueue(action='refresh')
        run_background_transition(first.pk)
        second = self.enqueue(action='refresh')
        self.assertNotEqual(first.pk, second.pk)
        run_background_transition(second.pk)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.se_log, 'ok,ok,')
        self.assertEqual(TransitionMessage.objects.filter(is_completed=True).count(), 2)

    @override_settings(USE_TZ=True)
    def test_defer_rolls_back_atomic_effect_without_error_or_callbacks(self):
        result = self.enqueue(action='wait')
        run_background_transition(result.pk)
        row = TransitionMessage.objects.get(pk=result.pk)
        self.assertFalse(row.is_completed)
        self.assertEqual(row.errors_count, 0)
        self.assertEqual(row.deferral_reason, 'provider allowance')
        self.assertGreater(row.eligible_at, timezone.now())
        self.assertFalse(_claimable().filter(pk=row.pk).exists())
        started = row.started_at
        run_background_transition(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.started_at, started)
        self.widget.refresh_from_db()
        self.assertEqual((self.widget.status, self.widget.se_log, self.widget.cb_log), ('draft', '', ''))

    @override_settings(USE_TZ=False, TIME_ZONE='Asia/Hong_Kong')
    def test_defer_uses_default_timezone_when_database_datetimes_are_naive(self):
        result = self.enqueue(action='wait')
        run_background_transition(result.pk)
        row = TransitionMessage.objects.get(pk=result.pk)
        self.assertFalse(row.is_completed)
        self.assertEqual(row.errors_count, 0)
        self.assertTrue(timezone.is_naive(row.eligible_at))
        self.assertAlmostEqual((row.eligible_at - timezone.now()).total_seconds(), 3600, delta=5)
        self.assertFalse(_claimable().filter(pk=row.pk).exists())
        run_background_transition(row.pk)
        self.widget.refresh_from_db()
        self.assertEqual((self.widget.status, self.widget.se_log, self.widget.cb_log), ('draft', '', ''))

    def test_lock_contention_counts_as_an_error(self):
        """A record held longer than the row lock wait is a failed attempt,
        not quiet waiting: the retry limit then reports a job stuck behind
        an abandoned transaction."""
        for location in ('state_protection', 'side_effect'):
            with self.subTest(location=location):
                self.widget = Widget.objects.create()
                result = self.enqueue()
                error = TransitionTemporarilyUnavailable('record busy', reason=RefusalReason.LOCKED)
                def locked(instance, **kwargs):
                    raise error
                protection = (
                    patch('django_logic.state.State.protected', side_effect=error)
                    if location == 'state_protection'
                    else patch.object(OperationProcess.transitions[3].side_effects, '_commands', [locked])
                )
                with protection:
                    run_background_transition(result.pk)
                row = TransitionMessage.objects.get(pk=result.pk)
                self.assertFalse(row.is_completed)
                self.assertEqual(row.errors_count, 1)
                self.assertIn('record busy', row.last_error_message)
                self.assertIsNone(row.eligible_at)
                self.assertEqual(row.deferral_reason, '')
                self.widget.refresh_from_db()
                self.assertEqual((self.widget.status, self.widget.se_log), ('draft', ''))


    def test_declared_fairness_is_saved_without_passing_metadata_to_hooks(self):
        row = self.enqueue()
        self.assertEqual(row.fairness_key, 'client:1')
        self.assertNotIn('request_key', row.kwargs)
        self.assertNotIn('fairness_key', row.kwargs)

    def test_defer_rejects_naive_datetime(self):
        with self.assertRaises(ValueError):
            Defer(until=timezone.now().replace(tzinfo=None))


@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class NonAtomicOperationContractTests(TransactionTestCase):
    setUp = OperationContractTests.setUp
    enqueue = OperationContractTests.enqueue


    def test_non_atomic_defer_keeps_confirmed_database_receipt(self):
        from django.db import connection
        if connection.vendor != 'postgresql':
            self.skipTest('requires a separate PostgreSQL job connection')
        with patch.object(OperationProcess.transitions[2], 'atomic', False):
            result = self.enqueue(action='wait')
            run_background_transition(result.pk)
        row = TransitionMessage.objects.get(pk=result.pk)
        self.assertEqual(row.errors_count, 0)
        self.assertGreater(row.eligible_at, timezone.now())
        self.widget.refresh_from_db()
        self.assertEqual((self.widget.status, self.widget.se_log), ('draft', 'ok,'))
