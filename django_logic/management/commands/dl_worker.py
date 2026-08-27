"""Run a pull worker: claim rows from the database and execute them.

    python manage.py dl_worker --queues django_logic.critical,django_logic.fast

One process per SLA group. ``--concurrency`` says how many attempts the
process runs at a time. The loop also runs the safety nets (stuck
report, cleanup), so pull mode needs no beat schedule. See
docs/design/PULL_WORKERS.md.
"""
from django.core.management.base import BaseCommand, CommandError

from django_logic import conf
from django_logic.background.pull import run_worker


class Command(BaseCommand):
    help = 'Run a pull worker for the given queues (BACKGROUND_EXECUTION=pull).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--queues', required=True,
            help='comma-separated queue names this worker serves',
        )
        parser.add_argument(
            '--concurrency', type=int, default=1,
            help=(
                'how many attempts this worker runs at a time (default 1). '
                'Each one holds a database connection while it runs.'
            ),
        )

    def handle(self, *args, **options):
        if conf.background_execution() != conf.EXECUTION_PULL:
            raise CommandError(
                "dl_worker needs DJANGO_LOGIC['BACKGROUND_EXECUTION']='pull'."
            )
        queues = [q for q in options['queues'].split(',') if q]
        if not queues:
            raise CommandError('--queues must name at least one queue.')
        concurrency = options['concurrency']
        if concurrency < 1:
            raise CommandError('--concurrency must be 1 or more.')
        run_worker(queues, forever=True, concurrency=concurrency)
