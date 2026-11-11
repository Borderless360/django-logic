"""Empty state values remain distinct from an action without a state change."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import skipUnless

from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import close_old_connections, connection, connections, transaction
from django.test import TestCase, TransactionTestCase

from django_logic import Transition
from django_logic.background.models import TransitionMessage
from django_logic.exceptions import TransitionNotAllowed, TransitionTemporarilyUnavailable
from django_logic.state import State
from tests.background.models import Widget


class EmptyTargetTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.widget = Widget.objects.create(status='draft')

    def test_reset_saves_an_empty_status(self):
        self.widget.empty_status_proc.reset()

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, '')

    def test_no_target_preserves_the_status(self):
        self.widget.empty_status_proc.touch()

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')
        self.assertEqual(self.widget.se_log, 'ok,')

    def test_reset_does_not_overwrite_a_newer_saved_status(self):
        Widget.objects.filter(pk=self.widget.pk).update(status='approved')

        with self.assertRaises(TransitionNotAllowed):
            self.widget.empty_status_proc.reset()

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'approved')

    def test_reset_rolls_back_with_the_enclosing_operation(self):
        with self.assertRaisesRegex(ValueError, 'operation failed'):
            with transaction.atomic():
                self.widget.empty_status_proc.reset()
                raise ValueError('operation failed')

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')

    def test_empty_target_still_requires_state_protection(self):
        with self.assertRaisesRegex(ImproperlyConfigured, 'lock=False needs target=None'):
            Transition('unsafe_reset', sources=['draft'], target='', lock=False)

    def test_another_operation_keeps_its_lock_when_reset_is_refused(self):
        other = State(Widget.objects.get(pk=self.widget.pk), 'status', 'empty_status_proc')
        self.assertTrue(other.lock())
        try:
            with self.assertRaises(TransitionNotAllowed):
                self.widget.empty_status_proc.reset()
            self.assertTrue(other.is_locked())
        finally:
            other.unlock()

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')

    def test_background_completion_can_clear_its_in_progress_status(self):
        self.widget.empty_status_proc.reset_later()

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, '')
        self.assertEqual(self.widget.se_log, 'ok,')
        message = TransitionMessage.objects.get(
            instance_id=str(self.widget.pk), process_name='empty_status_proc',
        )
        self.assertTrue(message.is_completed)


@skipUnless(connection.vendor == 'postgresql', 'This test needs PostgreSQL row locking.')
class ConcurrentEmptyTargetTests(TransactionTestCase):
    def test_reset_waits_for_another_writer_and_cannot_overwrite_its_result(self):
        cache.clear()
        self.addCleanup(cache.clear)
        widget = Widget.objects.create(status='draft')
        locked, release = Event(), Event()

        def save_newer_status():
            close_old_connections()
            try:
                with transaction.atomic():
                    current = Widget.objects.select_for_update().get(pk=widget.pk)
                    current.status = 'approved'
                    current.save(update_fields=['status'])
                    locked.set()
                    if not release.wait(timeout=10):
                        raise AssertionError('The reset did not finish its lock check.')
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as executor:
            writer = executor.submit(save_newer_status)
            try:
                self.assertTrue(locked.wait(timeout=5))
                with self.assertRaises(TransitionTemporarilyUnavailable):
                    widget.empty_status_proc.reset()
            finally:
                release.set()
            writer.result(timeout=10)

        with self.assertRaises(TransitionNotAllowed):
            widget.empty_status_proc.reset()
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'approved')
