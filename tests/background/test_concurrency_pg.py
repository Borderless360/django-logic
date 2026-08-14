"""Concurrency coverage for the durable runner. PostgreSQL only.

SQLite ignores ``select_for_update(nowait=True)`` and serialises every writer,
so the row lock and the race between two enqueues only mean something on
PostgreSQL. These tests run in the PostgreSQL CI job and skip elsewhere.

They pin two claims: no two workers run the same transition at once, and only
one uncompleted TransitionMessage exists per instance per process.
"""
import threading

from django.db import connections, transaction
from django.test import TransactionTestCase, override_settings

from django_logic.background.models import TransitionMessage
from django_logic.background.runner import run_background_transition
from django_logic.exceptions import TransitionNotAllowed
from tests.background.models import Widget
from tests.stability.base import requires_postgres, run_concurrent
from tests import dl_settings


_SYNC_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=3)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
@requires_postgres
class ExecuteRowLockTests(TransactionTestCase):
    """A second worker that reaches a row another worker holds must skip it.
    No side-effect runs twice, and the row stays for the worker holding it."""

    databases = '__all__'

    def test_second_worker_skips_locked_row(self):
        widget = Widget.objects.create(status='fulfilling')
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(widget.pk),
            process_name='process',
            transition_name='fulfil',
            queue_name='django_logic.critical',
            kwargs={},
        )

        lock_held = threading.Event()
        release = threading.Event()

        def hold_row():
            try:
                with transaction.atomic():
                    # Worker A grabs and holds the row lock.
                    TransitionMessage.objects.select_for_update().get(pk=transition_message.pk)
                    lock_held.set()
                    release.wait(timeout=10)
            finally:
                connections.close_all()

        holder = threading.Thread(target=hold_row)
        holder.start()
        try:
            self.assertTrue(lock_held.wait(timeout=10))
            # Worker B tries execute while A holds the lock — must no-op.
            run_background_transition(transition_message.pk)
        finally:
            release.set()
            holder.join(timeout=10)

        transition_message.refresh_from_db()
        self.assertFalse(transition_message.is_completed)        # B did not complete it
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'fulfilling')
        self.assertEqual(widget.se_log, '')       # bg_ok never ran in B


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
@requires_postgres
class ConcurrentPhaseOneTests(TransactionTestCase):
    """Two enqueue calls racing on the same instance: exactly one wins.

    Since 0.4 the loser is rejected by whichever guard it reaches first:
    the cache lock enqueue takes (``TransitionNotAllowed: State is locked``) or
    the partial unique constraint (``AlreadyInProgress``, a
    ``TransitionNotAllowed`` subclass). Both are correct rejections; the
    invariants are exactly one winner and exactly one TransitionMessage.
    """

    databases = '__all__'

    def test_two_concurrent_enqueues_only_one_wins(self):
        widget = Widget.objects.create(status='draft')

        def fulfil():
            fresh = Widget.objects.get(pk=widget.pk)
            return fresh.process.fulfil()

        results = run_concurrent(fulfil, n_threads=2)
        wins = [r for r, e in results if e is None]
        errors = [e for r, e in results if e is not None]

        self.assertEqual(len(wins), 1, results)
        self.assertEqual(len(errors), 1, results)
        self.assertIsInstance(errors[0], TransitionNotAllowed)

        # Exactly one TransitionMessage exists for the instance.
        self.assertEqual(
            TransitionMessage.objects.filter(
                instance_id=str(widget.pk)
            ).count(),
            1,
        )
