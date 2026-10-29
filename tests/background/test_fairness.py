"""Different clients make progress in a shared worker pool."""
import threading
from datetime import timedelta

from django.db import connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from django_logic.background.fairness import FairnessGroup
from django_logic.background.models import TransitionMessage
from django_logic.background.pull import claim_next
from tests import dl_settings
from tests.stability.base import requires_postgres


@requires_postgres
@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class FairClaimTests(TransactionTestCase):
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

    def test_unkeyed_legacy_rows_keep_fifo_and_share_capacity(self):
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
