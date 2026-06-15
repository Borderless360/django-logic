"""Issue #98 under real concurrency (PostgreSQL + real threads).

The owner discriminator is resolved per call (via the per-thread
``_transition_context`` and per-call kwargs) and written in the SAME atomic
INSERT as ``in_progress_state`` + the ``TransitionMessage`` row. These tests
prove, under genuine thread concurrency against PostgreSQL, that:

* the owner recorded on each row is the one resolved for THAT call — no bleed
  between concurrent threads driving different instances, and
* recording the owner does not weaken the one-in-flight concurrency guard
  (the partial-unique constraint) — concurrent attempts on one instance never
  leave two uncompleted rows, and every row created carries the correct owner.

Run under ``tests.settings_stability`` (Postgres + Redis); skipped on SQLite.
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
        # Two integrations driven at the same instant in separate threads. If
        # owner resolution leaked through any shared/global state, the rows
        # would cross-record; they must not.
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
        # Several threads race to start the SAME background transition on one
        # instance. The one-in-flight guard must hold (never two uncompleted
        # rows), the only tolerated errors are the guard firing or the state
        # having moved, and every row that IS created records the correct owner.
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

        # Invariant: at most one uncompleted row for this instance+process.
        self.assertLessEqual(
            TransitionMessage.objects.filter(
                instance_id=str(gmail.pk), is_completed=False
            ).count(),
            1,
        )
        rows = TransitionMessage.objects.filter(instance_id=str(gmail.pk))
        self.assertGreaterEqual(rows.count(), 1)
        for tm in rows:
            self.assertEqual(tm.owning_process_class, _GMAIL)
