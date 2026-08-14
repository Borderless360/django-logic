"""An attempt whose writes are silently rolled back counts as a failure.

Django's ``mark_for_rollback_on_error`` wraps every model write. It flags the
connection when a database error is raised inside an atomic block, even if the
caller catches that error. ``Atomic.__exit__`` then rolls back and raises
nothing, so an attempt that lost all its writes looks like a success.

A ``BackgroundTransition`` escapes this by luck: its target ``set_state`` is
the last statement in the attempt, so it hits the flagged connection and
raises ``TransactionManagementError``. A ``BackgroundAction`` writes no state,
so nothing runs after the caught error and the row completed with
``errors_count=0`` and success callbacks over discarded work.
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
    """The create-or-ignore idiom without the nested atomic it needs. This is
    how consumer code usually reaches this failure."""
    instance.customer_received = True
    instance.save(update_fields=['customer_received'])
    try:
        Invoice.objects.create(pk=instance.pk, status='dupe')
    except IntegrityError:
        pass


def _work_then_suppress_db_error_correctly(instance, **kwargs):
    """The same intent written correctly. The nested atomic absorbs the error
    and clears ``needs_rollback``, so the attempt really commits."""
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
        invoice = Invoice.objects.create(status='draft')

        with self.assertRaises(_SilentRollback):
            invoice.suppressed_error_proc.sync_out()

        transition_message = TransitionMessage.objects.get(
            instance_id=str(invoice.pk))
        invoice.refresh_from_db()
        # Charged and left uncompleted, so the periodic starter retries it.
        self.assertEqual(transition_message.errors_count, 1)
        self.assertFalse(transition_message.is_completed)
        # The attempt's write is gone, so the rollback really happened.
        self.assertFalse(invoice.customer_received)
        # Nothing reported the work as done.
        self.assertEqual(CALLBACKS_RAN, [])


@override_settings(DJANGO_LOGIC={**_SYNC, 'TRANSITION_MESSAGE_MAX_ERRORS': 1})
class TerminalTests(_Base):
    def test_at_max_errors_it_lands_in_failed_state(self):
        invoice = Invoice.objects.create(status='draft')

        with self.assertRaises(_SilentRollback):
            invoice.suppressed_error_proc.sync_out()

        transition_message = TransitionMessage.objects.get(
            instance_id=str(invoice.pk))
        invoice.refresh_from_db()
        self.assertTrue(transition_message.is_completed)
        self.assertEqual(invoice.status, 'failed')
        self.assertEqual(CALLBACKS_RAN, [])


@override_settings(DJANGO_LOGIC={**_SYNC, 'TRANSITION_MESSAGE_MAX_ERRORS': 3})
class CorrectlyNestedIsUntouchedTests(_Base):
    """The guard must fire only on the broken idiom. Here an inner ``atomic``
    absorbs the error, so the attempt commits and succeeds."""

    process = CorrectlyNestedProcess

    def test_a_nested_atomic_still_succeeds(self):
        invoice = Invoice.objects.create(status='draft')

        invoice.correctly_nested_proc.sync_out()

        transition_message = TransitionMessage.objects.get(
            instance_id=str(invoice.pk))
        invoice.refresh_from_db()
        self.assertTrue(transition_message.is_completed)
        self.assertEqual(transition_message.errors_count, 0)
        self.assertTrue(invoice.customer_received)
        self.assertEqual(CALLBACKS_RAN, [invoice.pk])


def _fail_attempt(instance, **kwargs):
    raise ValueError('boom')


def _suppress_db_error_on_failed_materialization(sender, instance, **kwargs):
    """Flags the connection inside the terminal ``failed_state`` savepoint.

    It hooks ``post_init`` because ``set_state`` ends with
    ``refresh_from_db``: a receiver there catches the error with no query
    left in the savepoint, which is the silent shape. A ``pre_save`` or
    ``post_save`` receiver still has queries after it, so the attempt raises
    ``TransactionManagementError`` and was always accounted.
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


