"""A phase-2 attempt discarded by a silent savepoint rollback is a failure.

Django's ``mark_for_rollback_on_error`` — which ``Model.save_base`` wraps every
write in — flags the connection when a database error is raised inside an
atomic block *even if the caller catches it*. ``Atomic.__exit__`` then takes
the rollback branch with no exception propagating, so an attempt whose writes
were all discarded returns as though it had worked.

A ``BackgroundTransition`` is protected by accident: its target ``set_state``
is the last statement inside the attempt and hits a poisoned connection, so it
raises ``TransactionManagementError`` and is accounted. A ``BackgroundAction``
writes no state, so nothing after the suppression touches the database and the
attempt returned clean — a completed row, ``errors_count=0`` and success
callbacks on top of work that was thrown away.
"""
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models.signals import post_init
from django.test import TestCase, override_settings

from django_logic import Action, Process, ProcessManager, Transition
from django_logic.background import BackgroundAction, BackgroundTransition
from django_logic.background.models import TransitionMessage
from django_logic.commands import _SilentRollback
from tests.models import Invoice

_SYNC = {'BACKGROUND_EXECUTION': 'sync'}

CALLBACKS_RAN: list = []


def _work_then_suppress_db_error(instance, **kwargs):
    """The create-or-ignore idiom *without* the nested atomic it needs — the
    common way real consumer code reaches this."""
    instance.customer_received = True
    instance.save(update_fields=['customer_received'])
    try:
        Invoice.objects.create(pk=instance.pk, status='dupe')
    except IntegrityError:
        pass


def _work_then_suppress_db_error_correctly(instance, **kwargs):
    """The same intent written correctly: the nested atomic absorbs the error
    and clears ``needs_rollback``, so the attempt really did commit."""
    instance.customer_received = True
    instance.save(update_fields=['customer_received'])
    try:
        with transaction.atomic():
            Invoice.objects.create(pk=instance.pk, status='dupe')
    except IntegrityError:
        pass


def _record_callback(instance, **kwargs):
    CALLBACKS_RAN.append(instance.pk)


class SuppressedErrorProcess(Process):
    process_name = 'suppressed_error_proc'
    transitions = [
        BackgroundAction(
            'sync_out', sources=['draft'], failed_state='failed',
            side_effects=[_work_then_suppress_db_error],
            callbacks=[_record_callback],
        ),
    ]


class CorrectlyNestedProcess(Process):
    process_name = 'correctly_nested_proc'
    transitions = [
        BackgroundAction(
            'sync_out', sources=['draft'], failed_state='failed',
            side_effects=[_work_then_suppress_db_error_correctly],
            callbacks=[_record_callback],
        ),
    ]


class _Base(TestCase):
    process = SuppressedErrorProcess

    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, self.process, state_field='status')
        self.addCleanup(
            ProcessManager.unbind_model_process, Invoice, self.process)
        cache.clear()
        CALLBACKS_RAN.clear()
        self.addCleanup(cache.clear)
        self.addCleanup(CALLBACKS_RAN.clear)


@override_settings(DJANGO_LOGIC={**_SYNC, 'TRANSITION_MESSAGE_MAX_ERRORS': 3})
class RetriesRemainTests(_Base):
    def test_the_discarded_attempt_is_charged_an_error_and_retried(self):
        inv = Invoice.objects.create(status='draft')

        with self.assertRaises(_SilentRollback):
            inv.suppressed_error_proc.sync_out()

        tm = TransitionMessage.objects.get(instance_id=str(inv.pk))
        inv.refresh_from_db()
        # Charged and left uncompleted, so the periodic starter retries it.
        self.assertEqual(tm.errors_count, 1)
        self.assertFalse(tm.is_completed)
        # The proof the attempt really was discarded: its write is gone.
        self.assertFalse(inv.customer_received)
        # …and nothing announced the work as done.
        self.assertEqual(CALLBACKS_RAN, [])


@override_settings(DJANGO_LOGIC={**_SYNC, 'TRANSITION_MESSAGE_MAX_ERRORS': 1})
class TerminalTests(_Base):
    def test_at_max_errors_it_lands_in_failed_state(self):
        inv = Invoice.objects.create(status='draft')

        with self.assertRaises(_SilentRollback):
            inv.suppressed_error_proc.sync_out()

        tm = TransitionMessage.objects.get(instance_id=str(inv.pk))
        inv.refresh_from_db()
        self.assertTrue(tm.is_completed)
        self.assertEqual(inv.status, 'failed')
        self.assertEqual(CALLBACKS_RAN, [])


