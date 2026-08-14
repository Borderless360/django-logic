"""The worker runs each attempt's database writes inside a savepoint.

A side-effect that raised a real database error used to abort the whole
transaction the worker was running in. Recording the error then failed too, so
``errors_count`` never reached ``MAX_ERRORS``, the periodic starter sent the row
to the queue forever, and the partial unique index blocked every later
background transition on the instance. With the savepoint, the database error is
recorded like any other failure and the row reaches its terminal state.

Side-effect writes from a failed attempt used to commit together with the error
bookkeeping, which forced users to make even plain database writes idempotent.
With the savepoint a failed attempt rolls back every write it made.

The terminal path has the same isolation. A swallowed exception there used to
leave the connection unusable, so recording the failure and marking the row
completed both failed.
"""
from django.db import IntegrityError
from django.test import TransactionTestCase, override_settings

from django_logic import Process
from django_logic.background import BackgroundTransition, sync_execution
from django_logic.background.dispatch import retry_pending
from django_logic.background.models import TransitionMessage
from tests.background.models import Widget
from tests import dl_settings


_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=2, TRANSITION_MESSAGE_RETRY_MINUTES=0)

# Call log, cleared for each test. It lets a test assert which hooks ran, and
# lets a side-effect fail only on its first call.
CALLS: list = []


def se_integrity_error(instance, **kwargs):
    """Raise a real IntegrityError through the ORM.

    Two identical uncompleted rows for an unrelated instance id break the
    partial unique index, so the second ``create`` raises. The first row must
    roll back with the attempt's savepoint.
    """
    CALLS.append('se_integrity_error')
    for _ in range(2):
        TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id='999999',
            process_name='dup_proc',
            transition_name='x',
            queue_name='q',
        )


def se_write_log(instance, **kwargs):
    CALLS.append('se_write_log')
    instance.se_log = (instance.se_log or '') + 'written,'
    instance.save(update_fields=['se_log'])


def se_boom(instance, **kwargs):
    CALLS.append('se_boom')
    raise ValueError('plain boom')


def se_boom_once(instance, **kwargs):
    """Fail on the first call only, so the retry succeeds."""
    CALLS.append('se_boom_once')
    if CALLS.count('se_boom_once') == 1:
        raise ValueError('first attempt fails')


class SavepointProcess(Process):
    """Not bound to Widget. The worker restores it from the ``process_class``
    recorded on the row."""

    process_name = 'sp_proc'
    transitions = [
        # A real database error inside a side-effect.
        BackgroundTransition(
            action_name='break_db',
            sources=['draft'],
            target='broken_done',
            in_progress_state='breaking',
            failed_state='broken',
            side_effects=[se_integrity_error],
        ),
        # A write, then a plain failure.
        BackgroundTransition(
            action_name='partial_write',
            sources=['draft'],
            target='pw_done',
            in_progress_state='pw_running',
            failed_state='pw_failed',
            side_effects=[se_write_log, se_boom],
        ),
        # Fails once, then succeeds on retry.
        BackgroundTransition(
            action_name='flaky_write',
            sources=['draft'],
            target='fw_done',
            in_progress_state='fw_running',
            failed_state='fw_failed',
            side_effects=[se_write_log, se_boom_once],
        ),
    ]


def _drive(widget, action, **kwargs):
    process = SavepointProcess(field_name='status', instance=widget)
    with sync_execution():
        return getattr(process, action)(**kwargs)


def _latest_message(widget):
    return (
        TransitionMessage.objects
        .filter(instance_id=str(widget.pk), process_name='sp_proc')
        .order_by('-id')
        .first()
    )


@override_settings(DJANGO_LOGIC=_SETTINGS)
class DatabaseErrorInSideEffectTests(TransactionTestCase):
    """A database error inside a side-effect used to retry forever."""

    def setUp(self):
        CALLS.clear()
        self.widget = Widget.objects.create()

    def test_integrity_error_is_recorded_not_transaction_management_error(self):
        # Before the fix this raised TransactionManagementError, errors_count
        # stayed at 0, and the row never completed.
        with self.assertRaises(IntegrityError):
            _drive(self.widget, 'break_db')

        transition_message = _latest_message(self.widget)
        self.assertIsNotNone(transition_message)
        self.assertEqual(transition_message.errors_count, 1)
        self.assertFalse(transition_message.is_completed)
        self.assertIn('UNIQUE', transition_message.last_error_message.upper())
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'breaking')
        # The side-effect's own writes rolled back with the savepoint.
        self.assertFalse(
            TransitionMessage.objects.filter(process_name='dup_proc').exists()
        )

    def test_db_error_row_reaches_terminal_state_via_retries(self):
        with self.assertRaises(IntegrityError):
            _drive(self.widget, 'break_db')

        # One retry tick. The second attempt fails the same way, reaches
        # MAX_ERRORS of 2, writes failed_state and completes the row. Before
        # the fix the row stayed at errors_count 0 forever.
        retry_pending()

        transition_message = _latest_message(self.widget)
        self.assertEqual(transition_message.errors_count, 2)
        self.assertTrue(transition_message.is_completed)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'broken')
        # A completed row is never retried again.
        self.assertEqual(retry_pending(), 0)

    def test_instance_accepts_new_work_after_terminal_failure(self):
        # Before the fix the row stayed uncompleted forever, so every later
        # background transition raised AlreadyInProgress. Now the row
        # completes and the instance accepts new background work.
        with self.assertRaises(IntegrityError):
            _drive(self.widget, 'break_db')
        retry_pending()  # the row reaches its terminal state

        self.widget.refresh_from_db()
        self.widget.status = 'draft'
        self.widget.save(update_fields=['status'])
        CALLS.clear()
        with self.assertRaises(ValueError):
            _drive(self.widget, 'flaky_write')  # fails once, retried below
        retry_pending()  # the retry succeeds
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'fw_done')


@override_settings(DJANGO_LOGIC=_SETTINGS)
class PartialWriteRollbackTests(TransactionTestCase):
    """A failed attempt leaves no database write behind."""

    def setUp(self):
        CALLS.clear()
        self.widget = Widget.objects.create()

    def test_failed_attempt_rolls_back_side_effect_writes(self):
        with self.assertRaises(ValueError):
            _drive(self.widget, 'partial_write')

        self.widget.refresh_from_db()
        # se_write_log ran, but its write rolled back.
        self.assertIn('se_write_log', CALLS)
        self.assertEqual(self.widget.se_log, '')
        transition_message = _latest_message(self.widget)
        self.assertEqual(transition_message.errors_count, 1)
        self.assertFalse(transition_message.is_completed)

    def test_successful_retry_persists_the_writes_exactly_once(self):
        with self.assertRaises(ValueError):
            _drive(self.widget, 'flaky_write')
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.se_log, '')  # the first attempt rolled back

        retry_pending()  # the second attempt succeeds

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'fw_done')
        # One surviving write, with no duplicate from the failed attempt.
        self.assertEqual(self.widget.se_log, 'written,')
        transition_message = _latest_message(self.widget)
        self.assertTrue(transition_message.is_completed)
