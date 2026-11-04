"""A worker observation explains missing progress without hiding it."""
from datetime import timedelta
from io import StringIO
import os
import time
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.db import connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from django_logic import Process
from django_logic.background import BackgroundTransition, queue_worker_presence
from django_logic.background import pull, safety_nets, worker_presence
from django_logic.background.models import TransitionMessage
from django_logic.background.worker_presence import record_worker_presence
from django_logic.testing import open_transition_message
from tests import dl_settings
from tests.background.models import Widget
from tests.stability.base import requires_postgres, requires_real_redis


class WorkerPresenceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _waiting_job(self):
        widget = Widget.objects.create(status='fulfilling')
        row = open_transition_message(widget, 'process', 'fulfil', queue_name='presence.waiting')
        TransitionMessage.objects.filter(pk=row.pk).update(created=timezone.now() - timedelta(hours=1))
        return row

    def test_worker_presence_does_not_downgrade_a_queue_with_no_progress(self):
        row = self._waiting_job()
        before = list(TransitionMessage.objects.values())
        record_worker_presence(['presence.busy', 'presence.waiting'])
        self.assertTrue(queue_worker_presence('presence.waiting'))
        with self.assertLogs('django-logic', level='ERROR') as captured:
            safety_nets.detect_stuck_transitions()
        self.assertEqual(len(captured.records), 1)
        self.assertIn('recent worker presence', captured.output[0])
        self.assertIn('no progress', captured.output[0])
        self.assertNotIn('Start one', captured.output[0])
        output = StringIO()
        call_command('dl_transitions', stdout=output)
        self.assertIn('recent worker presence but no queue progress', output.getvalue())
        self.assertIn(f'#{row.pk}', output.getvalue())
        self.assertEqual(list(TransitionMessage.objects.values()), before)

    def test_a_missing_worker_still_names_the_queue_to_start(self):
        self._waiting_job()
        with self.assertLogs('django-logic', level='ERROR') as captured:
            safety_nets.detect_stuck_transitions()
        self.assertIn('dl_worker --queues presence.waiting', captured.output[0])

    def test_cache_failure_keeps_database_progress_evidence_and_maintenance(self):
        self._waiting_job()
        observed = Widget.objects.create(status='fulfilled')
        recent = open_transition_message(observed, 'process', 'fulfil', queue_name='presence.waiting')
        TransitionMessage.objects.filter(pk=recent.pk).update(is_completed=True, started_at=timezone.now())
        with patch.object(worker_presence.cache, 'get', side_effect=ConnectionError), \
                patch.object(worker_presence.cache, 'set_many', side_effect=ConnectionError), \
                self.assertLogs('django-logic', level='WARNING') as captured:
            self.assertIsNone(queue_worker_presence('presence.waiting'))
            pull._run_safety_nets(['presence.waiting'])
        self.assertTrue(any('could not be saved' in text for text in captured.output))
        self.assertTrue(any('is behind' in text for text in captured.output))
        self.assertFalse(any(record.levelname == 'ERROR' for record in captured.records))

    def test_empty_queue_list_does_not_access_the_cache(self):
        with patch.object(worker_presence.cache, 'set_many') as write:
            record_worker_presence([])
        write.assert_not_called()


def wait_for_worker_observation(instance, **kwargs):
    deadline = time.monotonic() + 4
    while not queue_worker_presence('presence.spare'):
        if time.monotonic() >= deadline:
            raise TimeoutError('The full worker did not report its other queue.')
        time.sleep(0.01)
    output = StringIO()
    call_command('dl_transitions', queues='presence.spare', stdout=output)
    instance.se_log = output.getvalue()
    instance.save(update_fields=['se_log'])


class WorkerPresenceProcess(Process):
    process_name = 'process'
    transitions = [
        BackgroundTransition(
            action_name='wait', sources=['draft'], target='fulfilled',
            in_progress_state='fulfilling', queue='presence.busy', timeout=5,
            side_effects=[wait_for_worker_observation],
        ),
    ]


