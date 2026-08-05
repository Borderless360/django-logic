"""Pins for the instance-alias routing shipped in 0.12.0 (#187).

Mutation testing (M26) showed the whole fix was unguarded: reverting
``State.get_persisted_state`` — and every engine savepoint — to ``default``
left the suite green, because the test settings had a single database. The
second ``other`` alias plus the routed fixtures here make each of those
mutants fail:

* ``get_persisted_state`` must read the instance's own alias;
* the sync ``fail_transition`` savepoint must open on the instance's alias;
* the background attempt savepoint and ``_handle_failure``'s terminal
  ``failed_state`` savepoint likewise.

``django_logic.E002`` statically refuses a router that sends a
background-bound model off ``default`` — but only at check time, and an
explicit ``.using()`` with no router passes it silently, which is exactly the
case the engine's defensive routing covers. Bindings and the router are
installed per-test (after startup checks), so the check suite still sees the
default topology.
"""
from datetime import timedelta

from django.core.cache import cache
from django.db.models.signals import post_save
from django.test import TestCase, override_settings
from django.utils import timezone

from django_logic import Process, ProcessManager, Transition
from django_logic.background import BackgroundTransition
from django_logic.background.models import TransitionMessage
from django_logic.background.tasks import _watchdog_stale_attempts_inline
from django_logic.state import State
from tests.models import Invoice


def _boom(instance, **kwargs):
    raise ValueError('boom')


def _sibling_then_boom(instance, **kwargs):
    Invoice.objects.using(instance._state.db).create(status='sibling')
    raise ValueError('boom')


class RoutedFailProcess(Process):
    process_name = 'routed_fail_proc'
    transitions = [
        Transition('go', sources=['draft'], target='done',
                   failed_state='failed', side_effects=[_boom]),
    ]


class RoutedBgProcess(Process):
    process_name = 'routed_bg_proc'
    transitions = [
        BackgroundTransition(
            'go', sources=['draft'], target='done',
            in_progress_state='running', failed_state='failed',
            side_effects=[_sibling_then_boom]),
    ]


class _InvoiceOnOtherRouter:
    def db_for_read(self, model, **hints):
        return 'other' if model.__name__ == 'Invoice' else None

    def db_for_write(self, model, **hints):
        return 'other' if model.__name__ == 'Invoice' else None


class GetPersistedStateAliasTests(TestCase):
    databases = {'default', 'other'}

    def test_reads_the_instance_alias_not_default(self):
        w = Invoice.objects.using('other').create(status='real')
        # The same pk on default holding a different value is the mutant
        # detector: an unrouted read returns 'decoy'.
        Invoice.objects.using('default').create(pk=w.pk, status='decoy')
        self.assertEqual(w._state.db, 'other')
        self.assertEqual(State(w, 'status').get_persisted_state(), 'real')


class SyncFailedStateAliasTests(TestCase):
    databases = {'default', 'other'}

    def setUp(self):
        ProcessManager.bind_model_process(
            Invoice, RoutedFailProcess, state_field='status')
        self.addCleanup(
            ProcessManager.unbind_model_process, Invoice, RoutedFailProcess)
        cache.clear()
        self.addCleanup(cache.clear)

    def test_failed_state_lands_on_the_instance_alias(self):
        w = Invoice.objects.using('other').create(status='draft')
        with self.assertRaises(ValueError):
            w.routed_fail_proc.go()
        self.assertEqual(
            Invoice.objects.using('other').get(pk=w.pk).status, 'failed')
        self.assertFalse(Invoice.objects.using('default').exists())

    def test_rejected_failed_state_write_rolls_back_on_the_instance_alias(self):
        w = Invoice.objects.using('other').create(status='draft')

        def veto_failed(sender, instance, **kwargs):
            if instance.status == 'failed':
                # A write inside set_state's span, then a failure: the
                # fail_transition savepoint must roll BOTH back on 'other' —
                # a savepoint opened on default guards nothing.
                Invoice.objects.using(instance._state.db).create(
                    status='audit')
                raise RuntimeError('db refuses failed')

        post_save.connect(veto_failed, sender=Invoice)
        self.addCleanup(post_save.disconnect, veto_failed, sender=Invoice)

        with self.assertRaises(ValueError):  # the original error, unchanged
            w.routed_fail_proc.go()
        self.assertEqual(
            Invoice.objects.using('other').get(pk=w.pk).status, 'draft')
        self.assertFalse(
            Invoice.objects.using('other').filter(status='audit').exists())


