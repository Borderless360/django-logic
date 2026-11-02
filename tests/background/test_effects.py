"""Independent effects use the existing durable jobs without owning business state."""
import threading
import tempfile
import time
from io import StringIO
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from django_logic import Process, ProcessManager, Transition
from django_logic.background import BackgroundEffect, BackgroundTransition, Defer, cancel_pending, operation_options
from django_logic.background.exceptions import PermanentFailure
from django_logic.background.models import TransitionMessage
from django_logic.background.pull import claim_next, run_once
from django_logic.background.runner import finalize_stuck_attempt, run_background_transition
from django_logic.background.safety_nets import cleanup_completed_transitions
from django_logic.background.serializers import deserialize_kwargs
from tests import dl_settings
from tests.background.models import Widget, bg_ok
from tests.stability.base import requires_postgres


def record_effect(instance, **kwargs):
    Widget.objects.filter(pk=instance.pk).update(cb_log=kwargs.get('value', 'effect'))


def fail_effect(instance, **kwargs):
    raise PermanentFailure('optional failed')


def wait_effect(instance, **kwargs):
    raise Defer(until=timezone.now() + timedelta(hours=1), reason='allowance')


class EffectProcess(Process):
    process_name = 'effects'
    transitions = [
        BackgroundTransition('finish', sources=['draft'], target='ready', failed_state='failed',
                             side_effects=[bg_ok], queue='effect-test',
                             background_effects=[BackgroundEffect('archive', record_effect),
                                                 BackgroundEffect('notify', record_effect, queue='notify', timeout=5)]),
        Transition('advance', sources=['ready'], target='done'),
        BackgroundTransition('repeat', sources=['ready', 'done'], target='done',
                             background_effects=[BackgroundEffect('archive', record_effect)]),
    ]


class EffectTestSetup:
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        ProcessManager.bind_model_process(Widget, EffectProcess, state_field='status')
        self.addCleanup(ProcessManager.unbind_model_process, Widget, EffectProcess)
        self.widget = Widget.objects.create()

    def enqueue(self, **kwargs):
        with operation_options(fairness_key='client:one') as submission:
            self.widget.effects.finish(**kwargs)
        return TransitionMessage.objects.get(pk=submission.job_id)

    def finish(self, **kwargs):
        parent = self.enqueue(**kwargs)
        run_background_transition(parent.pk)
        self.widget.refresh_from_db()
        parent.refresh_from_db()
        return parent, list(TransitionMessage.objects.filter(parent_job_id=parent.pk).order_by('pk'))


