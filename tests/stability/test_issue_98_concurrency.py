"""Owner routing under real concurrency: PostgreSQL and real threads.

The process that declares the transition is resolved per call, from the
per-thread transition context and the call's kwargs, and written in the same
atomic INSERT as the ``in_progress_state`` and the ``TransitionMessage`` row.
These tests prove two things under real thread concurrency:

* each row records the owner resolved for that call, with nothing bleeding
  between threads that drive different instances;
* recording the owner does not weaken the one-uncompleted-row guard (the
  partial unique constraint), so concurrent attempts on one instance never
  leave two uncompleted rows.

Run under ``tests.settings_stability`` (PostgreSQL and Redis); skipped on
SQLite.
"""
from django_logic.background.exceptions import AlreadyInProgress
from django_logic.background.models import TransitionMessage
from django_logic.exceptions import TransitionNotAllowed
from tests.background.models import Conversation
from tests.stability.base import (
    StabilityTestCase,
    requires_postgres,
    run_concurrent,
)


_GMAIL = 'tests.background.models.GmailConversationProcess'
_DUMMY = 'tests.background.models.DummyConversationProcess'


@requires_postgres
class Issue98ConcurrentRoutingTests(StabilityTestCase):
    def test_concurrent_distinct_conversations_route_without_owner_bleed(self):
        # Two integrations run at the same moment in separate threads. If owner
        # resolution went through shared state, each row would record the other
        # thread's owner.
        gmail = Conversation.objects.create(
            status='open', source_integration='gmail'
        )
        dummy = Conversation.objects.create(
            status='open', source_integration='dummy'
        )

        def send(pk):
            conv = Conversation.objects.get(pk=pk)
            return conv.process.send_message_via_integration()

        outcomes = run_concurrent(
            send,
            n_threads=2,
            args_per_thread=[((gmail.pk,), {}), ((dummy.pk,), {})],
        )
        for result, error in outcomes:
            self.assertIsNone(error, f'unexpected error: {error!r}')

        gmail.refresh_from_db()
        dummy.refresh_from_db()
        self.assertIn('gmail_send,', gmail.se_log)
        self.assertNotIn('dummy_send,', gmail.se_log)
        self.assertIn('dummy_send,', dummy.se_log)
        self.assertNotIn('gmail_send,', dummy.se_log)

        gmail_tm = TransitionMessage.objects.get(instance_id=str(gmail.pk))
        dummy_tm = TransitionMessage.objects.get(instance_id=str(dummy.pk))
        self.assertEqual(gmail_tm.owning_process_class, _GMAIL)
        self.assertEqual(dummy_tm.owning_process_class, _DUMMY)

    def test_concurrent_same_instance_guard_holds_and_owner_correct(self):
        # Four threads race to start the same background transition on one
        # instance. At most one uncompleted row may exist, the only allowed
        # errors are the guard firing or the state having moved, and every row
        # created records the correct owner.
        gmail = Conversation.objects.create(
            status='open', source_integration='gmail'
        )

        def send():
            conv = Conversation.objects.get(pk=gmail.pk)
            return conv.process.send_message_via_integration()

        outcomes = run_concurrent(send, n_threads=4)

        for result, error in outcomes:
            if error is not None:
                self.assertIsInstance(
                    error,
                    (AlreadyInProgress, TransitionNotAllowed),
                    f'unexpected error type: {error!r}',
                )

        # Never two uncompleted rows for this instance and process.
        self.assertLessEqual(
            TransitionMessage.objects.filter(
                instance_id=str(gmail.pk), is_completed=False
            ).count(),
            1,
        )
        rows = TransitionMessage.objects.filter(instance_id=str(gmail.pk))
        self.assertGreaterEqual(rows.count(), 1)
        for row in rows:
            self.assertEqual(row.owning_process_class, _GMAIL)