@requires_postgres
@requires_real_redis
@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class WorkerPresenceProcessTests(TransactionTestCase):
    """Use real commits and shared Redis for separate worker processes."""
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_full_worker_reports_both_queues_before_its_job_finishes(self):
        busy = Widget.objects.create(status='draft')
        WorkerPresenceProcess(instance=busy, field_name='status').wait()
        busy_job = TransitionMessage.objects.get(instance_id=str(busy.pk))
        spare = Widget.objects.create(status='fulfilling')
        spare_job = open_transition_message(spare, 'process', 'fulfil', queue_name='presence.spare')
        TransitionMessage.objects.filter(pk=busy_job.pk).update(created=timezone.now() - timedelta(hours=2))
        TransitionMessage.objects.filter(pk=spare_job.pk).update(created=timezone.now() - timedelta(hours=1))

        pull.run_worker(['presence.busy', 'presence.spare'], forever=False, concurrency=1)

        busy.refresh_from_db()
        spare.refresh_from_db()
        self.assertEqual(busy.status, 'fulfilled')
        self.assertEqual(spare.status, 'fulfilled')
        self.assertIn('recent worker presence but no queue progress', busy.se_log)
        self.assertTrue(queue_worker_presence('presence.busy'))
        self.assertTrue(queue_worker_presence('presence.spare'))

    def test_hung_cache_write_does_not_delay_a_job_timeout(self):
        widget = Widget.objects.create(status='fulfilling')
        widget.process.hang()
        row = TransitionMessage.objects.get(instance_id=str(widget.pk))
        TransitionMessage.objects.filter(pk=row.pk).update(timeout_seconds=1)
        started = time.monotonic()
        with patch.object(worker_presence.cache, 'set_many', side_effect=lambda *args, **kwargs: time.sleep(30)), \
                patch.object(pull, 'SAFETY_NET_TIMEOUT_SECONDS', 0.5):
            pull.run_worker([row.queue_name], forever=False)
        row.refresh_from_db()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(row.errors_count, 1)
        self.assertIn('timeout', row.last_error_message)

    def observe_shared_queue(self):
        connections.close_all()
        child = os.fork()
        if child == 0:
            exit_code = 1
            try:
                pull.run_worker(['presence.shared'], forever=False)
                exit_code = 0
            finally:
                os._exit(exit_code)
        _, status = os.waitpid(child, 0)
        self.assertEqual(status, 0)

    def test_one_stopped_worker_does_not_clear_another_workers_observation(self):
        with patch.object(worker_presence.conf, 'retry_window_minutes', return_value=1 / 60):
            self.observe_shared_queue()
            self.assertTrue(queue_worker_presence('presence.shared'))
            time.sleep(0.65)
            self.observe_shared_queue()
            time.sleep(0.5)
            self.assertTrue(queue_worker_presence('presence.shared'))
            deadline = time.monotonic() + 2
            while queue_worker_presence('presence.shared') and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(queue_worker_presence('presence.shared'))

    def test_worker_error_after_presence_is_written_fails_the_observation_check(self):
        original_worker = pull.run_worker

        def fail_after_worker_returns(*args, **kwargs):
            original_worker(*args, **kwargs)
            raise RuntimeError('The worker failed after writing its presence.')

        with patch.object(pull, 'run_worker', side_effect=fail_after_worker_returns):
            with self.assertRaises(AssertionError):
                self.observe_shared_queue()
        self.assertTrue(queue_worker_presence('presence.shared'))

    def test_failed_maintenance_observation_expires_after_worker_stops(self):
        with patch.object(worker_presence.conf, 'retry_window_minutes', return_value=1 / 60), \
                patch.object(safety_nets, 'detect_stuck_transitions', side_effect=lambda: os._exit(17)), \
                self.assertLogs('django-logic', level='WARNING') as captured:
            pull.run_worker(['presence.failed'], forever=False)
        self.assertTrue(any('safety nets stopped with exit code 17' in text for text in captured.output))
        self.assertTrue(queue_worker_presence('presence.failed'))
        deadline = time.monotonic() + 2
        while queue_worker_presence('presence.failed') and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(queue_worker_presence('presence.failed'))
