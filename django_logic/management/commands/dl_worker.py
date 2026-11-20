"""Run a pull worker: claim rows from the database and execute them.

    python manage.py dl_worker --queues django_logic.critical,django_logic.fast

One process per SLA group. ``--concurrency`` says how many attempts the
process runs at a time. The loop also runs maintenance (stuck
report, cleanup), so pull mode needs no beat schedule. On SIGTERM the
worker takes no new job, waits for its running jobs and exits. See
docs/workers.md.
"""
import signal
import sys

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from django_logic import conf
from django_logic.background.pull import run_worker


def validate_worker_database():
    if connection.vendor != 'postgresql':
        raise CommandError('dl_worker requires PostgreSQL.')
    driver = connection.Database
    if driver.__name__ == 'psycopg' and tuple(map(int, driver.__version__.split('.')[:2])) < (3, 2):
        raise CommandError('dl_worker requires psycopg 3.2 or later, or psycopg2. Install django-logic[postgres].')


class Command(BaseCommand):
    help = 'Run a pull worker for the given queues (BACKGROUND_EXECUTION=pull).'

    def execute(self, *args, **options):
        if sys.platform != 'linux':
            raise CommandError(
                'dl_worker requires Linux. On macOS or Windows, run the worker '
                'in a Linux VM or container.'
            )
        return super().execute(*args, **options)

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
        validate_worker_database()
        queues = [q for q in options['queues'].split(',') if q]
        if not queues:
            raise CommandError('--queues must name at least one queue.')
        concurrency = options['concurrency']
        if concurrency < 1:
            raise CommandError('--concurrency must be 1 or more.')
        stop = []
        signal.signal(signal.SIGTERM, lambda *_: stop.append(True))
        run_worker(queues, forever=True, concurrency=concurrency, stop_requested=lambda: bool(stop))
