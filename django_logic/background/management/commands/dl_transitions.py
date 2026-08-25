"""Show the uncompleted background transitions, or send one to a worker now.

    python manage.py dl_transitions
    python manage.py dl_transitions --queues client_queue
    python manage.py dl_transitions --send 1234

The list is what an operator needs during an incident: which rows are
still open, and why each one is not moving. ``--send`` clears the retry
wait on one row and wakes the workers, so the next claim takes it
without waiting out ``RETRY_MINUTES``. A worker still claims the row —
this command runs no side-effects itself.
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from django_logic import conf
from django_logic.background.models import TransitionMessage
from django_logic.background.pull import notify_workers
from django_logic.background.safety_nets import _claimable


class Command(BaseCommand):
    help = (
        'List the uncompleted background transitions, or send one to a '
        'worker now.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--queues',
            help='comma-separated queue names to list (default: every queue)',
        )
        parser.add_argument(
            '--send', type=int, metavar='PK',
            help=(
                'clear the retry wait on this TransitionMessage and wake the '
                'workers, so the next claim takes it'
            ),
        )

    def handle(self, *args, **options):
        if options['send'] is not None:
            self._send(options['send'])
            return
        queues = None
        if options['queues']:
            queues = [q for q in options['queues'].split(',') if q]
        self._list(queues)

    def _list(self, queues):
        rows = TransitionMessage.objects.filter(is_completed=False)
        if queues is not None:
            rows = rows.filter(queue_name__in=queues)
        claimable = set(
            _claimable(queues).values_list('pk', flat=True)
        )
        now = timezone.now()
        max_errors = conf.max_errors()
        report_after = conf.retry_window_minutes()
        count = 0
        for row in rows.order_by('created'):
            count += 1
            age_minutes = int((now - row.created).total_seconds() // 60)
            why = self._why(
                row, claimable, max_errors, report_after, age_minutes)
            self.stdout.write(
                f'#{row.pk} {row.app_label}.{row.model_name}'
                f'#{row.instance_id} {row.process_name}.{row.transition_name} '
                f'queue={row.queue_name} errors={row.errors_count} '
                f'age={age_minutes}m — {why}'
            )
        self.stdout.write(f'{count} uncompleted transitions.')

    @staticmethod
    def _why(row, claimable, max_errors, report_after, age_minutes):
        """One line saying why this row is where it is."""
        if row.errors_count >= max_errors:
            return (
                f'at MAX_ERRORS ({max_errors}) — the stuck finalizer ends it '
                f'in its failed_state on its next pass, once a worker serves '
                f'{row.queue_name!r}'
            )
        if row.pk not in claimable:
            return (
                f'waiting out the retry pause after its last error: '
                f'{row.last_error_message[:100]}'
            )
        if row.started_at is not None:
            return (
                'an attempt has started it — it is running on a worker now, '
                'or its worker died and the next claim takes it'
            )
        if age_minutes >= report_after:
            return (
                f'claimable, and no worker has ever started it — does a '
                f'worker serve {row.queue_name!r}? Start one: '
                f'dl_worker --queues {row.queue_name}'
            )
        return 'claimable now — a worker takes it on its next claim'

    def _send(self, pk):
        row = TransitionMessage.objects.filter(
            pk=pk, is_completed=False).first()
        if row is None:
            raise CommandError(
                f'TransitionMessage#{pk} is not an uncompleted row. '
                f'A completed row is finished; nothing re-sends it.'
            )
        if row.errors_count >= conf.max_errors():
            raise CommandError(
                f'TransitionMessage#{pk} has spent all '
                f'{conf.max_errors()} of its attempts, so no claim will take '
                f'it. The stuck finalizer ends it in its failed_state; run '
                f'the transition again from the instance after that.'
            )
        TransitionMessage.objects.filter(pk=pk, is_completed=False).update(
            last_error_dt=None, modified=timezone.now())
        notify_workers()
        self.stdout.write(
            f'TransitionMessage#{pk}: the retry wait is cleared and the '
            f'workers are notified. A worker claims it next, once one '
            f'serves {row.queue_name!r}.'
        )