@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class BackgroundEffectTests(EffectTestSetup, TestCase):
    def test_children_are_saved_with_identity_and_do_not_hold_parent_gate(self):
        parent, children = self.finish(value='saved')
        self.assertTrue(parent.is_completed)
        self.assertEqual(parent.background_effect_names, ['archive', 'notify'])
        self.assertEqual([row.effect_name for row in children], ['archive', 'notify'])
        self.assertEqual([row.queue_name for row in children], ['effect-test', 'notify'])
        self.assertEqual([row.timeout_seconds for row in children], [None, 5])
        for child in children:
            self.assertEqual(child.parent_transition_name, 'finish')
            self.assertEqual(child.fairness_key, 'client:one')
            self.assertEqual(child.process_name, 'effects')
            self.assertNotEqual(child.transition_name, parent.transition_name)
            self.assertEqual(deserialize_kwargs(child.kwargs)['value'], 'saved')
            self.assertEqual(deserialize_kwargs(child.kwargs)['parent_id'], deserialize_kwargs(parent.kwargs)['tr_id'])
            self.assertNotEqual(deserialize_kwargs(child.kwargs)['tr_id'], deserialize_kwargs(parent.kwargs)['tr_id'])
        self.assertFalse(TransitionMessage.in_flight_for(self.widget, 'effects').exists())
        self.assertEqual(TransitionMessage.for_instance(self.widget, 'effects').count(), 3)
        from django_logic.testing.runner import latest_message, uncompleted_message
        from django_logic.testing.snapshot import snapshot
        self.assertEqual(latest_message(self.widget, 'effects').pk, parent.pk)
        self.assertIsNone(uncompleted_message(self.widget, 'effects'))
        self.assertEqual(snapshot(self.widget, process_name='effects')['transition_message']['background_effect_names'],
                         ['archive', 'notify'])
        self.widget.effects.advance()
        for child in children:
            run_background_transition(child.pk)
            child.refresh_from_db()
            self.assertTrue(child.is_completed)
            self.assertFalse(child.ended_in_failure)
        self.widget.refresh_from_db()
        self.assertEqual((self.widget.status, self.widget.cb_log), ('done', 'saved'))

    def test_same_effect_from_two_parent_operations_can_coexist(self):
        parent, children = self.finish()
        self.widget.effects.repeat()
        second = TransitionMessage.in_flight_for(self.widget, 'effects').get()
        run_background_transition(second.pk)
        second_child = TransitionMessage.objects.get(parent_job_id=second.pk)
        self.assertNotEqual(second_child.request_key, children[0].request_key)
        self.assertEqual(TransitionMessage.objects.filter(effect_name='archive', is_completed=False).count(), 2)

    def test_repeated_completion_does_not_duplicate_effect_jobs(self):
        from django_logic.background.effects import save_effect_jobs
        parent, children = self.finish()
        run_background_transition(parent.pk)
        save_effect_jobs(parent, EffectProcess.transitions[0], self.widget.effects.state,
                         deserialize_kwargs(parent.kwargs))
        self.assertEqual(TransitionMessage.objects.count(), 3)

    def test_required_successor_can_have_its_own_effects(self):
        with patch.object(EffectProcess.transitions[0], 'required_next', 'repeat'):
            parent, children = self.finish()
        successor = TransitionMessage.in_flight_for(self.widget, 'effects').get()
        self.assertEqual(successor.background_effect_names, ['archive'])
        run_background_transition(successor.pk)
        self.assertEqual(TransitionMessage.objects.filter(parent_job_id=successor.pk).count(), 1)
        self.assertEqual(TransitionMessage.objects.filter(parent_job_id=parent.pk).count(), 2)

    def test_required_successor_rejects_cross_database_effect_routing(self):
        from django_logic.background.runner import _write_required_next
        parent, children = self.finish()
        parent.required_next = 'repeat'
        with (patch('django_logic.background.effects.router.db_for_read', return_value='other'),
              self.assertRaisesMessage(ImproperlyConfigured, 'default database')):
            _write_required_next(parent, self.widget.effects.state, deserialize_kwargs(parent.kwargs))
        self.assertFalse(TransitionMessage.objects.filter(request_key=f'django-logic:required-next:{parent.pk}').exists())

    def test_cross_database_parent_is_refused_before_submission(self):
        with (patch('django_logic.background.effects.router.db_for_read', return_value='other'),
              self.assertRaisesMessage(ImproperlyConfigured, 'default database')):
            self.enqueue()
        self.assertFalse(TransitionMessage.objects.exists())

    def test_effect_insert_failure_rolls_back_all_children_and_parent_completion(self):
        parent = self.enqueue()
        original = TransitionMessage.save

        def refuse_second(row, *args, **kwargs):
            if row.effect_name == 'notify':
                raise RuntimeError('cannot save second effect')
            return original(row, *args, **kwargs)

        with patch.object(TransitionMessage, 'save', refuse_second):
            run_background_transition(parent.pk)
        parent.refresh_from_db()
        self.widget.refresh_from_db()
        self.assertFalse(parent.is_completed)
        self.assertEqual(parent.errors_count, 1)
        self.assertEqual((self.widget.status, self.widget.se_log), ('draft', ''))
        self.assertFalse(TransitionMessage.objects.filter(parent_job_id=parent.pk).exists())
        run_background_transition(parent.pk)
        self.assertEqual(TransitionMessage.objects.filter(parent_job_id=parent.pk).count(), 2)

    def test_failed_deferred_superseded_and_cancelled_parents_create_no_effects(self):
        for mode in ['failed', 'deferred', 'superseded', 'cancelled']:
            with self.subTest(mode=mode):
                self.widget = Widget.objects.create()
                parent = self.enqueue()
                if mode == 'cancelled':
                    self.assertEqual(cancel_pending(parent.pk), 'cancelled')
                elif mode == 'superseded':
                    Widget.objects.filter(pk=self.widget.pk).update(status='done')
                else:
                    action = fail_effect if mode == 'failed' else wait_effect
                    with patch.object(EffectProcess.transitions[0].side_effects, '_commands', [action]):
                        run_background_transition(parent.pk)
                run_background_transition(parent.pk)
                self.assertFalse(TransitionMessage.objects.filter(parent_job_id=parent.pk).exists())

    def test_failed_and_stuck_effect_never_write_parent_failed_state(self):
        parent, children = self.finish()
        with patch.object(EffectProcess.transitions[0].background_effects[0], 'action', fail_effect):
            run_background_transition(children[0].pk)
        TransitionMessage.objects.filter(pk=children[1].pk).update(errors_count=5)
        self.assertTrue(finalize_stuck_attempt(children[1].pk))
        self.widget.refresh_from_db()
        parent.refresh_from_db()
        self.assertEqual(self.widget.status, 'ready')
        self.assertTrue(parent.is_completed)
        self.assertFalse(parent.ended_in_failure)
        for child in children:
            child.refresh_from_db()
            self.assertTrue(child.ended_in_failure)

    def test_waiting_effect_keeps_fairness_and_leaves_other_clients_ready(self):
        parent, children = self.finish()
        with patch.object(EffectProcess.transitions[0].background_effects[0], 'action', wait_effect):
            run_background_transition(children[0].pk)
        child = TransitionMessage.objects.get(pk=children[0].pk)
        self.assertEqual((child.deferral_count, child.errors_count, child.fairness_key), (1, 0, 'client:one'))
        self.widget = Widget.objects.create()
        with operation_options(fairness_key='client:two') as submission:
            self.widget.effects.finish()
        self.assertEqual(claim_next(['effect-test']), submission.job_id)

    def test_missing_effect_and_wrong_owner_fail_closed(self):
        parent, children = self.finish()
        with patch.object(EffectProcess.transitions[0], 'background_effects', ()):
            run_background_transition(children[0].pk)
        TransitionMessage.objects.filter(pk=children[1].pk).update(owning_process_class='gone.OtherProcess')
        run_background_transition(children[1].pk)
        for child in children:
            child.refresh_from_db()
            self.assertTrue(child.ended_in_failure)
            self.assertIn('[unrestorable]', child.last_error_message)
        self.widget.refresh_from_db()
        self.assertEqual((self.widget.status, self.widget.cb_log), ('ready', ''))

    def test_old_worker_lookup_cannot_restore_effect_as_parent(self):
        from django_logic.background.runner import _find_transition
        parent, children = self.finish()
        child = children[0]
        child.effect_name = ''  # A pre-3.1 worker cannot read this field.
        self.assertIsNone(_find_transition(self.widget.effects, child))

    def test_old_parent_snapshot_does_not_pick_up_new_declarations(self):
        parent = self.enqueue()
        TransitionMessage.objects.filter(pk=parent.pk).update(background_effect_names=[])
        run_background_transition(parent.pk)
        self.assertFalse(TransitionMessage.objects.filter(parent_job_id=parent.pk).exists())

    def test_removing_a_frozen_effect_prevents_parent_completion(self):
        parent = self.enqueue()
        with patch.object(EffectProcess.transitions[0], 'background_effects', ()):
            run_background_transition(parent.pk)
        parent.refresh_from_db()
        self.assertFalse(parent.is_completed)
        self.assertIn('not declared', parent.last_error_message)

    def test_cleanup_removes_old_cancelled_and_failed_effects_but_keeps_parent_failure(self):
        parent, children = self.finish()
        self.assertEqual(cancel_pending(children[0].pk), 'cancelled')
        TransitionMessage.objects.filter(pk=parent.pk).update(ended_in_failure=True)
        TransitionMessage.objects.filter(pk=children[1].pk).update(is_completed=True, ended_in_failure=True,
                                                                 completed_at=timezone.now())
        TransitionMessage.objects.update(modified=timezone.now() - timedelta(days=10))
        cleanup_completed_transitions()
        self.assertTrue(TransitionMessage.objects.filter(pk=parent.pk).exists())
        self.assertFalse(TransitionMessage.objects.filter(pk=children[0].pk).exists())
        self.assertFalse(TransitionMessage.objects.filter(pk=children[1].pk).exists())

    def test_job_report_identifies_effect_and_parent_without_claiming_a_state_change(self):
        parent, children = self.finish()
        TransitionMessage.objects.filter(pk=children[0].pk).update(errors_count=5)
        output = StringIO()
        call_command('dl_transitions', stdout=output)
        self.assertIn('effects.finish/archive', output.getvalue())
        self.assertIn(f'parent_job={parent.pk}', output.getvalue())
        self.assertIn('effect failure without changing the parent state', output.getvalue())
        self.assertNotIn('django-logic:effect:', output.getvalue())

    def test_earlier_producer_gets_database_defaults(self):
        original = self.enqueue()
        original.is_completed = True
        original.save()
        new_fields = {'background_effect_names', 'effect_name', 'parent_transition_name', 'parent_job_id'}
        quote = connection.ops.quote_name
        columns = ', '.join(quote(field.column) for field in TransitionMessage._meta.concrete_fields
                            if field.column not in new_fields and field.column != 'id')
        table = quote(TransitionMessage._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f'INSERT INTO {table} ({columns}) SELECT {columns} FROM {table} WHERE id = %s', [original.pk])
        copied = TransitionMessage.objects.exclude(pk=original.pk).get()
        self.assertEqual((copied.background_effect_names, copied.effect_name,
                          copied.parent_transition_name, copied.parent_job_id), ([], '', '', None))

    def test_declaration_validation(self):
        for effect in [lambda: BackgroundEffect('', record_effect),
                       lambda: BackgroundEffect('bad', None),
                       lambda: BackgroundEffect('bad', record_effect, atomic='no'),
                       lambda: BackgroundEffect('bad', record_effect, timeout=0),
                       lambda: BackgroundTransition('x', sources=[], background_effects=[record_effect]),
                       lambda: BackgroundTransition('x', sources=[], background_effects=[
                           BackgroundEffect('same', record_effect), BackgroundEffect('same', record_effect)]),
                       lambda: Transition('django-logic:effect:any', sources=[])]:
            with self.subTest(effect=effect), self.assertRaises(ImproperlyConfigured):
                effect()


