"""Crash accounting must not wait behind another worker's row lock."""
import os
import signal
import threading
import time
import unittest
from collections import deque
from contextlib import contextmanager
from unittest.mock import patch

from django.db import OperationalError, connections, transaction
from django.test import SimpleTestCase, TransactionTestCase

from django_logic.background.models import TransitionMessage
from django_logic.background import pull
from django_logic.background.pull import _Attempt, _harvest, _try_account
from django_logic.testing import open_transition_message
from tests.background.models import Widget
from tests.stability.base import requires_postgres


@requires_postgres
class AccountingContentionTests(TransactionTestCase):
    databases = '__all__'

    def _row(self):
        widget = Widget.objects.create(status='fulfilling')
        return open_transition_message(widget, 'process', 'fulfil')

    @contextmanager
    def _locked_row(self, row, *, complete=False):
        locked = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        errors = []

        def hold_row():
            try:
                with transaction.atomic():
                    TransitionMessage.objects.select_for_update().get(pk=row.pk)
                    locked.set()
                    release.wait(timeout=4)
                    if complete:
                        TransitionMessage.objects.filter(pk=row.pk).update(
                            is_completed=True,
                        )
            except BaseException as exc:
                errors.append(exc)
            finally:
                connections.close_all()
                finished.set()

        holder = threading.Thread(target=hold_row)
        holder.start()
        try:
            self.assertTrue(locked.wait(timeout=5), errors)
            yield finished
        finally:
            release.set()
            holder.join(timeout=5)
            self.assertFalse(holder.is_alive(), 'the row holder did not stop')
            self.assertEqual(errors, [], 'the row holder failed')

    def test_locked_accounting_retries_once_after_the_row_is_released(self):
        row = self._row()
        attempts = {}
        pending = deque([
            _Attempt(
                pk=row.pk, timeout_seconds=None, deadline=None,
                reaped=True, exit_code=3,
            ),
        ])
        with self._locked_row(row) as finished, \
                patch('django_logic.background.pull.logger.error') as log_error, \
                patch('django_logic.background.pull.logger.warning') as log_warning:
            started = time.monotonic()
            _harvest(attempts, pending, block=False)
            self.assertLess(time.monotonic() - started, 2)
            self.assertFalse(finished.is_set())
            self.assertEqual(attempts, {})
            self.assertEqual(len(pending), 1)
            log_error.assert_not_called()
            log_warning.assert_not_called()
            row.refresh_from_db()
            self.assertEqual(row.errors_count, 0)

        _harvest(attempts, pending, block=True)
        _harvest(attempts, pending, block=False)
        self.assertEqual(attempts, {})
        self.assertEqual(list(pending), [])
        row.refresh_from_db()
        self.assertEqual(row.errors_count, 1)
        self.assertIn('[crashed]', row.last_error_message)

    def test_a_replacement_completion_discards_the_deferred_error(self):
        row = self._row()
        attempt = _Attempt(
            pk=row.pk, timeout_seconds=None, deadline=None,
            reaped=True, exit_code=3,
        )
        with self._locked_row(row, complete=True) as finished:
            self.assertFalse(_try_account(attempt))
            self.assertFalse(finished.is_set())
        self.assertTrue(_try_account(attempt))
        row.refresh_from_db()
        self.assertTrue(row.is_completed)
        self.assertEqual(row.errors_count, 0)
        self.assertEqual(row.last_error_message, '')

    @unittest.skipUnless(hasattr(os, 'fork'), 'needs os.fork')
    def test_a_locked_accounting_row_does_not_delay_a_sibling_timeout(self):
        locked_row = self._row()
        sibling_row = self._row()
        connections.close_all()
        sibling_pid = os.fork()
        if sibling_pid == 0:
            time.sleep(30)
            os._exit(0)

        pending = deque([
            _Attempt(
                pk=locked_row.pk, timeout_seconds=None, deadline=None,
                reaped=True, exit_code=3,
            ),
        ])
        attempts = {
            sibling_pid: _Attempt(
                pk=sibling_row.pk, timeout_seconds=0.2,
                deadline=time.monotonic() + 0.2,
            ),
        }
        try:
            with self._locked_row(locked_row) as finished:
                started = time.monotonic()
                while sibling_pid in attempts and time.monotonic() - started < 2:
                    _harvest(attempts, pending, block=False)
                    time.sleep(0.01)
                self.assertLess(time.monotonic() - started, 2)
                self.assertNotIn(sibling_pid, attempts)
                self.assertEqual([attempt.pk for attempt in pending], [locked_row.pk])
                self.assertFalse(finished.is_set())
                sibling_row.refresh_from_db()
                self.assertEqual(sibling_row.errors_count, 1)
                self.assertIn('[timeout]', sibling_row.last_error_message)

            _harvest(attempts, pending, block=True)
            self.assertEqual(attempts, {})
            self.assertEqual(list(pending), [])
            locked_row.refresh_from_db()
            self.assertEqual(locked_row.errors_count, 1)
        finally:
            sibling = attempts.get(sibling_pid)
            if sibling is not None and not sibling.reaped:
                try:
                    os.kill(sibling_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(sibling_pid, 0)
                except ChildProcessError:
                    pass

    def test_a_long_accounting_lock_warns_after_five_seconds_then_once_a_minute(self):
        row = self._row()
        attempt = _Attempt(
            pk=row.pk, timeout_seconds=None, deadline=None,
            reaped=True, exit_code=3,
        )
        with self._locked_row(row), \
                patch.object(pull.time, 'monotonic') as clock, \
                self.assertLogs('django-logic', level='WARNING') as logs:
            for seconds, warning_count in ((100, 0), (104, 0), (105, 1), (106, 1), (164, 1), (165, 2)):
                clock.return_value = seconds
                self.assertFalse(_try_account(attempt))
                self.assertEqual(len(logs.records), warning_count)
            self.assertEqual([record.levelname for record in logs.records], ['WARNING', 'WARNING'])
            self.assertEqual([record.transition_message_pk for record in logs.records], [row.pk, row.pk])
            self.assertEqual([record.wait_seconds for record in logs.records], [5, 65])
            for record in logs.records:
                self.assertIn(f'TransitionMessage#{row.pk}', record.getMessage())
            row.refresh_from_db()
            self.assertEqual(row.errors_count, 0)
        self.assertTrue(_try_account(attempt))
        row.refresh_from_db()
        self.assertEqual(row.errors_count, 1)


class PendingAccountingTests(SimpleTestCase):
    """A reaped child leaves a pending write, never a live process slot."""

    @staticmethod
    def _attempt(pk):
        return _Attempt(pk=pk, timeout_seconds=None, deadline=None,
                        reaped=True, exit_code=3)

    @staticmethod
    def _busy_error():
        class LockUnavailable(Exception):
            sqlstate = '55P03'
        error = OperationalError('The row is locked.')
        error.__cause__ = LockUnavailable()
        return error

    def test_retries_stay_one_second_apart_across_harvest_calls(self):
        first, second = self._attempt(1), self._attempt(2)
        pending = deque([first, second])
        now = [100.0]
        calls = []

        def refuse(attempt, exit_code):
            calls.append((attempt.pk, now[0]))
            raise self._busy_error()

        with patch.object(pull, '_account', side_effect=refuse), \
                patch.object(pull.time, 'monotonic', side_effect=lambda: now[0]):
            for instant in (100, 100.01, 100.5, 100.99, 101, 101.01, 101.99, 102):
                now[0] = instant
                _harvest({}, pending, block=False)
        self.assertEqual(calls, [(1, 100), (2, 100), (1, 101), (2, 101), (1, 102), (2, 102)])
        self.assertEqual(list(pending), [first, second])

    def test_a_reused_process_id_cannot_replace_a_pending_error(self):
        old = _Attempt(pk=1, timeout_seconds=None, deadline=None)
        active, pending = {12345: old}, deque()
        attempts_recorded = []
        blocked = {1}

        def account(attempt, exit_code):
            if attempt.pk in blocked:
                raise self._busy_error()
            attempts_recorded.append((attempt.pk, exit_code))

        with patch.object(pull, '_account', side_effect=account), \
                patch.object(pull.time, 'monotonic', return_value=100):
            with patch.object(pull.os, 'waitpid', return_value=(12345, 3 << 8)):
                _harvest(active, pending, block=False)
            self.assertEqual(active, {})
            self.assertEqual(list(pending), [old])
            self.assertTrue(old.reaped)
            self.assertEqual(old.exit_code, 3)

            # This is an injected operating-system PID reuse, not a claim
            # that a real machine reused its PID during the test.
            replacement = _Attempt(pk=2, timeout_seconds=None, deadline=None)
            active[12345] = replacement
            with patch.object(pull.os, 'waitpid', return_value=(12345, 4 << 8)):
                _harvest(active, pending, block=False)
            self.assertEqual(active, {})
            self.assertEqual(list(pending), [old])
            self.assertEqual(attempts_recorded, [(2, 4)])
        blocked.clear()
        with patch.object(pull, '_account', side_effect=account), \
                patch.object(pull.time, 'monotonic', return_value=101):
            _harvest(active, pending, block=False)
            _harvest(active, pending, block=False)
        self.assertEqual(list(pending), [])
        self.assertEqual(attempts_recorded, [(2, 4), (1, 3)])

    def test_pending_writes_do_not_use_all_time_before_checking_an_expired_child(self):
        active = {12345: _Attempt(pk=99, timeout_seconds=1, deadline=99)}
        pending = deque(self._attempt(pk) for pk in range(1, 31))
        order = []

        def account(attempt, exit_code):
            order.append(('write', attempt.pk))
            raise self._busy_error()

        with patch.object(pull, '_account', side_effect=account), \
                patch.object(pull.time, 'monotonic', return_value=100), \
                patch.object(pull.os, 'kill', side_effect=lambda pid, sig: order.append(('kill', pid))), \
                patch.object(pull.os, 'waitpid', return_value=(0, 0)):
            _harvest(active, pending, block=False)
        self.assertEqual(order[0], ('kill', 12345))
        self.assertLessEqual(sum(kind == 'write' for kind, _ in order), 16)
        self.assertEqual(len(pending), 30)
        self.assertTrue(active[12345].killed)

    def test_retries_check_a_child_deadline_between_accounting_writes(self):
        now = [100.0]
        active = {12345: _Attempt(pk=99, timeout_seconds=1, deadline=100.1)}
        pending = deque([self._attempt(1), self._attempt(2)])
        order = []

        def account(attempt, exit_code):
            order.append(('write', attempt.pk))
            now[0] += 0.2
            raise self._busy_error()

        with patch.object(pull, '_account', side_effect=account), \
                patch.object(pull.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(pull.os, 'kill', side_effect=lambda pid, sig: order.append(('kill', pid))), \
                patch.object(pull.os, 'waitpid', return_value=(0, 0)):
            _harvest(active, pending, block=False)
        self.assertEqual(order, [('write', 1), ('kill', 12345), ('write', 2)])
        self.assertTrue(active[12345].killed)

    def test_retained_limit_stops_claims_then_releases_capacity_when_a_write_lands(self):
        class StopWorker(BaseException):
            pass

        now = [100.0]
        state = {'active': {}, 'pending': deque()}
        claimed, completed, exclusions = [], set(), []
        blocked = {1, 2}
        stopped_at_limit = []
        original_harvest = pull._harvest
        pauses = []

        def claim(queues, *, exclude_pks):
            excluded = set(exclude_pks)
            exclusions.append(excluded)
            for pk in (1, 2, 3):
                if pk not in excluded and pk not in completed:
                    self.assertNotIn(pk, claimed, 'A pending row was claimed again before accounting landed.')
                    if pk == 3:
                        self.assertEqual(stopped_at_limit, [True])
                        self.assertIn(2, excluded)
                    claimed.append(pk)
                    return pk
            return None

        def start(pk, active):
            state['active'] = active
            if pk is not None:
                active[9000 + pk] = _Attempt(pk=pk, timeout_seconds=None, deadline=None)

        def reap(pid, options):
            if not state['active']:
                raise ChildProcessError
            child_pid, attempt = next(iter(state['active'].items()))
            return child_pid, (0 if attempt.pk == 3 else 3 << 8)

        def account(attempt, exit_code):
            if attempt.pk in blocked:
                raise self._busy_error()
            if attempt.pk == 3:
                self.assertEqual([item.pk for item in state['pending']], [2])
            self.assertNotIn(attempt.pk, completed)
            completed.add(attempt.pk)

        def harvest(active, pending, **kwargs):
            state['active'], state['pending'] = active, pending
            return original_harvest(active, pending, **kwargs)

        def pause(seconds):
            pauses.append(seconds)
            self.assertLess(len(pauses), 30, 'The worker did not reuse the released capacity.')
            if 3 in completed:
                self.assertEqual([item.pk for item in state['pending']], [2])
                raise StopWorker
            if len(state['pending']) == 2 and blocked == {1, 2}:
                self.assertEqual(claimed, [1, 2])
                self.assertEqual(state['active'], {})
                stopped_at_limit.append(True)
                blocked.remove(1)
            now[0] += max(seconds, 1)

        with patch.object(pull, 'MAX_PENDING_ACCOUNTING', 2), \
                patch.object(pull, 'claim_next', side_effect=claim), \
                patch.object(pull, '_start_attempt', side_effect=start), \
                patch.object(pull, '_account', side_effect=account), \
                patch.object(pull, '_harvest', side_effect=harvest), \
                patch.object(pull.os, 'waitpid', side_effect=reap), \
                patch.object(pull.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(pull.time, 'sleep', side_effect=pause), \
                patch.object(pull, '_wait_for_work', side_effect=pause):
            with self.assertRaises(StopWorker):
                pull.run_worker(['django_logic'], concurrency=1)
        self.assertEqual(claimed, [1, 2, 3])
        self.assertEqual(completed, {1, 3})
        self.assertEqual([attempt.pk for attempt in state['pending']], [2])
        self.assertIn({1}, exclusions)
