"""dl_transitions: what an operator can ask during an incident.

The list must say why each uncompleted row is not moving, and ``--send``
must clear the retry wait on one row without running anything itself.
"""
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from django_logic.background.models import TransitionMessage
from django_logic.testing import open_transition_message
from tests.background.models import Widget
from tests import dl_settings


_SETTINGS = dl_settings(
    BACKGROUND_EXECUTION='sync',
    TRANSITION_MESSAGE_MAX_ERRORS=3,
    TRANSITION_MESSAGE_RETRY_MINUTES=2,
)


@override_settings(DJANGO_LOGIC=_SETTINGS)
class ListUncompletedTests(TestCase):
    def _row(self, queue='django_logic.critical', **updates):
        widget = Widget.objects.create(status='fulfilling')
        row = open_transition_message(
            widget, 'process', 'fulfil', queue_name=queue)
        if updates:
            TransitionMessage.objects.filter(pk=row.pk).update(**updates)
            row.refresh_from_db()
        return row

    def _listing(self, **options):
        out = StringIO()
        call_command('dl_transitions', stdout=out, **options)
        return out.getvalue()

    def test_a_completed_row_is_not_listed(self):
        self._row(is_completed=True)
        listing = self._listing()
        self.assertIn('0 uncompleted transitions.', listing)

    def test_a_fresh_row_reads_as_claimable(self):
        created = self._row()
        listing = self._listing()
        self.assertIn(f'#{created.pk}', listing)
        self.assertIn('claimable now', listing)
        self.assertIn('1 uncompleted transitions.', listing)

    def test_a_row_at_max_errors_names_the_finalizer(self):
        self._row(errors_count=3, last_error_dt=timezone.now())
        listing = self._listing()
        self.assertIn('at MAX_ERRORS (3)', listing)
        self.assertIn("once a worker serves 'django_logic.critical'", listing)

    def test_a_row_inside_its_retry_pause_says_so(self):
        self._row(errors_count=1, last_error_dt=timezone.now(),
                  last_error_message='the courier said no')
        listing = self._listing()
        self.assertIn('waiting out the retry pause', listing)
        self.assertIn('the courier said no', listing)

    def test_a_row_no_worker_ever_started_names_the_queue(self):
        self._row(
            queue='nobody_serves_this',
            created=timezone.now() - timedelta(hours=4),
        )
        listing = self._listing()
        self.assertIn("does a worker serve 'nobody_serves_this'", listing)
        self.assertIn('dl_worker --queues nobody_serves_this', listing)

    def test_a_started_row_is_not_reported_as_untouched(self):
        self._row(started_at=timezone.now())
        self.assertIn('an attempt has started it', self._listing())

    def test_the_queue_filter_holds(self):
        self._row(queue='django_logic.slow')
        self.assertIn(
            '0 uncompleted transitions.',
            self._listing(queues='django_logic.critical'),
        )
        self.assertIn(
            '1 uncompleted transitions.',
            self._listing(queues='django_logic.slow'),
        )


@override_settings(DJANGO_LOGIC=_SETTINGS)
class SendOneRowTests(TestCase):
    def _row(self, **updates):
        widget = Widget.objects.create(status='fulfilling')
        row = open_transition_message(widget, 'process', 'fulfil')
        if updates:
            TransitionMessage.objects.filter(pk=row.pk).update(**updates)
            row.refresh_from_db()
        return row

    def test_send_clears_the_retry_wait(self):
        row = self._row(errors_count=1, last_error_dt=timezone.now())
        out = StringIO()
        call_command('dl_transitions', send=row.pk, stdout=out)
        row.refresh_from_db()
        self.assertIsNone(row.last_error_dt)
        # The attempt count is untouched: MAX_ERRORS still bounds it.
        self.assertEqual(row.errors_count, 1)
        # The claim is promised only once a worker serves the queue.
        self.assertIn("once one serves 'django_logic'", out.getvalue())

    def test_send_refuses_a_completed_row(self):
        row = self._row(is_completed=True)
        with self.assertRaises(CommandError) as ctx:
            call_command('dl_transitions', send=row.pk, stdout=StringIO())
        self.assertIn('not an uncompleted row', str(ctx.exception))

    def test_send_refuses_a_row_no_claim_would_take(self):
        row = self._row(errors_count=3, last_error_dt=timezone.now())
        with self.assertRaises(CommandError) as ctx:
            call_command('dl_transitions', send=row.pk, stdout=StringIO())
        self.assertIn('spent all 3 of its attempts', str(ctx.exception))
        row.refresh_from_db()
        self.assertIsNotNone(row.last_error_dt)