class SyncBackgroundEffectTests(EffectTestSetup, TestCase):
    def test_all_effects_attempted_after_parent_completes_without_raising_parent_failure(self):
        seen = []

        def observe_then_fail(instance, **kwargs):
            parent = TransitionMessage.objects.get(effect_name='')
            seen.append((parent.is_completed, Widget.objects.get(pk=instance.pk).status))
            raise PermanentFailure('child only')

        with (patch.object(EffectProcess.transitions[0].background_effects[0], 'action', observe_then_fail),
              self.captureOnCommitCallbacks(execute=True)):
            parent = self.enqueue(value='second ran')
        self.assertEqual(seen, [(True, 'ready')])
        self.widget.refresh_from_db()
        self.assertEqual((self.widget.status, self.widget.cb_log), ('ready', 'second ran'))
        self.assertFalse(parent.ended_in_failure)
        self.assertEqual(TransitionMessage.objects.filter(parent_job_id=parent.pk, is_completed=True).count(), 2)

    def test_outer_rollback_discards_effect_execution(self):
        calls = []
        with patch.object(EffectProcess.transitions[0].background_effects[0], 'action',
                          lambda *args, **kwargs: calls.append('ran')):
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        self.enqueue()
                        self.assertEqual(calls, [])
                        raise ValueError('caller rolled back')
                except ValueError:
                    pass
        self.assertEqual(calls, [])
        self.assertFalse(TransitionMessage.objects.exists())


