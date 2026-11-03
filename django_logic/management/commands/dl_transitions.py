"""Show waiting jobs and recorded timings, or send one job to a worker now.

    python manage.py dl_transitions
    python manage.py dl_transitions --queues client_queue
    python manage.py dl_transitions --send 1234
    python manage.py dl_transitions --stats --since 7d --json

The list is what an operator needs during an incident: which rows are
still open, and why each one is not moving. ``--send`` clears the retry
wait on one row and wakes the workers, so the next claim takes it
without waiting out ``RETRY_MINUTES``. A worker still claims the row —
this command runs no side-effects itself.
"""
from datetime import timedelta
import json
import re

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from django_logic import conf
from django_logic.background.diagnostics import transition_statistics
from django_logic.background.models import TransitionMessage
from django_logic.background.pull import notify_workers
from django_logic.background.safety_nets import _claimable, _queue_is_served
from django_logic.background.worker_presence import queue_worker_presence


class Command(BaseCommand):
    help = (
        'List waiting jobs, report recorded timings, or send one job to a worker now.'
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
        parser.add_argument('--stats', action='store_true', help='report retained job counts and measured timings')
        parser.add_argument('--since', help='age of the oldest submitted jobs to include, such as 24h or 7d (default: 7d)')
        parser.add_argument('--json', action='store_true', help='write --stats as JSON')

    def handle(self, *args, **options):
        if options['stats'] and options['send'] is not None:
            raise CommandError('--stats cannot be combined with --send.')
        if not options['stats'] and (options['since'] is not None or options['json']):
            raise CommandError('--since and --json require --stats.')
        if options['send'] is not None:
            self._send(options['send'])
            return
        queues = None
        if options['queues']:
            queues = [q for q in options['queues'].split(',') if q]
        if options['stats']:
            self._statistics(queues, options['since'] or '7d', options['json'])
            return
        self._list(queues)

    def _statistics(self, queues, since, as_json):
        match = re.fullmatch(r'([1-9][0-9]*)([mhdw])', since)
        if match is None:
            raise CommandError('--since needs a positive duration, such as 30m, 24h, 7d or 1w.')
        minutes = int(match[1]) * {'m': 1, 'h': 60, 'd': 1440, 'w': 10080}[match[2]]
        try:
            cutoff = timezone.now() - timedelta(minutes=minutes)
        except OverflowError as error:
            raise CommandError('--since is too large.') from error
        groups = transition_statistics(since=cutoff, queues=queues)
        presence = {group['queue_name']: None for group in groups}
        for queue_name in presence:
            presence[queue_name] = queue_worker_presence(queue_name)
        notes = [
            'Counts cover the recorded history of retained jobs created since the cutoff, not events within the window.',
            'Initial wait uses first_started_at. Missing measurements are excluded; percentile sample counts are included.',
            'Repeated starts include deferrals and competing starts, so they are not an exact retry count.',
            'Duration covers only the last measured attempt. Total runtime and worker utilization are not available.',
            'Worker presence is a recent maintenance observation, not proof of queue progress.',
        ]
        if as_json:
            self.stdout.write(json.dumps({'since': cutoff.isoformat(), 'groups': groups,
                                          'worker_presence': presence, 'notes': notes}))
            return
        self.stdout.write(f'Retained jobs created since {cutoff.isoformat()}')
        for note in notes:
            self.stdout.write(note)
        for group in groups:
            self.stdout.write(
                f"queue={group['queue_name']} "
                f"{group['app_label']}.{group['model_name']}.{group['process_name']}.{group['transition_name']} "
                f"effect={group['effect_name'] or '-'} jobs={group['jobs']} pending={group['pending']} "
                f"succeeded={group['succeeded']} failed={group['failed']} "
                f"other_completed={group['other_completed']} errors={group['errors']} "
                f"recorded_starts={group['recorded_starts']} repeated_starts={group['repeated_starts']} "
                f"deferrals={group['deferrals']} worker_presence={presence[group['queue_name']]} "
                f"initial_wait_ms={json.dumps(group['initial_wait_ms'], sort_keys=True)} "
                f"last_attempt_duration_ms={json.dumps(group['last_attempt_duration_ms'], sort_keys=True)}"
            )
        self.stdout.write(f'{len(groups)} transition groups.')

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
        presence = {}

        def queue_is_served(queue_name):
            if queue_name not in served:
                served[queue_name] = _queue_is_served(
                    queue_name, now, report_after)
            return served[queue_name]

        def queue_has_worker(queue_name):
            if queue_name not in presence:
                presence[queue_name] = queue_worker_presence(queue_name)
            return presence[queue_name]

        count = 0
        for row in rows.order_by('created'):
            count += 1
            age_minutes = int((now - row.created).total_seconds() // 60)
            why = self._why(
                row, claimable, max_errors, report_after, age_minutes,
                queue_is_served, queue_has_worker)
            parent = f'parent_job={row.parent_job_id} ' if row.effect_name else ''
            self.stdout.write(
                f'#{row.pk} {row.recorded_model_label}'
                f'#{row.instance_id} {row.process_name}.{row.operation_name} '
                f'queue={row.queue_name} errors={row.errors_count} '
                f'{parent}'
                f'start_attempts={row.start_attempt_count} deferrals={row.deferral_count} '
                f'age={age_minutes}m — {why}'
            )
        self.stdout.write(f'{count} uncompleted transitions.')

    @staticmethod
    def _why(row, claimable, max_errors, report_after, age_minutes,
             queue_is_served, queue_has_worker):
        """One line saying why this row is where it is."""
        if row.eligible_at is not None and row.eligible_at > timezone.now():
            return f'waiting until {row.eligible_at.isoformat()}: {row.deferral_reason}'
        if row.errors_count >= max_errors:
            if row.effect_name:
                return (f'at MAX_ERRORS ({max_errors}) — the next worker check records '
                        'the effect failure without changing the parent state')
            return (
                f'at MAX_ERRORS ({max_errors}) — the stuck finalizer ends it '
                f'in its failed_state on the next pass of any running worker'
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
            if queue_is_served(row.queue_name):
                return (
                    f'claimable, and waiting its turn — a worker takes rows '
                    f'from {row.queue_name!r}, and older rows go first'
                )
            if queue_has_worker(row.queue_name):
                return (
                    'claimable, with recent worker presence but no queue progress '
                    f'for {report_after} minutes; check shared capacity and blocked jobs'
                )
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
        if row.eligible_at is not None and row.eligible_at > timezone.now():
            raise CommandError('This job is deferred. --send does not override provider or application waiting.')
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
