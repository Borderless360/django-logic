"""Show waiting jobs, or send one job to a worker now.

    python manage.py dl_transitions
    python manage.py dl_transitions --queues client_queue
    python manage.py dl_transitions --send 1234

The list is what an operator needs during an incident: which rows are
still open, and why each one is not moving. ``--send`` clears the retry
wait on one row and wakes the workers, so the next claim takes it
without waiting out ``TRANSITION_MESSAGE_RETRY_MINUTES``. A worker still claims the row —
this command runs no side-effects itself.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from django_logic import conf
from django_logic.background.models import TransitionMessage
from django_logic.background.pull import notify_workers
from django_logic.background.safety_nets import _claimable, _queue_is_served


class Command(BaseCommand):
    help = (
        'List waiting jobs or send one job to a worker now.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--queues', '--queue', dest='queues',
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
            if options['queues']:
                raise CommandError('--send identifies one job and cannot be combined with --queues.')
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
        served = {}

        def queue_is_served(queue_name):
            if queue_name not in served:
                served[queue_name] = _queue_is_served(
                    queue_name, now, report_after)
            return served[queue_name]

        count = 0
        for row in rows.order_by('created'):
            count += 1
            age_minutes = int((now - row.created).total_seconds() // 60)
            why = self._why(
                row, claimable, max_errors, report_after, age_minutes,
                queue_is_served)
            self.stdout.write(
                f'#{row.pk} {row.recorded_model_label}'
                f'#{row.instance_id} {row.process_name}.{row.transition_name} '
                f'queue={row.queue_name} errors={row.errors_count} '
                f'start_attempts={row.start_attempt_count} deferrals={row.deferral_count} '
                f'age={age_minutes}m — {why}'
            )
        self.stdout.write(f'{count} uncompleted transitions.')

    @staticmethod
    def _why(row, claimable, max_errors, report_after, age_minutes,
             queue_is_served):
        """One line saying why this row is where it is."""
        if row.failure_pending:
            return 'business work has failed; maintenance retries recording the required failure outcome'
        if row.eligible_at is not None and row.eligible_at > timezone.now():
            return f'waiting until {row.eligible_at.isoformat()}: {row.deferral_reason}'
        if row.errors_count >= max_errors:
            return (
                f'at TRANSITION_MESSAGE_MAX_ERRORS ({max_errors}) — maintenance completes it '
                f'in its failed_state on the next pass of any running worker'
            )
        if row.pk not in claimable:
            return (
                f'waiting out the retry pause after its last error: '
                f'{row.last_error_message[:100]}'
            )
        if row.errors_count and row.last_error_dt is None:
            return (
                'retry wait cleared; a worker may claim it; '
                f'last recorded error: {row.last_error_message[:100]}'
            )
        if (row.started_at is not None
                and (row.last_error_dt is None or row.last_error_dt < row.started_at)
                and (row.last_deferred_at is None or row.last_deferred_at < row.started_at)):
            return (
                'an attempt has started it with no recorded result — it may still be running, '
                'or its worker stopped and another worker must claim it'
            )
        if age_minutes >= report_after:
            if queue_is_served(row.queue_name):
                return (
                    f'claimable, and waiting its turn — a worker takes rows '
                    f'from {row.queue_name!r}; fairness groups take turns and ready work goes first within each group'
                )
            return (
                f'claimable, with no recent queue progress — does a '
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
        if row.failure_pending:
            raise CommandError('Business work has failed. Maintenance retries its required failure record; --send cannot repeat the work.')
        if row.eligible_at is not None and row.eligible_at > timezone.now():
            raise CommandError('This job is deferred. --send does not override provider or application waiting.')
        if row.errors_count >= conf.max_errors():
            raise CommandError(
                f'TransitionMessage#{pk} has spent all '
                f'{conf.max_errors()} of its attempts, so no claim will take '
                f'it. Maintenance completes the failed job; run '
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
