"""Every engine write must land on the instance's own database alias.

Mutation testing showed the routing fix was unguarded: pointing
``State.get_persisted_state`` — and every engine savepoint — back at ``default``
left the suite green, because the test settings had one database. The second
``other`` alias and the routed fixtures here make each of those mutants fail:

* ``get_persisted_state`` must read the instance's own alias;
* the synchronous ``fail_transition`` savepoint must open on that alias;
* so must the background attempt savepoint and the terminal ``failed_state``
  savepoint in ``_handle_failure``.

A startup check refuses a router that sends a background-bound model off
``default``, but it only runs at check time. An explicit ``.using()`` with no
router passes it, and that is the case the engine's own routing covers. The
bindings and the router are installed per test, after the startup checks, so the
check suite still sees the default topology.
"""
from datetime import timedelta

from django.core.cache import cache
from django.db.models.signals import post_save
from django.test import TestCase, override_settings
from django.utils import timezone

from django_logic import Process, ProcessManager, Transition
from django_logic.background import BackgroundTransition
from django_logic.background.models import TransitionMessage
from django_logic.background.runner import finalize_stuck_attempt
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
        invoice = Invoice.objects.using('other').create(status='real')
        # The same pk on 'default' holds a different value, so an unrouted read
        # returns 'decoy'.
        Invoice.objects.using('default').create(pk=invoice.pk, status='decoy')
        self.assertEqual(invoice._state.db, 'other')
        self.assertEqual(
            State(invoice, 'status').get_persisted_state(), 'real')


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
        invoice = Invoice.objects.using('other').create(status='draft')
        with self.assertRaises(ValueError):
            invoice.routed_fail_proc.go()
        self.assertEqual(
            Invoice.objects.using('other').get(pk=invoice.pk).status, 'failed')
        self.assertFalse(Invoice.objects.using('default').exists())

    def test_rejected_failed_state_write_rolls_back_on_the_instance_alias(self):
        invoice = Invoice.objects.using('other').create(status='draft')

        def veto_failed(sender, instance, **kwargs):
            if instance.status == 'failed':
                # A write inside set_state, then a failure. The
                # fail_transition savepoint must roll both back on 'other'; a
                # savepoint opened on 'default' guards nothing.
                Invoice.objects.using(instance._state.db).create(
                    status='audit')
                raise RuntimeError('db refuses failed')

        post_save.connect(veto_failed, sender=Invoice)
        self.addCleanup(post_save.disconnect, veto_failed, sender=Invoice)

        with self.assertRaises(ValueError):  # the original error, unchanged
            invoice.routed_fail_proc.go()
        self.assertEqual(
            Invoice.objects.using('other').get(pk=invoice.pk).status, 'draft')
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
        invoice = Invoice.objects.using('other').create(status='draft')
        with self.assertRaises(ValueError):
            invoice.routed_bg_proc.go()
        # Attempt savepoint on 'other': the sibling write rolled back there.
        self.assertFalse(
            Invoice.objects.using('other').filter(status='sibling').exists())
        # MAX_ERRORS=1 makes this terminal, so _handle_failure's savepoint
        # wrote failed_state on 'other'.
        self.assertEqual(
            Invoice.objects.using('other').get(pk=invoice.pk).status, 'failed')
        transition_message = TransitionMessage.objects.get(
            instance_id=str(invoice.pk))
        self.assertTrue(transition_message.is_completed)
        self.assertEqual(transition_message.errors_count, 1)
        # The TransitionMessage row stays on 'default', and no instance rows
        # leaked there.
        self.assertFalse(Invoice.objects.using('default').exists())


@override_settings(DATABASE_ROUTERS=[_InvoiceOnOtherRouter()])
class StuckFinalizerAliasTests(TestCase):
    """The stuck finalizer is the other terminal writer, so its failed_state
    savepoint must open on the instance's alias too. Without this test,
    pointing that savepoint at 'default' leaves the suite green."""

    databases = {'default', 'other'}

    def setUp(self):
        ProcessManager.bind_model_process(
            Invoice, RoutedBgProcess, state_field='status')
        self.addCleanup(
            ProcessManager.unbind_model_process, Invoice, RoutedBgProcess)
        cache.clear()
        self.addCleanup(cache.clear)

    def _stale_transition_message(self, invoice):
        return TransitionMessage.objects.create(
            app_label='tests',
            model_name='invoice',
            instance_id=str(invoice.pk),
            process_name='routed_bg_proc',
            transition_name='go',
            queue_name='django_logic',
            started_at=timezone.now() - timedelta(seconds=120),
            errors_count=2,  # already at MAX_ERRORS=2; the stuck finalizer takes it
        )

    @override_settings(DJANGO_LOGIC={
        'BACKGROUND_EXECUTION': 'sync',
        'TRANSITION_MESSAGE_MAX_ERRORS': 2,
    })
    def test_terminal_failed_state_lands_on_the_instance_alias(self):
        invoice = Invoice.objects.using('other').create(status='running')
        transition_message = self._stale_transition_message(invoice)

        self.assertTrue(finalize_stuck_attempt(transition_message.pk))

        self.assertEqual(
            Invoice.objects.using('other').get(pk=invoice.pk).status, 'failed')
        transition_message = TransitionMessage.objects.get(
            instance_id=str(invoice.pk))
        self.assertTrue(transition_message.is_completed)
        self.assertFalse(Invoice.objects.using('default').exists())

    @override_settings(DJANGO_LOGIC={
        'BACKGROUND_EXECUTION': 'sync',
        'TRANSITION_MESSAGE_MAX_ERRORS': 2,
    })
    def test_vetoed_terminal_write_rolls_back_on_the_instance_alias(self):
        # A landed write proves nothing here: set_state routes by instance
        # whichever connection the savepoint guards. Only a rolled-back write
        # tells the aliases apart, because a savepoint opened on 'default'
        # leaves both writes standing on 'other'.
        invoice = Invoice.objects.using('other').create(status='running')
        transition_message = self._stale_transition_message(invoice)

        def veto_failed(sender, instance, **kwargs):
            if instance.status == 'failed':
                Invoice.objects.using(instance._state.db).create(
                    status='audit')
                raise RuntimeError('db refuses failed')

        post_save.connect(veto_failed, sender=Invoice)
        self.addCleanup(post_save.disconnect, veto_failed, sender=Invoice)

        self.assertTrue(finalize_stuck_attempt(transition_message.pk))

        transition_message.refresh_from_db()
        # The row completes despite the veto.
        self.assertTrue(transition_message.is_completed)
        self.assertIn('failed_state write',
                      transition_message.failure_side_effect_error)
        self.assertEqual(
            Invoice.objects.using('other').get(pk=invoice.pk).status,
            'running')
        self.assertFalse(
            Invoice.objects.using('other').filter(status='audit').exists())
