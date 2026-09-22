"""Crash accounting must not wait behind another worker's row lock."""
import os
import signal
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from django.db import connections, transaction
from django.test import TransactionTestCase

from django_logic.background.models import TransitionMessage
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
        attempts = {
            999999: _Attempt(
                pk=row.pk, timeout_seconds=None, deadline=None,
                reaped=True, exit_code=3,
            ),
        }
        with self._locked_row(row) as finished, \
                patch('django_logic.background.pull.logger.error') as log_error, \
                patch('django_logic.background.pull.logger.warning') as log_warning:
            started = time.monotonic()
            _harvest(attempts, block=False)
            self.assertLess(time.monotonic() - started, 2)
            self.assertFalse(finished.is_set())
            self.assertEqual(len(attempts), 1)
            log_error.assert_not_called()
            log_warning.assert_not_called()
            row.refresh_from_db()
            self.assertEqual(row.errors_count, 0)

        _harvest(attempts, block=False)
        _harvest(attempts, block=False)
        self.assertEqual(attempts, {})
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

        attempts = {
            999999: _Attempt(
                pk=locked_row.pk, timeout_seconds=None, deadline=None,
                reaped=True, exit_code=3,
            ),
            sibling_pid: _Attempt(
                pk=sibling_row.pk, timeout_seconds=0.2,
                deadline=time.monotonic() + 0.2,
            ),
        }
        try:
            with self._locked_row(locked_row) as finished:
                started = time.monotonic()
                while sibling_pid in attempts and time.monotonic() - started < 2:
                    _harvest(attempts, block=False)
                    time.sleep(0.01)
                self.assertLess(time.monotonic() - started, 2)
                self.assertNotIn(sibling_pid, attempts)
                self.assertIn(999999, attempts)
                self.assertFalse(finished.is_set())
                sibling_row.refresh_from_db()
                self.assertEqual(sibling_row.errors_count, 1)
                self.assertIn('[timeout]', sibling_row.last_error_message)

            _harvest(attempts, block=False)
            self.assertEqual(attempts, {})
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
