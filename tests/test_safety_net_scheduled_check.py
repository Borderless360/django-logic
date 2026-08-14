"""The check that warns when celery mode has an unscheduled safety net.

The five periodic tasks are the durability half of
``BACKGROUND_EXECUTION='celery'``. A consumer ran seven weeks with none of them
scheduled, because Celery ignores ``app.conf.beat_schedule = {...}`` when the
project also defines the ``CELERY_``-namespaced setting.
"""
from celery import current_app
from django.test import SimpleTestCase, modify_settings, override_settings

from django_logic.background.settings import beat_schedule
from django_logic.checks import check_safety_net_is_scheduled
from tests import dl_settings


class SafetyNetScheduledCheckTests(SimpleTestCase):
    def _run(self):
        return check_safety_net_is_scheduled(app_configs=None)

    @override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='sync'))
    def test_sync_mode_is_not_checked(self):
        # Sync mode produces no durable rows, so there is nothing to recover.
        self.assertEqual(self._run(), [])

    @modify_settings(INSTALLED_APPS={'remove': 'django_logic.background'})
    @override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='celery'))
    def test_not_checked_without_the_background_app(self):
        # BACKGROUND_EXECUTION defaults to 'celery' and the core app registers
        # the checks, so an install that never added the background app would
        # otherwise be warned about rows it cannot have.
        with self._beat({}):
            self.assertEqual(self._run(), [])

    @override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='celery'))
    def test_celery_mode_with_nothing_scheduled_warns(self):
        with self._beat({}):
            findings = self._run()

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].id, 'django_logic.W002')
        for task in (entry['task'] for entry in beat_schedule().values()):
            self.assertIn(task, findings[0].msg)
        self.assertIn('CELERY_BEAT_SCHEDULE', findings[0].hint)

    @override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='celery'))
    def test_celery_mode_with_everything_scheduled_is_clean(self):
        with self._beat(beat_schedule()):
            self.assertEqual(self._run(), [])

    @override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='celery'))
    def test_entries_are_matched_by_task_name_not_entry_key(self):
        # A consumer may name the entries whatever it likes.
        renamed = {
            'whatever-%d' % i: entry
            for i, entry in enumerate(beat_schedule().values())
        }
        with self._beat(renamed):
            self.assertEqual(self._run(), [])

    @override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='celery'))
    def test_a_partial_schedule_names_only_what_is_missing(self):
        shipped = beat_schedule()
        dropped = 'django-logic-watchdog'
        partial = {k: v for k, v in shipped.items() if k != dropped}

        with self._beat(partial):
            findings = self._run()

        self.assertEqual(len(findings), 1)
        self.assertIn(shipped[dropped]['task'], findings[0].msg)
        for key, entry in partial.items():
            self.assertNotIn(entry['task'], findings[0].msg)

    def _beat(self, schedule):
        class _Ctx:
            def __enter__(inner):
                inner.previous = current_app.conf.beat_schedule
                current_app.conf.beat_schedule = schedule
                return inner

            def __exit__(inner, *exc):
                current_app.conf.beat_schedule = inner.previous

        return _Ctx()
