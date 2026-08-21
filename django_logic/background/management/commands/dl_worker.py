"""Run a pull worker: claim rows from the database and execute them.

    python manage.py dl_worker --queues django_logic.critical,django_logic.fast

One process per SLA group. The
loop also runs the safety nets (watchdog, stuck report, cleanup), so
pull mode needs no beat schedule. See docs/design/PULL_WORKERS.md.
"""
from django.core.management.base import BaseCommand, CommandError

from django_logic.background import settings as bg_settings
from django_logic.background.pull import run_worker


class Command(BaseCommand):
    help = 'Run a pull worker for the given queues (BACKGROUND_EXECUTION=pull).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--queues', required=True,
            help='comma-separated queue names this worker serves',
        )
        parser.add_argument(
            '--once', action='store_true',
            help='drain what is claimable now, run the safety nets, exit',
        )

    def handle(self, *args, **options):
        if bg_settings.background_execution() != bg_settings.EXECUTION_PULL:
            raise CommandError(
                "dl_worker needs DJANGO_LOGIC['BACKGROUND_EXECUTION']='pull'."
            )
        queues = [q for q in options['queues'].split(',') if q]
        if not queues:
            raise CommandError('--queues must name at least one queue.')
        run_worker(queues, forever=not options['once'])
