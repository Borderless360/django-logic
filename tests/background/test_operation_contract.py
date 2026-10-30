"""Durable submission, expected waiting, cancellation, and required follow-up."""
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from django_logic import Process, ProcessManager, Transition
from django_logic.background import BackgroundTransition, Defer, operation_options, cancel_pending
from django_logic.background.exceptions import RequestConflict
from django_logic.background.models import TransitionMessage
from django_logic.background.runner import run_background_transition
from django_logic.background.serializers import deserialize_kwargs, serialize_kwargs
from django_logic.background.safety_nets import _claimable, cleanup_completed_transitions
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
                             side_effects=[bg_ok], callbacks=[bg_callback], required_next='finish'),
        BackgroundTransition('finish', sources=['ready'], target='done', side_effects=[bg_ok]),
        BackgroundTransition('wait', sources=['draft'], target='done', side_effects=[wait_for_provider],
                             callbacks=[bg_callback], failed_state='failed'),
        BackgroundTransition('plain', sources=['draft'], target='done', side_effects=[bg_ok]),
        Transition('sync_only', sources=['draft'], target='done'),
    ]


@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class OperationContractTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        ProcessManager.bind_model_process(Widget, OperationProcess, state_field='status')
        self.addCleanup(ProcessManager.unbind_model_process, Widget, OperationProcess)
        self.widget = Widget.objects.create()

    def enqueue(self, action='plain', key='request:1', **kwargs):
        with operation_options(request_key=key, fairness_key='client:1') as result:
            getattr(self.widget.operations, action)(**kwargs)
        return result

    def test_repeat_returns_same_completed_operation_without_rerunning(self):
        first = self.enqueue(value=1)
        run_background_transition(first.job_id)
        self.widget.refresh_from_db()
        second = self.enqueue(value=1)
        self.assertEqual(first.job_id, second.job_id)
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(self.widget.se_log, 'ok,')
        with self.assertRaises(RequestConflict):
            self.enqueue(value=2)
        self.assertEqual(TransitionMessage.objects.count(), 1)

    def test_key_conflict_checks_object_action_and_actor(self):
        self.enqueue()
        with self.assertRaises(RequestConflict):
            self.enqueue(action='wait')
        other = Widget.objects.create()
        with operation_options(request_key='request:1'):
            with self.assertRaises(RequestConflict):
                other.operations.plain()

    def test_request_identity_preserves_lists_in_escaped_user_dictionaries(self):
        data = {'__dl_type__': 'set', 'value': [1, 2]}
        first = self.enqueue(data=data)
        repeated = self.enqueue(data=data)
        self.assertEqual(repeated.job_id, first.job_id)
        self.assertFalse(repeated.created)
        with self.assertRaises(RequestConflict):
            self.enqueue(data={'__dl_type__': 'set', 'value': [2, 1]})
        self.assertEqual(TransitionMessage.objects.count(), 1)

    def test_replay_returns_trace_when_kwargs_root_is_escaped(self):
        kwargs = {'__dl_type__': 'caller-data', 'value': [1, 2]}
        with operation_options(request_key='root-tag') as first:
            original_trace = self.widget.operations.plain(**kwargs)
        with operation_options(request_key='root-tag') as repeated:
            repeated_trace = self.widget.operations.plain(**kwargs)
        self.assertEqual(repeated_trace, original_trace)
        self.assertEqual(repeated.job_id, first.job_id)
        self.assertFalse(repeated.created)

    def test_required_successor_reuse_ignores_only_trace_when_kwargs_root_is_escaped(self):
        from django_logic.background.runner import _write_required_next

        result = self.enqueue(action='start', __dl_type__='caller-data', value=[1, 2])
        run_background_transition(result.job_id)
        parent = TransitionMessage.objects.get(pk=result.job_id)
        self.assertTrue(parent.is_completed)
        self.widget.refresh_from_db()
        original = deserialize_kwargs(parent.kwargs)
        _write_required_next(parent, self.widget.operations.state, original)
        self.assertEqual(TransitionMessage.objects.count(), 2)
        with self.assertRaises(RequestConflict):
            _write_required_next(parent, self.widget.operations.state, {**original, 'value': [2, 1]})

    def test_required_successor_preserves_lists_in_escaped_user_dictionaries(self):
        result = self.enqueue(action='start', data={'__dl_type__': 'set', 'value': [1, 2]})
        parent = TransitionMessage.objects.get(pk=result.job_id)
        child_kwargs = deserialize_kwargs(parent.kwargs)
        child_kwargs.update(parent_id=child_kwargs['tr_id'], tr_id='other-trace',
                            data={'__dl_type__': 'set', 'value': [2, 1]})
        TransitionMessage.objects.create(
            **TransitionMessage.instance_key(self.widget, 'operations'), field_name='status',
            transition_name='finish', queue_name=parent.queue_name, is_completed=True,
            owning_process_class=parent.owning_process_class,
            request_key=f'django-logic:required-next:{parent.pk}', kwargs=serialize_kwargs(child_kwargs),
        )
        run_background_transition(parent.pk)
        parent.refresh_from_db()
        self.assertFalse(parent.is_completed)
        self.assertEqual(parent.errors_count, 1)
        self.assertIn('different operation inputs', parent.last_error_message)
        self.widget.refresh_from_db()
        self.assertEqual((self.widget.status, self.widget.se_log), ('draft', ''))

    def test_request_identity_includes_actual_process_class(self):
        from tests.background.test_restore_verification import RogueProcess

        with operation_options(request_key='same-action') as first:
            self.widget.process.fulfil()
        process = RogueProcess(field_name='status', instance=self.widget)
        with operation_options(request_key='same-action'):
            with self.assertRaises(RequestConflict):
                process.fulfil()
        self.assertTrue(first.created)
        self.assertEqual(TransitionMessage.objects.count(), 1)

    def test_required_successor_exists_before_callbacks_and_has_same_fairness(self):
        result = self.enqueue(action='start')
        parent = TransitionMessage.objects.get(pk=result.job_id)
        observed = []
        def callback(instance, **kwargs):
            observed.append(TransitionMessage.objects.filter(request_key=f'django-logic:required-next:{parent.pk}').exists())
        with patch.object(OperationProcess.transitions[0].callbacks, '_commands', [callback]):
            run_background_transition(parent.pk)
        child = TransitionMessage.objects.get(request_key=f'django-logic:required-next:{parent.pk}')
        self.assertEqual(observed, [True])
        self.assertEqual(child.fairness_key, 'client:1')
        run_background_transition(parent.pk)
        self.assertEqual(TransitionMessage.objects.count(), 2)
        run_background_transition(child.pk)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'done')
        self.assertEqual(self.widget.se_log, 'ok,ok,')

    def test_successor_write_failure_rolls_back_parent_completion_and_database_effect(self):
        result = self.enqueue(action='start')
        with patch('django_logic.background.runner._write_required_next', side_effect=RuntimeError('write failed')):
            run_background_transition(result.job_id)
        parent = TransitionMessage.objects.get(pk=result.job_id)
        self.assertFalse(parent.is_completed)
        self.assertEqual(parent.errors_count, 1)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')
        self.assertEqual(self.widget.se_log, '')

    @override_settings(USE_TZ=True)
    def test_defer_rolls_back_atomic_effect_without_error_or_callbacks(self):
        result = self.enqueue(action='wait')
        run_background_transition(result.job_id)
        row = TransitionMessage.objects.get(pk=result.job_id)
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
        run_background_transition(result.job_id)
        row = TransitionMessage.objects.get(pk=result.job_id)
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
                result = self.enqueue(key=f'lock:{location}')
                error = TransitionTemporarilyUnavailable('record busy', reason=RefusalReason.LOCKED)
                def locked(instance, **kwargs):
                    raise error
                protection = (
                    patch('django_logic.state.State.protected', side_effect=error)
                    if location == 'state_protection'
                    else patch.object(OperationProcess.transitions[3].side_effects, '_commands', [locked])
                )
                with protection:
                    run_background_transition(result.job_id)
                row = TransitionMessage.objects.get(pk=result.job_id)
                self.assertFalse(row.is_completed)
                self.assertEqual(row.errors_count, 1)
                self.assertIn('record busy', row.last_error_message)
                self.assertIsNone(row.eligible_at)
                self.assertEqual(row.deferral_reason, '')
                self.widget.refresh_from_db()
                self.assertEqual((self.widget.status, self.widget.se_log), ('draft', ''))

    def test_pending_cancellation_is_terminal_and_does_not_run_effect(self):
        result = self.enqueue()
        self.assertEqual(cancel_pending(result.job_id), 'cancelled')
        self.assertEqual(cancel_pending(result.job_id), 'finished')
        self.assertEqual(cancel_pending(result.job_id + 100), 'missing')
        run_background_transition(result.job_id)
        row = TransitionMessage.objects.get(pk=result.job_id)
        self.assertEqual(row.completion_kind, 'cancelled')
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.se_log, '')

    def test_cleanup_retains_request_identity(self):
        result = self.enqueue()
        run_background_transition(result.job_id)
        TransitionMessage.objects.filter(pk=result.job_id).update(modified=timezone.now() - timedelta(days=30))
        cleanup_completed_transitions()
        self.assertTrue(TransitionMessage.objects.filter(pk=result.job_id).exists())

    def test_metadata_is_consumed_once_and_not_passed_to_hooks(self):
        with operation_options(fairness_key='client:2') as result:
            self.widget.operations.plain()
        row = TransitionMessage.objects.get(pk=result.job_id)
        self.assertEqual(row.fairness_key, 'client:2')
        self.assertNotIn('request_key', row.kwargs)
        self.assertNotIn('fairness_key', row.kwargs)

    def test_defer_rejects_naive_datetime(self):
        with self.assertRaises(ValueError):
            Defer(until=timezone.now().replace(tzinfo=None))

    def test_public_request_cannot_use_internal_successor_prefix(self):
        with self.assertRaises(ValueError):
            self.enqueue(key='django-logic:required-next:1')
        self.assertEqual(TransitionMessage.objects.count(), 0)

    def test_required_next_refuses_another_database_before_submission(self):
        self.widget._state.db = 'other'
        with self.assertRaisesMessage(ImproperlyConfigured, 'same database'):
            self.widget.operations.start()
        self.assertEqual(TransitionMessage.objects.count(), 0)

    def test_unrelated_internal_key_cannot_complete_parent_without_successor(self):
        result = self.enqueue(action='start')
        other = Widget.objects.create()
        TransitionMessage.objects.create(
            **TransitionMessage.instance_key(other, 'operations'), field_name='status',
            transition_name='plain', queue_name='django_logic', is_completed=True,
            request_key=f'django-logic:required-next:{result.job_id}',
        )
        run_background_transition(result.job_id)
        parent = TransitionMessage.objects.get(pk=result.job_id)
        self.assertFalse(parent.is_completed)
        self.assertEqual(parent.errors_count, 1)
        self.assertIn('different operation inputs', parent.last_error_message)
        self.widget.refresh_from_db()
        self.assertEqual((self.widget.status, self.widget.se_log), ('draft', ''))


