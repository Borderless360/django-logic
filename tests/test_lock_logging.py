"""A failed lock acquisition must be visible in the logs.

A leaked lock once froze seven instances. The retry sent each of them to the
queue again every 20 minutes for ten days. Every attempt logged one ``Start``
line and nothing more, because ``change_state`` raised before it logged
anything. The log therefore read as "the transition starts and the worker drops
it", which is the wrong conclusion, and it reached a bug report. One line at the
raise site and the ``instance_key`` on the lifecycle lines make a frozen
instance findable with a per-instance log filter.
"""
from django.core.cache import cache
from django.test import TestCase, override_settings

from django_logic import Process, ProcessManager, Transition
from django_logic.background import BackgroundTransition
from django_logic.exceptions import TransitionNotAllowed
from django_logic.logger import TransitionEventType
from tests.models import Invoice

_SYNC = {'BACKGROUND_EXECUTION': 'sync'}


def _noop(instance, **kwargs):
    pass


def _boom(instance, **kwargs):
    raise ValueError('boom')


class LockLoggingProcess(Process):
    process_name = 'lock_logging_proc'
    transitions = [
        Transition('go', sources=['draft'], target='done'),
        Transition('act_fail', sources=['draft'], failed_state='ll_act_failed',
               side_effects=[_boom]),
        BackgroundTransition(
            'bg', sources=['draft'], target='done',
            in_progress_state='ll_running', failed_state='ll_failed',
            side_effects=[_noop],
        ),
    ]


@override_settings(DJANGO_LOGIC=_SYNC)
class FailedLockAcquisitionLoggingTests(TestCase):
    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, LockLoggingProcess, state_field='status')
        self.addCleanup(
            ProcessManager.unbind_model_process, Invoice, LockLoggingProcess)
        cache.clear()
        self.addCleanup(cache.clear)

    def _locked_by_someone_else(self, inv):
        # A foreign holder: same key, different token — exactly the leaked-lock
        # shape from the incident (the holder is gone, the key remains).
        inv.lock_logging_proc.state.lock()

    def test_sync_failed_acquire_is_logged_with_the_instance_key(self):
        inv = Invoice.objects.create(status='draft')
        self._locked_by_someone_else(inv)
        key = inv.lock_logging_proc.state.instance_key

        with self.assertLogs('django-logic.transition', level='INFO') as logs:
            with self.assertRaises(TransitionNotAllowed):
                inv.lock_logging_proc.go()

        wanted = [
            line for line in logs.output
            if f'{TransitionEventType.LOCK.value} failed {key}' in line
        ]
        self.assertTrue(wanted, logs.output)
        # INFO per #154 — losing the lock race is expected concurrency, and at
        # ERROR ten days of it would have paged for the wrong reason.
        self.assertTrue(wanted[0].startswith('INFO:'), wanted[0])

    def test_background_failed_acquire_is_logged_with_the_instance_key(self):
        inv = Invoice.objects.create(status='draft')
        self._locked_by_someone_else(inv)
        key = inv.lock_logging_proc.state.instance_key

        with self.assertLogs('django-logic.transition', level='INFO') as logs:
            with self.assertRaises(TransitionNotAllowed):
                inv.lock_logging_proc.bg()

        self.assertTrue(
            any(f'{TransitionEventType.LOCK.value} failed {key}' in line
                for line in logs.output),
            logs.output,
        )



@override_settings(DJANGO_LOGIC=_SYNC)
class LifecycleLinesCarryInstanceKeyTests(TestCase):
    """A log store filtered by instance must show the lock lifecycle without a
    tr_id self-join — Start being the only key-carrying line is what hid the
    incident (#188)."""

    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, LockLoggingProcess, state_field='status')
        self.addCleanup(
            ProcessManager.unbind_model_process, Invoice, LockLoggingProcess)
        cache.clear()
        self.addCleanup(cache.clear)

    def test_sync_lock_and_unlock_name_the_instance(self):
        inv = Invoice.objects.create(status='draft')
        key = inv.lock_logging_proc.state.instance_key

        with self.assertLogs('django-logic.transition', level='INFO') as logs:
            inv.lock_logging_proc.go()

        per_instance = [line for line in logs.output if key in line]
        self.assertTrue(
            any(f'{TransitionEventType.LOCK.value} {key}' in line
                for line in per_instance), logs.output)
        self.assertTrue(
            any(f'{TransitionEventType.UNLOCK.value} {key}' in line
                for line in per_instance), logs.output)

    def test_action_failed_state_write_lock_and_unlock_name_the_instance(self):
        # The Transition's write-scoped lock (#185) is the engine's newest
        # acquisition — without these lines a crash inside the write window
        # would be unattributable, the exact #188 blind spot.
        inv = Invoice.objects.create(status='draft')
        key = inv.lock_logging_proc.state.instance_key

        with self.assertLogs('django-logic.transition', level='INFO') as logs:
            with self.assertRaises(ValueError):
                inv.lock_logging_proc.act_fail()

        per_instance = [line for line in logs.output if key in line]
        self.assertTrue(
            any(f'{TransitionEventType.LOCK.value} {key}' in line
                for line in per_instance), logs.output)
        self.assertTrue(
            any(f'{TransitionEventType.UNLOCK.value} {key}' in line
                for line in per_instance), logs.output)
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'll_act_failed')

    def test_enqueue_lock_and_unlock_name_the_instance(self):
        inv = Invoice.objects.create(status='draft')
        key = inv.lock_logging_proc.state.instance_key

        with self.assertLogs('django-logic.transition', level='INFO') as logs:
            inv.lock_logging_proc.bg()

        self.assertTrue(
            any(f'{TransitionEventType.LOCK.value} {key}' in line
                for line in logs.output), logs.output)
        self.assertTrue(
            any(f'{TransitionEventType.UNLOCK.value} {key}' in line
                for line in logs.output), logs.output)

    def test_revalidation_failure_unlock_is_visible(self):
        inv = Invoice.objects.create(status='draft')
        key = inv.lock_logging_proc.state.instance_key
        # Move the persisted state out from under the resolved transition so
        # the under-lock revalidation fails after a successful acquire.
        Invoice.objects.filter(pk=inv.pk).update(status='elsewhere')

        with self.assertLogs('django-logic.transition', level='INFO') as logs:
            with self.assertRaises(TransitionNotAllowed):
                inv.lock_logging_proc.go()

        self.assertTrue(
            any(f'{TransitionEventType.UNLOCK.value} {key} '
                f'after revalidation failure' in line
                for line in logs.output),
            logs.output,
        )
        # And the lock really is free for the next caller.
        self.assertFalse(inv.lock_logging_proc.state.is_locked())
