"""Missing work completes under its job lock, and failed writes remain visible."""
from concurrent.futures import ThreadPoolExecutor

from django.db import DatabaseError, OperationalError, connection, connections, transaction
from django.test import TransactionTestCase, override_settings

from django_logic.background.models import TransitionMessage
from django_logic.background.runner import run_background_transition
from tests import dl_settings
from tests.stability.base import requires_postgres


@requires_postgres
@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class RestoreCompletionTests(TransactionTestCase):
    def setUp(self):
        self.message = TransitionMessage.objects.create(
            app_label='removed_app', model_name='removed_model', instance_id='1',
            process_name='process', transition_name='finish', queue_name='restore-test',
        )

    @staticmethod
    def is_completion(sql):
        return sql.startswith('UPDATE ') and '"is_completed" =' in sql.split(' WHERE ', 1)[0]

    def test_another_worker_cannot_take_the_job_before_completion_is_written(self):
        checked = []

        def compete():
            try:
                with transaction.atomic():
                    TransitionMessage.objects.select_for_update(nowait=True).get(pk=self.message.pk)
                return False
            except OperationalError as error:
                cause = error.__cause__
                self.assertEqual(
                    getattr(cause, 'sqlstate', None) or getattr(cause, 'pgcode', None), '55P03')
                return True
            finally:
                connections.close_all()

        def inspect_completion(execute, sql, params, many, context):
            if self.is_completion(sql):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    checked.append(executor.submit(compete).result(timeout=5))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(inspect_completion):
            run_background_transition(self.message.pk)

        self.assertEqual(checked, [True])
        self.message.refresh_from_db()
        self.assertTrue(self.message.is_completed)
        self.assertTrue(self.message.ended_in_failure)
        self.assertEqual(self.message.completion_kind, 'failed')
        self.assertEqual(self.message.errors_count, 0)
        self.assertIsNone(self.message.duration_ms)
        self.assertTrue(self.message.last_error_message.startswith('[unrestorable]'))

    def test_a_rejected_completion_write_raises_and_the_next_attempt_can_complete(self):
        def reject_completion(execute, sql, params, many, context):
            if self.is_completion(sql):
                with connection.cursor() as cursor:
                    cursor.execute("SELECT CAST('not-an-integer' AS integer)")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(reject_completion):
            with self.assertRaises(DatabaseError):
                run_background_transition(self.message.pk)

        self.message.refresh_from_db()
        self.assertFalse(self.message.is_completed)
        self.assertEqual(self.message.last_error_message, '')

        run_background_transition(self.message.pk)
        self.message.refresh_from_db()
        self.assertTrue(self.message.is_completed)
        self.assertEqual(self.message.start_attempt_count, 2)