@override_settings(DJANGO_LOGIC={**_SYNC, 'TRANSITION_MESSAGE_MAX_ERRORS': 3})
class CorrectlyNestedIsUntouchedTests(_Base):
    """The guard must fire only on the broken idiom: an inner ``atomic``
    absorbs the error, so this attempt commits and succeeds as before."""

    process = CorrectlyNestedProcess

    def test_a_nested_atomic_still_succeeds(self):
        inv = Invoice.objects.create(status='draft')

        inv.correctly_nested_proc.sync_out()

        tm = TransitionMessage.objects.get(instance_id=str(inv.pk))
        inv.refresh_from_db()
        self.assertTrue(tm.is_completed)
        self.assertEqual(tm.errors_count, 0)
        self.assertTrue(inv.customer_received)
        self.assertEqual(CALLBACKS_RAN, [inv.pk])


def _fail_attempt(instance, **kwargs):
    raise ValueError('boom')


def _suppress_db_error_on_failed_materialization(sender, instance, **kwargs):
    """Poisons the terminal ``failed_state`` write's savepoint (#189).

    ``post_init`` because ``set_state`` ends with ``refresh_from_db``: a
    receiver firing there suppresses the error with NO query following it
    inside the savepoint, which is the genuinely silent shape —
    ``pre_save``/``post_save`` receivers poison the connection while queries
    still remain, so those raise ``TransactionManagementError`` and were
    already accounted honestly.
    """
    if instance.pk is None or instance.status != 'failed':
        return
    try:
        Invoice.objects.create(pk=instance.pk, status='dupe')
    except IntegrityError:
        pass


def _suppress_db_error_correctly_on_failed_materialization(
    sender, instance, **kwargs,
):
    if instance.pk is None or instance.status != 'failed':
        return
    try:
        with transaction.atomic():
            Invoice.objects.create(pk=instance.pk, status='dupe')
    except IntegrityError:
        pass


FSE_SAW: list = []


def _record_fse_observed_status(instance, **kwargs):
    FSE_SAW.append(instance.status)


class TerminalWritePoisonProcess(Process):
    process_name = 'terminal_write_poison_proc'
    transitions = [
        BackgroundTransition(
            'sync_out', sources=['draft'], target='done',
            in_progress_state='syncing', failed_state='failed',
            side_effects=[_fail_attempt],
            failure_callbacks=[_record_fse_observed_status],
            callbacks=[_record_callback],
        ),
    ]


@override_settings(DJANGO_LOGIC={**_SYNC, 'TRANSITION_MESSAGE_MAX_ERRORS': 1})
class TerminalFailedStateWritePoisonedTests(_Base):
    """A silently-discarded terminal ``failed_state`` write must be recorded
    as the failure it is, not logged as a landed ``SET_STATE`` (#189)."""

    process = TerminalWritePoisonProcess

    def setUp(self):
        super().setUp()
        post_init.connect(
            _suppress_db_error_on_failed_materialization, sender=Invoice)
        self.addCleanup(
            post_init.disconnect,
            _suppress_db_error_on_failed_materialization, sender=Invoice)

    def test_discarded_write_is_reported_not_logged_as_landed(self):
        FSE_SAW.clear()
        self.addCleanup(FSE_SAW.clear)
        inv = Invoice.objects.create(status='draft')

        with self.assertLogs('django-logic.transition', level='INFO') as logs:
            with self.assertRaises(ValueError):
                inv.terminal_write_poison_proc.sync_out()

        tm = TransitionMessage.objects.get(instance_id=str(inv.pk))
        inv.refresh_from_db()
        # The #178 invariant holds: the row still terminalises.
        self.assertTrue(tm.is_completed)
        # The proof the write was discarded: the instance stays parked.
        self.assertEqual(inv.status, 'syncing')
        # The failure hooks saw the restored state, not the phantom value
        # the discarded savepoint left on the in-memory instance.
        self.assertEqual(FSE_SAW, ['syncing'])
        # …and both problems are visible where an operator looks.
        self.assertIn('failed_state write', tm.failure_side_effect_error)
        self.assertIn('boom', tm.last_error_message)
        self.assertEqual(CALLBACKS_RAN, [])
        output = '\n'.join(logs.output)
        self.assertIn('could not write failed_state', output)
        self.assertNotIn('Set State failed', output)


class SyncFailPoisonProcess(Process):
    process_name = 'sync_fail_poison_proc'
    transitions = [
        Transition('go', sources=['draft'], target='done',
                   failed_state='failed', side_effects=[_fail_attempt]),
    ]