def record_receipt(instance, request_path='', **kwargs):
    if Widget.objects.get(pk=instance.pk).se_log == 'confirmed':
        return
    with open(request_path, 'a') as requests:
        requests.write('accepted\n')
    Widget.objects.filter(pk=instance.pk).update(se_log='confirmed')


def slow_effect(instance, **kwargs):
    time.sleep(30)


class NonAtomicEffectProcess(Process):
    process_name = 'receipt_effects'
    transitions = [
        BackgroundTransition('finish', sources=['draft'], target='ready', failed_state='failed',
                             atomic=False, side_effects=[record_receipt], queue='receipt-parent',
                             background_effects=[BackgroundEffect('archive', record_effect, atomic=False),
                                                 BackgroundEffect('notify', record_effect, atomic=False)]),
    ]


@requires_postgres
@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class ConcurrentBackgroundEffectTests(EffectTestSetup, TransactionTestCase):
    def test_non_atomic_parent_insert_failure_preserves_receipt_before_safe_retry(self):
        ProcessManager.bind_model_process(Widget, NonAtomicEffectProcess, state_field='status')
        self.addCleanup(ProcessManager.unbind_model_process, Widget, NonAtomicEffectProcess)
        with tempfile.NamedTemporaryFile() as request_file:
            self.widget.receipt_effects.finish(request_path=request_file.name)
            parent = TransitionMessage.in_flight_for(self.widget, 'receipt_effects').get()
            table = connection.ops.quote_name(TransitionMessage._meta.db_table)
            with connection.cursor() as cursor:
                cursor.execute('''CREATE FUNCTION dl_test_refuse_effect() RETURNS trigger AS $$
                    BEGIN RAISE EXCEPTION 'effect insert refused for test'; END;
                    $$ LANGUAGE plpgsql''')
                cursor.execute(f'''CREATE TRIGGER dl_test_refuse_effect BEFORE INSERT ON {table}
                    FOR EACH ROW WHEN (NEW.parent_job_id = {parent.pk} AND NEW.effect_name = 'notify')
                    EXECUTE FUNCTION dl_test_refuse_effect()''')
            try:
                run_background_transition(parent.pk)
                parent.refresh_from_db()
                self.widget.refresh_from_db()
                self.assertFalse(parent.is_completed)
                self.assertEqual(parent.errors_count, 1)
                self.assertEqual((self.widget.status, self.widget.se_log), ('draft', 'confirmed'))
                self.assertFalse(TransitionMessage.objects.filter(parent_job_id=parent.pk).exists())
            finally:
                with connection.cursor() as cursor:
                    cursor.execute(f'DROP TRIGGER dl_test_refuse_effect ON {table}')
                    cursor.execute('DROP FUNCTION dl_test_refuse_effect()')
            run_background_transition(parent.pk)
            parent.refresh_from_db()
            self.assertTrue(parent.is_completed)
            self.assertEqual(TransitionMessage.objects.filter(parent_job_id=parent.pk).count(), 2)
            request_file.seek(0)
            self.assertEqual(request_file.read(), b'accepted\n')
            for child in TransitionMessage.objects.filter(parent_job_id=parent.pk):
                run_background_transition(child.pk)
                child.refresh_from_db()
                self.assertTrue(child.is_completed)
                self.assertFalse(child.ended_in_failure, child.last_error_message)

    @override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull', TRANSITION_MESSAGE_MAX_ERRORS=1))
    def test_killed_effect_is_finalized_without_changing_parent_state(self):
        parent, children = self.finish()
        TransitionMessage.objects.filter(pk=children[0].pk).update(timeout_seconds=1, queue_name='effect-timeout')
        with patch.object(EffectProcess.transitions[0].background_effects[0], 'action', slow_effect):
            self.assertTrue(run_once(['effect-timeout'], isolate=True))
        child = TransitionMessage.objects.get(pk=children[0].pk)
        self.assertEqual(child.errors_count, 1)
        self.assertIn('[timeout]', child.last_error_message)
        finalize_stuck_attempt(child.pk)
        self.widget.refresh_from_db()
        parent.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(self.widget.status, 'ready')
        self.assertTrue(parent.is_completed)
        self.assertFalse(parent.ended_in_failure)
        self.assertTrue(child.ended_in_failure)

    @override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='sync'))
    def test_outer_commit_runs_effects_only_after_the_commit(self):
        calls = []
        with patch.object(EffectProcess.transitions[0].background_effects[0], 'action',
                          lambda *args, **kwargs: calls.append('ran')):
            with transaction.atomic():
                parent = self.enqueue()
                self.assertTrue(parent.is_completed)
                self.assertEqual(calls, [])
            self.assertEqual(calls, ['ran'])

    def test_two_workers_finish_same_parent_once(self):
        parent = self.enqueue()
        started, release = threading.Event(), threading.Event()
        failures = []

        def held(instance, **kwargs):
            bg_ok(instance)
            started.set()
            if not release.wait(10):
                raise AssertionError('test release was not received')

        def run():
            try:
                run_background_transition(parent.pk)
            except BaseException as error:
                failures.append(error)
            finally:
                connections.close_all()

        with patch.object(EffectProcess.transitions[0].side_effects, '_commands', [held]):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(started.wait(5))
                run_background_transition(parent.pk)
                self.assertFalse(TransitionMessage.objects.filter(parent_job_id=parent.pk).exists())
            finally:
                release.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.se_log, 'ok,')
        self.assertEqual(TransitionMessage.objects.filter(parent_job_id=parent.pk).count(), 2)

    def test_running_effect_does_not_block_another_parent_action(self):
        parent, children = self.finish()
        started, release = threading.Event(), threading.Event()
        failures = []

        def held(instance, **kwargs):
            started.set()
            if not release.wait(10):
                raise AssertionError('test release was not received')

        def run():
            try:
                run_background_transition(children[0].pk)
            except BaseException as error:
                failures.append(error)
            finally:
                connections.close_all()

        with patch.object(EffectProcess.transitions[0].background_effects[0], 'action', held):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(started.wait(5))
                self.assertEqual(cancel_pending(children[0].pk), 'running')
                self.widget.effects.advance()
                self.assertEqual(Widget.objects.get(pk=self.widget.pk).status, 'done')
            finally:
                release.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        child = TransitionMessage.objects.get(pk=children[0].pk)
        self.assertTrue(child.is_completed)
        self.assertFalse(child.ended_in_failure)