class TerminalWriteDiscardProcess(Process):
    process_name = 'terminal_write_discard_proc'
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
class TerminalFailedStateWriteDiscardedTests(_Base):
    """A terminal ``failed_state`` write that is silently rolled back must be
    recorded as a failure, not logged as a state change that landed."""

    process = TerminalWriteDiscardProcess

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
        invoice = Invoice.objects.create(status='draft')

        with self.assertLogs('django-logic.transition', level='INFO') as logs:
            with self.assertRaises(ValueError):
                invoice.terminal_write_discard_proc.sync_out()

        transition_message = TransitionMessage.objects.get(
            instance_id=str(invoice.pk))
        invoice.refresh_from_db()
        # The row still completes, so nothing keeps retrying it.
        self.assertTrue(transition_message.is_completed)
        # The write was discarded, so the instance stays where it was.
        self.assertEqual(invoice.status, 'syncing')
        # The failure hooks read the stored state, not the value the
        # rolled-back savepoint left on the in-memory instance.
        self.assertEqual(FSE_SAW, ['syncing'])
        # An operator can see both problems on the row.
        self.assertIn('failed_state write',
                      transition_message.failure_side_effect_error)
        self.assertIn('boom', transition_message.last_error_message)
        self.assertEqual(CALLBACKS_RAN, [])
        output = '\n'.join(logs.output)
        self.assertIn('could not write failed_state', output)
        self.assertNotIn('Set State failed', output)


class SyncFailDiscardProcess(Process):
    process_name = 'sync_fail_discard_proc'
    transitions = [
        Transition('go', sources=['draft'], target='done',
                   failed_state='failed', side_effects=[_fail_attempt]),
    ]


class ActionWriteDiscardProcess(Process):
    process_name = 'action_write_discard_proc'
    transitions = [
        Action('go', sources=['draft'], failed_state='failed',
               side_effects=[_fail_attempt]),
    ]


class _SyncDiscardBase(_Base):
    """The synchronous failure paths must not log a state change when a
    receiver silently rolls back their ``failed_state`` savepoint."""

    def setUp(self):
        super().setUp()
        post_init.connect(
            _suppress_db_error_on_failed_materialization, sender=Invoice)
        self.addCleanup(
            post_init.disconnect,
            _suppress_db_error_on_failed_materialization, sender=Invoice)

    def assert_discarded_write_reported(self, drive):
        invoice = Invoice.objects.create(status='draft')

        with self.assertLogs('django-logic.transition', level='INFO') as logs:
            with self.assertRaises(ValueError):
                drive(invoice)

        # Check before the refresh. The failure hooks and the caller read the
        # in-memory attribute, so it must not hold a state the database never
        # had.
        self.assertEqual(invoice.status, 'draft')
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'draft')
        output = '\n'.join(logs.output)
        self.assertIn('could not write failed_state', output)
        self.assertNotIn('Set State failed', output)


@override_settings(DJANGO_LOGIC=_SYNC)
class SyncTransitionWriteDiscardedTests(_SyncDiscardBase):
    process = SyncFailDiscardProcess

    def test_discarded_write_is_reported_not_logged_as_landed(self):
        self.assert_discarded_write_reported(
            lambda invoice: invoice.sync_fail_discard_proc.go())


@override_settings(DJANGO_LOGIC=_SYNC)
class SyncActionWriteDiscardedTests(_SyncDiscardBase):
    process = ActionWriteDiscardProcess

    def test_discarded_write_is_reported_not_logged_as_landed(self):
        self.assert_discarded_write_reported(
            lambda invoice: invoice.action_write_discard_proc.go())


@override_settings(DJANGO_LOGIC=_SYNC)
class SyncTransitionWriteCorrectlyNestedTests(_Base):
    """Control: with the nested atomic in the same receiver, both synchronous
    writers must be left alone."""

    process = SyncFailDiscardProcess

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
        invoice = Invoice.objects.create(status='draft')

        with self.assertLogs('django-logic.transition', level='INFO') as logs:
            with self.assertRaises(ValueError):
                invoice.sync_fail_discard_proc.go()

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'failed')
        self.assertIn('Set State failed', '\n'.join(logs.output))


@override_settings(DJANGO_LOGIC={**_SYNC, 'TRANSITION_MESSAGE_MAX_ERRORS': 1})
class TerminalWriteCorrectlyNestedTests(_Base):
    """Control: with the nested atomic in the same receiver, the terminal
    write must be left alone."""

    process = TerminalWriteDiscardProcess

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
        invoice = Invoice.objects.create(status='draft')

        with self.assertLogs('django-logic.transition', level='INFO') as logs:
            with self.assertRaises(ValueError):
                invoice.terminal_write_discard_proc.sync_out()

        transition_message = TransitionMessage.objects.get(
            instance_id=str(invoice.pk))
        invoice.refresh_from_db()
        self.assertTrue(transition_message.is_completed)
        self.assertEqual(invoice.status, 'failed')
        self.assertEqual(transition_message.failure_side_effect_error, '')
        self.assertIn('Set State failed', '\n'.join(logs.output))
