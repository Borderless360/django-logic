"""A stopped job must not leave its nested synchronous work locked."""
import os
from pathlib import Path
import signal
import tempfile
import time

from django.test import TransactionTestCase, override_settings

from django_logic import Process, Transition
from django_logic.background import BackgroundTransition
from django_logic.background.models import TransitionMessage
from django_logic.background.pull import run_once
from django_logic.state import State
from tests import dl_settings
from tests.background.models import Widget
from tests.stability.base import requires_postgres, requires_real_redis


def stop_first_call(instance, *, marker_path, stop, **kwargs):
    marker = Path(marker_path)
    if marker.exists():
        return
    marker.write_text('The nested transition started.\n')
    if stop == 'crash':
        os.kill(os.getpid(), signal.SIGKILL)
    time.sleep(30)


class NestedStopProcess(Process):
    process_name = 'nested_stop'
    transitions = [Transition('finish', sources=['draft'], target='finished',
                              side_effects=[stop_first_call])]


def finish_nested(instance, *, nested_pk, marker_path, stop, **kwargs):
    nested = Widget.objects.get(pk=nested_pk)
    NestedStopProcess(instance=nested, field_name='status').finish(marker_path=marker_path, stop=stop)


class ParentStopProcess(Process):
    process_name = 'parent_stop'
    transitions = [BackgroundTransition('finish', sources=['draft'], target='finished',
                                        queue='nested_stop', timeout=1,
                                        side_effects=[finish_nested])]


@requires_postgres
@requires_real_redis
@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull',
                                            TRANSITION_MESSAGE_RETRY_MINUTES=0))
class NestedProcessStopTests(TransactionTestCase):
    def tearDown(self):
        super().tearDown()

    def check_stopped_job(self, stop):
        parent = Widget.objects.create(status='draft')
        nested = Widget.objects.create(status='draft')
        with tempfile.TemporaryDirectory(prefix='dl-nested-stop-') as directory:
            marker = str(Path(directory) / 'started')
            ParentStopProcess(instance=parent, field_name='status').finish(
                nested_pk=nested.pk, marker_path=marker, stop=stop)
            self.assertTrue(run_once(['nested_stop'], isolate=True))
            self.assertTrue(Path(marker).exists(), 'The real job process must enter the nested transition.')
            row = TransitionMessage.objects.get()
            self.assertEqual(row.errors_count, 1)
            self.assertIn('[timeout]' if stop == 'timeout' else '[crashed]', row.last_error_message)
            nested.refresh_from_db()
            self.assertEqual(nested.status, 'draft')
            self.assertTrue(run_once(['nested_stop'], isolate=True))
            parent.refresh_from_db()
            nested.refresh_from_db()
            row.refresh_from_db()
            self.assertEqual((parent.status, nested.status), ('finished', 'finished'))
            self.assertTrue(row.is_completed)
            self.assertFalse(row.ended_in_failure)
            self.assertEqual(row.errors_count, 1)

    def test_timeout_releases_nested_work_for_the_same_jobs_retry(self):
        self.check_stopped_job('timeout')

    def test_crash_releases_nested_work_for_the_same_jobs_retry(self):
        self.check_stopped_job('crash')


    def test_callback_can_start_the_next_transition(self):
        widget = Widget.objects.create(status='draft')
        next_step = Transition('second', sources=['first'], target='finished')

        def continue_work(instance, **kwargs):
            next_step.change_state(State(instance, 'status', process_name='nested_stop'))

        first = Transition('first', sources=['draft'], target='first', callbacks=[continue_work])
        first.change_state(State(widget, 'status', process_name='nested_stop'))
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'finished')
