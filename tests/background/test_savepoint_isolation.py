"""R1/R2 regressions — savepoint isolation of user code in phase 2.

R1: a side-effect that raises a *genuine database error* through the ORM
used to poison the phase-2 atomic block: ``record_error`` itself then
raised ``TransactionManagementError``, the error was never recorded,
``errors_count`` never reached ``MAX_ERRORS``, the periodic starter
re-dispatched the row forever, and the partial-unique constraint blocked
every future background transition on the instance. With the savepoint,
the DB error is recorded like any other failure and the row reaches its
terminal state.

R2: side-effect writes from a *failed* attempt used to commit together
with the error bookkeeping (verified pre-fix: one surviving row), forcing
users into perfect idempotency even for plain DB writes. With the
savepoint, a failed attempt rolls back all of its side-effect writes —
all-or-nothing per attempt.

The same isolation applies to ``failure_side_effects`` on the terminal
path (their swallowed exception used to leave the connection aborted, so
``record_failure_side_effect_error`` / ``mark_as_completed`` blew up).
"""
from django.db import IntegrityError
from django.test import TransactionTestCase, override_settings

from django_logic import Process
from django_logic.background import BackgroundTransition, sync_execution
from django_logic.background.dispatch import retry_pending
from django_logic.background.models import TransitionMessage
from tests.background.models import Widget


_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 2,
    # 0 so retry_pending()'s recency guard considers every row eligible.
    'TRANSITION_MESSAGE_RETRY_MINUTES': 0,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}

# Module-level call log, reset per test. Lets tests assert which hooks ran
# and lets a side-effect fail only on its first invocation.
CALLS: list = []