@override_settings(DATABASE_ROUTERS=[_InvoiceOnOtherRouter()])
class BackgroundAliasTests(TestCase):
    databases = {'default', 'other'}

    def setUp(self):
        ProcessManager.bind_model_process(
            Invoice, RoutedBgProcess, state_field='status')
        self.addCleanup(
            ProcessManager.unbind_model_process, Invoice, RoutedBgProcess)
        cache.clear()
        self.addCleanup(cache.clear)

    @override_settings(DJANGO_LOGIC={
        'BACKGROUND_EXECUTION': 'sync',
        'TRANSITION_MESSAGE_MAX_ERRORS': 1,
    })
    def test_attempt_savepoint_and_failed_state_use_the_instance_alias(self):
        w = Invoice.objects.using('other').create(status='draft')
        with self.assertRaises(ValueError):
            w.routed_bg_proc.go()
        # Attempt savepoint on 'other': the sibling write rolled back there.
        self.assertFalse(
            Invoice.objects.using('other').filter(status='sibling').exists())
        # MAX_ERRORS=1 → terminal: _handle_failure's savepoint wrote
        # failed_state on 'other'.
        self.assertEqual(
            Invoice.objects.using('other').get(pk=w.pk).status, 'failed')
        tm = TransitionMessage.objects.get(instance_id=str(w.pk))
        self.assertTrue(tm.is_completed)
        self.assertEqual(tm.errors_count, 1)
        # The TM row stays on default; no instance rows leaked there.
        self.assertFalse(Invoice.objects.using('default').exists())


@override_settings(DATABASE_ROUTERS=[_InvoiceOnOtherRouter()])
class WatchdogFinalizeAliasTests(TestCase):
    """The OTHER terminal writer: _finalize_terminal_from_watchdog's
    failed_state savepoint must open on the instance's alias too — without
    this pin, reverting that site to DEFAULT leaves the suite green."""

    databases = {'default', 'other'}

    def setUp(self):
        ProcessManager.bind_model_process(
            Invoice, RoutedBgProcess, state_field='status')
        self.addCleanup(
            ProcessManager.unbind_model_process, Invoice, RoutedBgProcess)
        cache.clear()
        self.addCleanup(cache.clear)

    def _stale_tm(self, w):
        return TransitionMessage.objects.create(
            app_label='tests',
            model_name='invoice',
            instance_id=str(w.pk),
            process_name='routed_bg_proc',
            transition_name='go',
            queue_name='django_logic',
            started_at=timezone.now() - timedelta(seconds=120),
            timeout_seconds=60,
            errors_count=1,  # the watchdog's increment hits MAX_ERRORS=2
        )

    @override_settings(DJANGO_LOGIC={
        'BACKGROUND_EXECUTION': 'sync',
        'TRANSITION_MESSAGE_MAX_ERRORS': 2,
    })
    def test_watchdog_terminal_failed_state_lands_on_the_instance_alias(self):
        w = Invoice.objects.using('other').create(status='running')
        self._stale_tm(w)

        self.assertEqual(_watchdog_stale_attempts_inline(), 1)

        self.assertEqual(
            Invoice.objects.using('other').get(pk=w.pk).status, 'failed')
        tm = TransitionMessage.objects.get(instance_id=str(w.pk))
        self.assertTrue(tm.is_completed)
        self.assertFalse(Invoice.objects.using('default').exists())

    @override_settings(DJANGO_LOGIC={
        'BACKGROUND_EXECUTION': 'sync',
        'TRANSITION_MESSAGE_MAX_ERRORS': 2,
    })
    def test_vetoed_watchdog_write_rolls_back_on_the_instance_alias(self):
        # The distinguishing pin: a landed-write assertion passes whichever
        # connection the savepoint guards (set_state routes by instance
        # regardless). Only a rolled-back write can tell the aliases apart —
        # a savepoint opened on 'default' would leave BOTH writes standing
        # on 'other'.
        w = Invoice.objects.using('other').create(status='running')
        tm = self._stale_tm(w)

        def veto_failed(sender, instance, **kwargs):
            if instance.status == 'failed':
                Invoice.objects.using(instance._state.db).create(
                    status='audit')
                raise RuntimeError('db refuses failed')

        post_save.connect(veto_failed, sender=Invoice)
        self.addCleanup(post_save.disconnect, veto_failed, sender=Invoice)

        self.assertEqual(_watchdog_stale_attempts_inline(), 1)

        tm.refresh_from_db()
        self.assertTrue(tm.is_completed)  # completes despite the veto
        self.assertIn('failed_state write', tm.failure_side_effect_error)
        self.assertEqual(
            Invoice.objects.using('other').get(pk=w.pk).status, 'running')
        self.assertFalse(
            Invoice.objects.using('other').filter(status='audit').exists())
