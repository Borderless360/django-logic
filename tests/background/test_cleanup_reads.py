"""A cleanup pass reads through its two indexes, not through the whole job table."""
from datetime import timedelta

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from django_logic.background.models import TransitionMessage
from django_logic.background.safety_nets import cleanup_completed_transitions
from tests import dl_settings
from tests.stability.base import requires_postgres


def _completed_jobs(count, *, days_ago, failed=False, record='record'):
    moment = timezone.now() - timedelta(days=days_ago)
    rows = TransitionMessage.objects.bulk_create([
        TransitionMessage(
            app_label='bg_tests', model_name='widget', instance_id=f'{record}-{index}',
            process_name='cleanup_test', transition_name='finished',
            queue_name='django_logic.cleanup_test', is_completed=True, ended_in_failure=failed,
            completed_at=moment,
        ) for index in range(count)
    ])
    TransitionMessage.objects.filter(pk__in=[row.pk for row in rows]).update(modified=moment)


def _indexes_read(plan):
    names = {plan['Index Name']} if 'Index Name' in plan else set()
    for child in plan.get('Plans', []):
        names |= _indexes_read(child)
    return names


@requires_postgres
@override_settings(DJANGO_LOGIC=dl_settings(TRANSITION_MESSAGE_CLEANUP_DAYS=7))
class CleanupReadTests(TestCase):
    def test_a_pass_with_nothing_to_delete_reads_through_the_cleanup_indexes(self):
        _completed_jobs(40, days_ago=1)
        _completed_jobs(3, days_ago=1, failed=True, record='recent-failure')
        _completed_jobs(5, days_ago=30, failed=True, record='kept-failure')

        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(cleanup_completed_transitions(), 0)

        reads = [query['sql'] for query in queries if query['sql'].startswith('SELECT')]
        self.assertEqual(len(reads), 2, reads)
        plans = []
        with connection.cursor() as cursor:
            # PostgreSQL reads a table this small whole. These settings show the plan of a large table.
            for setting in ('enable_seqscan', 'enable_bitmapscan', 'enable_sort'):
                cursor.execute(f'SET LOCAL {setting} = off')
            for read in reads:
                cursor.execute('EXPLAIN (FORMAT JSON) ' + read)
                plans.append(cursor.fetchone()[0][0]['Plan'])
        jobs_that_did_not_fail, replaced_failures = plans
        # The read by age holds no failed job, so a kept failure costs this read nothing.
        self.assertEqual(_indexes_read(jobs_that_did_not_fail), {'dl_bg_cleanup_age_idx'})
        self.assertIn('dl_bg_failure_idx', _indexes_read(replaced_failures))
