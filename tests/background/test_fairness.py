"""Different clients make progress in a shared worker pool."""
import threading
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.db import connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from django_logic import Process
from django_logic.background import BackgroundTransition, Defer
from django_logic.background.fairness import FairnessGroup
from django_logic.background.models import TransitionMessage
from django_logic.background.pull import claim_next
from django_logic.background.runner import run_background_transition
from tests import dl_settings
from tests.background.models import Widget
from tests.stability.base import requires_postgres


def client_key(instance, **kwargs):
    return instance.se_log


def wait_for_ready_work(instance, dependency_id=None, **kwargs):
    if dependency_id and not Widget.objects.filter(pk=dependency_id, status='done').exists():
        raise Defer(until=timezone.now() + timedelta(seconds=10), reason='Waiting for the other job.')


class ReadyWorkProcess(Process):
    process_name = 'ready_work'
    transitions = [
        BackgroundTransition(
            action_name='finish', sources=['draft'], target='done',
            side_effects=[wait_for_ready_work], fairness_key=client_key, queue='fair-test',
        ),
    ]


@requires_postgres
@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class FairClaimTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_submitted_work_runs_without_a_precreated_client_turn_record(self):
        widget = Widget.objects.create()
        widget.process.fulfil()
        message = TransitionMessage.objects.get(instance_id=str(widget.pk))
        self.assertFalse(FairnessGroup.objects.exists())

        self.assertEqual(claim_next([message.queue_name]), message.pk)
        self.assertTrue(FairnessGroup.objects.filter(key=message.fairness_key).exists())
        run_background_transition(message.pk)
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'fulfilled')

    def rows(self, keys, queue='fair-test'):
        offset = TransitionMessage.objects.count()
        return TransitionMessage.objects.bulk_create([
            TransitionMessage(
                app_label='background_tests', model_name='widget',
                instance_id=str(offset + index), process_name='process',
                transition_name='fulfil', queue_name=queue, fairness_key=key,
            ) for index, key in enumerate(keys)
        ])

    def take(self, queues=None):
        pk = claim_next(queues or ['fair-test'])
        if pk is None:
            return None
        row = TransitionMessage.objects.get(pk=pk)
        TransitionMessage.objects.filter(pk=pk).update(is_completed=True)
        return row

    def test_thousand_small_clients_progress_before_large_client_second_turn(self):
        self.rows(['0-large'] * 1000 + [f'small-{i:04}' for i in range(1000)])
        first_round = [self.take().fairness_key for _ in range(1001)]
        self.assertEqual(len(set(first_round)), 1001)
        self.assertEqual(first_round.count('0-large'), 1)
        self.assertEqual(self.take().fairness_key, '0-large')
        self.assertEqual(TransitionMessage.objects.filter(is_completed=False).count(), 998)

    def test_one_client_can_fill_every_free_slot(self):
        rows = self.rows(['only-client'] * 8)
        claimed = []
        for row in rows:
            pk = claim_next(['fair-test'], exclude_pks=claimed)
            self.assertEqual(pk, row.pk)
            claimed.append(pk)
        self.assertIsNone(claim_next(['fair-test'], exclude_pks=claimed))

    def test_unkeyed_rows_keep_fifo_and_share_capacity(self):
        rows = self.rows(['', '', 'new-client'])
        claimed = [self.take().pk for _ in range(3)]
        self.assertEqual(claimed, [rows[0].pk, rows[2].pk, rows[1].pk])

    def test_deferred_and_other_queue_jobs_do_not_take_turns(self):
        deferred = self.rows(['a-deferred'])[0]
        TransitionMessage.objects.filter(pk=deferred.pk).update(
            eligible_at=timezone.now() + timedelta(hours=1),
        )
        self.rows(['b-other'], queue='other')
        runnable = self.rows(['c-ready'])[0]
        self.assertEqual(self.take().pk, runnable.pk)
        self.assertIsNone(self.take())
        self.assertIsNone(FairnessGroup.objects.filter(key='a-deferred').first())

    def test_repeated_defer_does_not_delay_its_ready_dependency(self):
        self.run_dependency_case(atomic=True)

    def test_non_atomic_repeated_defer_does_not_delay_its_ready_dependency(self):
        self.run_dependency_case(atomic=False)

    def run_dependency_case(self, *, atomic):
        clock = [timezone.now()]
        waiting = Widget.objects.create(se_log='shared-client')
        dependency = Widget.objects.create(se_log='shared-client')
        with patch('django.utils.timezone.now', side_effect=lambda: clock[0]), patch.object(
            ReadyWorkProcess.transitions[0], 'atomic', atomic,
        ):
            ReadyWorkProcess(field_name='status', instance=waiting).finish(dependency_id=dependency.pk)
            waiting_job = TransitionMessage.objects.get(instance_id=str(waiting.pk))
            for seconds in (11, 1):
                self.assertEqual(claim_next(['fair-test']), waiting_job.pk)
                run_background_transition(waiting_job.pk)
                clock[0] += timedelta(seconds=seconds)
            waiting_job.refresh_from_db()
            self.assertEqual((waiting_job.deferral_count, waiting_job.errors_count), (2, 0))

            # The dependency arrives before the second postponement ends.
            ReadyWorkProcess(field_name='status', instance=dependency).finish()
            dependency_job = TransitionMessage.objects.get(instance_id=str(dependency.pk))
            for client in range(12):
                for _ in range(4):
                    other = Widget.objects.create(se_log=f'client-{client:02}')
                    ReadyWorkProcess(field_name='status', instance=other).finish()

            first_round = []
            for _ in range(13):
                chosen = claim_next(['fair-test'])
                self.assertIsNotNone(chosen)
                first_round.append(TransitionMessage.objects.get(pk=chosen))
                run_background_transition(chosen)
                clock[0] += timedelta(seconds=2)
            self.assertEqual(len({row.fairness_key for row in first_round}), 13)
            self.assertEqual(
                [row.pk for row in first_round if row.fairness_key == 'shared-client'],
                [dependency_job.pk],
                'The ready dependency must run before the older job checks it again.',
            )
            self.assertTrue(TransitionMessage.objects.filter(
                is_completed=False, fairness_key__startswith='client-',
            ).exists())

            for _ in range(39):
                chosen = claim_next(['fair-test'])
                if chosen is None:
                    break
                run_background_transition(chosen)
                clock[0] += timedelta(seconds=2)
            self.assertFalse(TransitionMessage.objects.filter(is_completed=False).exists())
            waiting.refresh_from_db()
            dependency.refresh_from_db()
            waiting_job.refresh_from_db()
            self.assertEqual((waiting.status, dependency.status), ('done', 'done'))
            self.assertEqual((waiting_job.start_attempt_count, waiting_job.deferral_count), (3, 2))
            self.assertFalse(TransitionMessage.objects.filter(errors_count__gt=0).exists())

    def test_due_postponed_job_runs_between_earlier_and_later_ready_work(self):
        postponed, earlier, later = self.rows(['same'] * 3)
        now = timezone.now()
        for row, seconds in ((postponed, 30), (earlier, 20), (later, 5)):
            TransitionMessage.objects.filter(pk=row.pk).update(created=now - timedelta(seconds=seconds))
        TransitionMessage.objects.filter(pk=postponed.pk).update(eligible_at=now - timedelta(seconds=10))
        self.assertEqual([self.take().pk for _ in range(3)], [earlier.pk, postponed.pk, later.pk])

    def test_unkeyed_postponed_job_allows_earlier_ready_work_to_run(self):
        postponed, ready = self.rows(['', ''])
        TransitionMessage.objects.filter(pk=postponed.pk).update(eligible_at=timezone.now())
        self.assertEqual([self.take().pk for _ in range(2)], [ready.pk, postponed.pk])

    def test_old_eligibility_date_does_not_move_a_newer_job_ahead(self):
        first, second = self.rows(['same', 'same'])
        TransitionMessage.objects.filter(pk=second.pk).update(eligible_at=first.created - timedelta(days=1))
        self.assertEqual([self.take().pk for _ in range(2)], [first.pk, second.pk])

    def test_error_retry_wait_still_applies_within_one_client(self):
        retry, ready = self.rows(['same', 'same'])
        now = timezone.now()
        TransitionMessage.objects.filter(pk=retry.pk).update(errors_count=1, last_error_dt=now)
        with patch('django.utils.timezone.now', return_value=now):
            self.assertEqual(self.take().pk, ready.pk)
            self.assertIsNone(self.take())
        with patch('django.utils.timezone.now', return_value=now + timedelta(minutes=3)):
            self.assertEqual(self.take().pk, retry.pk)

    def test_locked_client_does_not_block_other_clients(self):
        first, second = self.rows(['a', 'b'])
        ready, release = threading.Event(), threading.Event()

        def hold():
            try:
                with transaction.atomic():
                    TransitionMessage.objects.select_for_update().get(pk=first.pk)
                    ready.set()
                    release.wait(10)
            finally:
                connections.close_all()

        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(ready.wait(5))
            self.assertEqual(self.take().pk, second.pk)
        finally:
            release.set()
            thread.join(10)
        self.assertEqual(self.take().pk, first.pk)

    def test_a_second_worker_uses_shared_turn_order(self):
        self.rows(['a'] * 4 + ['b'] * 4)
        first = self.take()
        result, failures = [], []

        def another_worker():
            try:
                result.append(self.take().fairness_key)
            except BaseException as exc:
                failures.append(exc)
            finally:
                connections.close_all()

        thread = threading.Thread(target=another_worker)
        thread.start()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(first.fairness_key, 'a')
        self.assertEqual(result, ['b'])
        self.assertEqual(self.take().fairness_key, 'a')