@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class NonAtomicOperationContractTests(TransactionTestCase):
    setUp = OperationContractTests.setUp
    enqueue = OperationContractTests.enqueue

    def test_required_next_uses_job_connection_and_preserves_receipt(self):
        from django.db import connection
        if connection.vendor != 'postgresql':
            self.skipTest('requires a separate PostgreSQL job connection')
        with patch.object(OperationProcess.transitions[0], 'atomic', False):
            result = self.enqueue(action='start')
            run_background_transition(result.job_id)
        parent = TransitionMessage.objects.get(pk=result.job_id)
        self.assertTrue(parent.is_completed)
        child = TransitionMessage.objects.get(request_key=f'django-logic:required-next:{parent.pk}')
        self.assertFalse(child.is_completed)
        self.widget.refresh_from_db()
        self.assertEqual((self.widget.status, self.widget.se_log), ('ready', 'ok,'))

    def test_non_atomic_defer_keeps_confirmed_database_receipt(self):
        from django.db import connection
        if connection.vendor != 'postgresql':
            self.skipTest('requires a separate PostgreSQL job connection')
        with patch.object(OperationProcess.transitions[2], 'atomic', False):
            result = self.enqueue(action='wait')
            run_background_transition(result.job_id)
        row = TransitionMessage.objects.get(pk=result.job_id)
        self.assertEqual(row.errors_count, 0)
        self.assertGreater(row.eligible_at, timezone.now())
        self.widget.refresh_from_db()
        self.assertEqual((self.widget.status, self.widget.se_log), ('draft', 'ok,'))
