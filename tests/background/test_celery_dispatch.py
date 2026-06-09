"""Celery-mode dispatch coverage.

The default suite runs in sync mode, so the celery branches of
``dispatch_transition`` (transaction.on_commit + apply_async) and
``_retry_pending_inline`` (re-route a stale row to its own queue) were
previously never executed. These tests run in celery mode with
``apply_async`` mocked — no broker required. ``captureOnCommitCallbacks``
fires the on_commit callbacks so the enqueue actually happens.
"""
from datetime import timedelta
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from django_logic.background.models import TransitionMessage
from django_logic.background.tasks import _retry_pending_inline
from tests.background.models import Widget


_CELERY_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'celery',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 3,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 0,  # cutoff == now
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}

_APPLY_ASYNC = (
    'django_logic.background.tasks.'
    'run_background_transition_task.apply_async'
)


@override_settings(DJANGO_LOGIC=_CELERY_SETTINGS)
class CeleryDispatchTests(TestCase):
    def test_phase_one_enqueues_on_commit_with_queue_routing(self):
        widget = Widget.objects.create()
        with patch(_APPLY_ASYNC) as mock_async:
            with self.captureOnCommitCallbacks(execute=True):
                tr_id = widget.process.fulfil()
            self.assertIsNotNone(tr_id)

        # Celery mode does NOT run phase 2 inline: the row stays uncompleted
        # and the state remains in_progress until a worker drains the queue.
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'fulfilling')
        tm = TransitionMessage.objects.get(transition_name='fulfil')
        self.assertFalse(tm.is_completed)

        # The task is enqueued exactly once, routed to the transition's own
        # declared queue — the "no default queue" guarantee. `shadow` gives the
        # dispatch a per-transition name in Celery events / Flower (issue #78).
        mock_async.assert_called_once_with(
            args=[tm.pk], queue='django_logic.critical',
            shadow='django_logic.bg_tests.fulfil',
        )

    def test_no_dispatch_when_phase_one_transaction_rolls_back(self):
        widget = Widget.objects.create()
        with patch(_APPLY_ASYNC) as mock_async:
            with self.assertRaises(RuntimeError):
                with self.captureOnCommitCallbacks(execute=True):
                    with transaction.atomic():
                        widget.process.fulfil()
                        raise RuntimeError('roll back phase 1')
            # The on_commit enqueue is discarded with the rolled-back txn.
            mock_async.assert_not_called()


@override_settings(DJANGO_LOGIC=_CELERY_SETTINGS)
class CeleryRetryRoutingTests(TestCase):
    """The periodic retry re-dispatches each stale row to its OWN queue
    (a slow export never lands on the critical queue)."""

    def _make_stale(self, widget, queue, transition_name):
        tm = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(widget.pk),
            process_name='process',
            transition_name=transition_name,
            queue_name=queue,
            kwargs={},
        )
        TransitionMessage.objects.filter(pk=tm.pk).update(
            created=timezone.now() - timedelta(minutes=5),
        )
        return tm

    def test_retry_redispatches_each_row_to_its_own_queue(self):
        w1 = Widget.objects.create(status='fulfilling')
        w2 = Widget.objects.create(status='exporting')
        tm1 = self._make_stale(w1, 'django_logic.critical', 'fulfil')
        tm2 = self._make_stale(w2, 'django_logic.slow', 'generate_export')

        with patch(_APPLY_ASYNC) as mock_async:
            dispatched = _retry_pending_inline()

        self.assertEqual(dispatched, 2)
        routed = {
            tuple(c.kwargs['args']): c.kwargs['queue']
            for c in mock_async.call_args_list
        }
        self.assertEqual(
            routed,
            {(tm1.pk,): 'django_logic.critical',
             (tm2.pk,): 'django_logic.slow'},
        )
        # Nothing ran inline in celery mode.
        w1.refresh_from_db()
        self.assertEqual(w1.status, 'fulfilling')

    def test_dispatch_error_on_one_row_does_not_stop_the_scan(self):
        w1 = Widget.objects.create(status='fulfilling')
        w2 = Widget.objects.create(status='exporting')
        self._make_stale(w1, 'django_logic.critical', 'fulfil')
        self._make_stale(w2, 'django_logic.slow', 'generate_export')

        # First apply_async raises (broker hiccup); the loop must continue.
        with patch(_APPLY_ASYNC, side_effect=[Exception('broker down'), None]):
            dispatched = _retry_pending_inline()

        # One failed, one succeeded — the scan didn't abort on the first.
        self.assertEqual(dispatched, 1)