class ActionWritePoisonProcess(Process):
    process_name = 'action_write_poison_proc'
    transitions = [
        Action('go', sources=['draft'], failed_state='failed',
               side_effects=[_fail_attempt]),
    ]


class _SyncPoisonBase(_Base):
    """The #192 shape: the sync failure paths' ``failed_state`` savepoints
    must not log a false SET_STATE when a receiver silently discards them
    (the sync analog of #189; same ``post_init`` receiver spot)."""

    def setUp(self):
        super().setUp()
        post_init.connect(
            _suppress_db_error_on_failed_materialization, sender=Invoice)
        self.addCleanup(
            post_init.disconnect,
            _suppress_db_error_on_failed_materialization, sender=Invoice)

    def assert_discarded_write_reported(self, drive):
        inv = Invoice.objects.create(status='draft')

        with self.assertLogs('django-logic.transition', level='INFO') as logs:
            with self.assertRaises(ValueError):
                drive(inv)

        # BEFORE the refresh: the in-memory attribute must not hold the
        # discarded value — the failure hooks and the sync caller read it,
        # and a phantom 'failed' here is a state the database never had.
        self.assertEqual(inv.status, 'draft')
        inv.refresh_from_db()
        # The write really was discarded — the instance stays at its source.
        self.assertEqual(inv.status, 'draft')
        output = '\n'.join(logs.output)
        self.assertIn('could not write failed_state', output)
        self.assertNotIn('Set State failed', output)


@override_settings(DJANGO_LOGIC=_SYNC)
class SyncTransitionWritePoisonedTests(_SyncPoisonBase):
    process = SyncFailPoisonProcess

    def test_discarded_write_is_reported_not_logged_as_landed(self):
        self.assert_discarded_write_reported(
            lambda inv: inv.sync_fail_poison_proc.go())


@override_settings(DJANGO_LOGIC=_SYNC)
class SyncActionWritePoisonedTests(_SyncPoisonBase):
    process = ActionWritePoisonProcess

    def test_discarded_write_is_reported_not_logged_as_landed(self):
        self.assert_discarded_write_reported(
            lambda inv: inv.action_write_poison_proc.go())


@override_settings(DJANGO_LOGIC=_SYNC)
class SyncTransitionWriteCorrectlyNestedTests(_Base):
    """Control: the nested-atomic idiom in the same receiver spot must
    leave both sync writers untouched — no over-firing."""

    process = SyncFailPoisonProcess

    def setUp(self):
        super().setUp()
        post_init.connect(
            _suppress_db_error_correctly_on_failed_materialization,
            sender=Invoice)
        self.addCleanup(
            post_init.disconnect,
            _suppress_db_error_correctly_on_failed_materialization,
            sender=Invoice)

    def test_the_write_lands_and_set_state_is_logged(self):
        inv = Invoice.objects.create(status='draft')

        with self.assertLogs('django-logic.transition', level='INFO') as logs:
            with self.assertRaises(ValueError):
                inv.sync_fail_poison_proc.go()

        inv.refresh_from_db()
        self.assertEqual(inv.status, 'failed')
        self.assertIn('Set State failed', '\n'.join(logs.output))


@override_settings(DJANGO_LOGIC={**_SYNC, 'TRANSITION_MESSAGE_MAX_ERRORS': 1})
class TerminalWriteCorrectlyNestedTests(_Base):
    """Control: the correct nested-atomic idiom in the same receiver spot
    must leave the terminal write untouched — no over-firing."""

    process = TerminalWritePoisonProcess

    def setUp(self):
        super().setUp()
        post_init.connect(
            _suppress_db_error_correctly_on_failed_materialization,
            sender=Invoice)
        self.addCleanup(
            post_init.disconnect,
            _suppress_db_error_correctly_on_failed_materialization,
            sender=Invoice)

    def test_the_write_lands_and_nothing_is_recorded(self):
        inv = Invoice.objects.create(status='draft')

        with self.assertLogs('django-logic.transition', level='INFO') as logs:
            with self.assertRaises(ValueError):
                inv.terminal_write_poison_proc.sync_out()

        tm = TransitionMessage.objects.get(instance_id=str(inv.pk))
        inv.refresh_from_db()
        self.assertTrue(tm.is_completed)
        self.assertEqual(inv.status, 'failed')
        self.assertEqual(tm.failure_side_effect_error, '')
        self.assertIn('Set State failed', '\n'.join(logs.output))
