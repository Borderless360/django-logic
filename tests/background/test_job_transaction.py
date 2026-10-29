"""A job stops when its original database transaction ends unexpectedly."""
from contextlib import contextmanager
from unittest.mock import patch
from django.core.cache import cache
from django.db import connections
from django.test import TransactionTestCase, override_settings

from django_logic import Process
from django_logic.background import BackgroundTransition
from django_logic.background.models import TransitionMessage
from django_logic.background.non_atomic import JobOwnershipLost
from django_logic.background.runner import run_background_transition
from tests import dl_settings
from tests.background.models import Widget
from tests.stability.base import requires_postgres

_test_job_alias = None


def record_receipt_and_end_transaction(instance, **kwargs):
    instance.se_log = 'confirmed'
    instance.save(update_fields=['se_log'])
    job = connections[_test_job_alias]
    # Model a connection wrapper unexpectedly committing the lock transaction.
    # The same connection remains usable; a simple health query would pass.
    job.connection.commit()


def another_external_operation(instance, **kwargs):
    instance.se_log += ',second-request'
    instance.save(update_fields=['se_log'])


class LostBeforeNextProcess(Process):
    process_name = 'lost_before_next'
    transitions = [BackgroundTransition(
        'submit', sources=['draft'], target='submitted', in_progress_state='submitting',
        atomic=False, side_effects=[record_receipt_and_end_transaction, another_external_operation],
    )]


class LostBeforeCompletionProcess(Process):
    process_name = 'lost_before_completion'
    transitions = [BackgroundTransition(
        'submit', sources=['draft'], target='submitted', in_progress_state='submitting',
        atomic=False, side_effects=[record_receipt_and_end_transaction],
    )]


@requires_postgres
@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class JobTransactionTests(TransactionTestCase):
    def test_lost_transaction_stops_next_operation_and_finalization(self):
        from django_logic.background.non_atomic import _job_connection

        @contextmanager
        def capture_job_connection():
            global _test_job_alias
            with _job_connection() as alias:
                _test_job_alias = alias
                try:
                    yield alias
                finally:
                    _test_job_alias = None

        for process_class in [LostBeforeNextProcess, LostBeforeCompletionProcess]:
            with self.subTest(process=process_class.__name__):
                cache.clear()
                instance = Widget.objects.create()
                process_class(field_name='status', instance=instance).submit()
                row = TransitionMessage.objects.get(instance_id=str(instance.pk))
                with patch('django_logic.background.non_atomic._job_connection', capture_job_connection), self.assertRaises(JobOwnershipLost):
                    run_background_transition(row.pk)
                instance.refresh_from_db()
                row.refresh_from_db()
                self.assertEqual(instance.se_log, 'confirmed')
                self.assertEqual(instance.status, 'submitting')
                self.assertFalse(row.is_completed)
                self.assertEqual(row.errors_count, 0)