def se_integrity_error(instance, **kwargs):
    """Raise a real IntegrityError through the ORM (R1).

    Two identical uncompleted rows for an unrelated fake instance violate
    the partial unique constraint; the second ``create`` raises. The first
    row must roll back with the attempt's savepoint.
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
    """Fail only on the first invocation — the retry then succeeds."""
    CALLS.append('se_boom_once')
    if CALLS.count('se_boom_once') == 1:
        raise ValueError('first attempt fails')


def fse_integrity_error(instance, **kwargs):
    CALLS.append('fse_integrity_error')
    for _ in range(2):
        TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id='888888',
            process_name='dup_proc_fse',
            transition_name='x',
            queue_name='q',
        )


def fse_write_log(instance, **kwargs):
    CALLS.append('fse_write_log')
    instance.cb_log = (instance.cb_log or '') + 'fse_written,'
    instance.save(update_fields=['cb_log'])


def fse_boom(instance, **kwargs):
    CALLS.append('fse_boom')
    raise RuntimeError('cleanup boom')


class SavepointProcess(Process):
    """Not bound to Widget — phase 2 restores it via the recorded
    ``process_class`` (the AttributeError fallback path)."""

    process_name = 'sp_proc'
    transitions = [
        # R1: genuine DB error in a side-effect.
        BackgroundTransition(
            action_name='break_db',
            sources=['draft'],
            target='broken_done',
            in_progress_state='breaking',
            failed_state='broken',
            side_effects=[se_integrity_error],
        ),
        # R2: partial write + plain failure.
        BackgroundTransition(
            action_name='partial_write',
            sources=['draft'],
            target='pw_done',
            in_progress_state='pw_running',
            failed_state='pw_failed',
            side_effects=[se_write_log, se_boom],
        ),
        # R2 success path: fails once, then succeeds on retry.
        BackgroundTransition(
            action_name='flaky_write',
            sources=['draft'],
            target='fw_done',
            in_progress_state='fw_running',
            failed_state='fw_failed',
            side_effects=[se_write_log, se_boom_once],
        ),
        # R1 terminal path: DB error inside failure_side_effects.
        BackgroundTransition(
            action_name='bad_cleanup_db',
            sources=['draft'],
            target='bc_done',
            in_progress_state='bc_running',
            failed_state='bc_failed',
            side_effects=[se_boom],
            failure_side_effects=[fse_integrity_error],
        ),
        # R2 for failure_side_effects: partial fse write + fse failure.
        BackgroundTransition(
            action_name='bad_cleanup_partial',
            sources=['draft'],
            target='bcp_done',
            in_progress_state='bcp_running',
            failed_state='bcp_failed',
            side_effects=[se_boom],
            failure_side_effects=[fse_write_log, fse_boom],
        ),
    ]


def _drive(widget, action, **kwargs):
    process = SavepointProcess(field_name='status', instance=widget)
    with sync_execution():
        return getattr(process, action)(**kwargs)


def _tm(widget):
    return (
        TransitionMessage.objects
        .filter(instance_id=str(widget.pk), process_name='sp_proc')
        .order_by('-id')
        .first()
    )


@override_settings(DJANGO_LOGIC=_SETTINGS)
class DatabaseErrorInSideEffectTests(TransactionTestCase):
    """R1 — the defect that used to retry forever and brick the instance."""

    def setUp(self):
        CALLS.clear()
        self.widget = Widget.objects.create()

    def test_integrity_error_is_recorded_not_transaction_management_error(self):
        # Pre-fix this raised TransactionManagementError (the poisoned outer
        # transaction), errors_count stayed 0 and the row never completed.
        with self.assertRaises(IntegrityError):
            _drive(self.widget, 'break_db')

        tm = _tm(self.widget)
        self.assertIsNotNone(tm)
        self.assertEqual(tm.errors_count, 1)
        self.assertFalse(tm.is_completed)
        self.assertIn('UNIQUE', tm.last_error_message.upper())
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'breaking')
        # The side-effect's own writes rolled back with the savepoint.
        self.assertFalse(
            TransitionMessage.objects.filter(process_name='dup_proc').exists()
        )

    def test_db_error_row_reaches_terminal_state_via_retries(self):
        with self.assertRaises(IntegrityError):
            _drive(self.widget, 'break_db')

        # One retry tick: the second attempt fails the same way, reaches
        # MAX_ERRORS=2 and finalizes — failed_state + completed. Pre-fix
        # the row stayed at errors_count=0 forever.
        retry_pending()

        tm = _tm(self.widget)
        self.assertEqual(tm.errors_count, 2)
        self.assertTrue(tm.is_completed)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'broken')
        # No further retries are possible on a completed row.
        self.assertEqual(retry_pending(), 0)

    def test_instance_is_not_bricked_after_terminal_failure(self):
        # Pre-fix, the forever-uncompleted row made every future background
        # transition raise AlreadyInProgress. Post-fix the row completes,
        # so new background work on the instance is accepted again.
        with self.assertRaises(IntegrityError):
            _drive(self.widget, 'break_db')
        retry_pending()  # reaches terminal state

        self.widget.refresh_from_db()
        self.widget.status = 'draft'
        self.widget.save(update_fields=['status'])
        CALLS.clear()
        with self.assertRaises(ValueError):
            _drive(self.widget, 'flaky_write')  # fails once, retried below
        retry_pending()
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'fw_done')


@override_settings(DJANGO_LOGIC=_SETTINGS)
class PartialWriteRollbackTests(TransactionTestCase):
    """R2 — failed attempts are all-or-nothing for DB writes."""

    def setUp(self):
        CALLS.clear()
        self.widget = Widget.objects.create()

    def test_failed_attempt_rolls_back_side_effect_writes(self):
        with self.assertRaises(ValueError):
            _drive(self.widget, 'partial_write')

        self.widget.refresh_from_db()
        # se_write_log ran (in memory) but its committed write rolled back.
        self.assertIn('se_write_log', CALLS)
        self.assertEqual(self.widget.se_log, '')
        tm = _tm(self.widget)
        self.assertEqual(tm.errors_count, 1)
        self.assertFalse(tm.is_completed)

    def test_successful_retry_persists_the_writes_exactly_once(self):
        with self.assertRaises(ValueError):
            _drive(self.widget, 'flaky_write')
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.se_log, '')  # attempt 1 rolled back

        retry_pending()  # attempt 2 succeeds

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'fw_done')
        # Exactly one surviving write — no duplicate from the failed attempt.
        self.assertEqual(self.widget.se_log, 'written,')
        tm = _tm(self.widget)
        self.assertTrue(tm.is_completed)


@override_settings(DJANGO_LOGIC=dict(_SETTINGS, TRANSITION_MESSAGE_MAX_ERRORS=1))
class FailureSideEffectIsolationTests(TransactionTestCase):
    """R1 terminal path — failure_side_effects get the same isolation."""

    def setUp(self):
        CALLS.clear()
        self.widget = Widget.objects.create()

    def test_db_error_in_failure_side_effects_still_finalizes_the_row(self):
        # MAX_ERRORS=1: the first failure is terminal. The fse raises a real
        # IntegrityError; pre-fix the aborted connection made
        # record_failure_side_effect_error / mark_as_completed blow up on
        # the terminal path too.
        with self.assertRaises(ValueError):
            _drive(self.widget, 'bad_cleanup_db')

        tm = _tm(self.widget)
        self.assertTrue(tm.is_completed)
        self.assertIn('UNIQUE', tm.failure_side_effect_error.upper())
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'bc_failed')
        self.assertIn('fse_integrity_error', CALLS)
        # The fse's own partial insert rolled back.
        self.assertFalse(
            TransitionMessage.objects.filter(process_name='dup_proc_fse').exists()
        )

    def test_failed_cleanup_rolls_back_its_partial_writes(self):
        with self.assertRaises(ValueError):
            _drive(self.widget, 'bad_cleanup_partial')

        tm = _tm(self.widget)
        self.assertTrue(tm.is_completed)
        self.assertIn('cleanup boom', tm.failure_side_effect_error)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'bcp_failed')
        # fse_write_log ran but its write rolled back with the fse savepoint.
        self.assertIn('fse_write_log', CALLS)
        self.assertEqual(self.widget.cb_log, '')
