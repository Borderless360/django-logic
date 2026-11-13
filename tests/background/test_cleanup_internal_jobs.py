"""Completed jobs expire while the latest business failure stays visible."""
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.db.models import QuerySet
from django.test import TestCase, override_settings
from django.utils import timezone

from django_logic import Process, ProcessManager
from django_logic.background import BackgroundTransition
from django_logic.background.exceptions import PermanentFailure
from django_logic.background.models import TransitionMessage
from django_logic.background.runner import run_background_transition
from django_logic.background.safety_nets import cleanup_completed_transitions
from tests import dl_settings, unbind_model_process
from tests.background.models import Widget, bg_ok


def record_notification(instance, **kwargs):
    if kwargs.get('notification_failure'):
        raise PermanentFailure('The notification could not be delivered.')
    bg_ok(instance, **kwargs)


class RetentionProcess(Process):
    process_name = 'retention'
    transitions = [
        BackgroundTransition('start', sources=['draft'], target='ready', side_effects=[bg_ok]),
        BackgroundTransition('notify', sources=['ready'], side_effects=[record_notification]),
    ]


@override_settings(DJANGO_LOGIC=dl_settings(
    BACKGROUND_EXECUTION='pull', TRANSITION_MESSAGE_CLEANUP_DAYS=7,
))
class JobCleanupTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        ProcessManager.bind_model_process(Widget, RetentionProcess, state_field='status')
        self.addCleanup(unbind_model_process, Widget, RetentionProcess)
        self.widget = Widget.objects.create()

    def old_row(self, *, failed=False, days_ago=30, process_name='retention'):
        row = TransitionMessage.objects.create(
            **TransitionMessage.instance_key(self.widget, process_name),
            field_name='status', transition_name='start',
            owning_process_class=f'{RetentionProcess.__module__}.{RetentionProcess.__name__}',
            kwargs={'process_class': f'{RetentionProcess.__module__}.{RetentionProcess.__name__}'},
            is_completed=True, ended_in_failure=failed,
        )
        past = timezone.now() - timedelta(days=days_ago)
        TransitionMessage.objects.filter(pk=row.pk).update(modified=past, completed_at=past)
        return row

    def start_and_notify(self, **kwargs):
        self.widget.retention.start()
        first = TransitionMessage.objects.get()
        run_background_transition(first.pk)
        self.widget.refresh_from_db()
        self.widget.retention.notify(**kwargs)
        second = TransitionMessage.objects.get(is_completed=False)
        run_background_transition(second.pk)
        return first, second

    def test_completed_jobs_expire_without_repeating_business_work(self):
        first, second = self.start_and_notify()
        run_background_transition(first.pk)
        run_background_transition(second.pk)
        self.widget.refresh_from_db()
        self.assertEqual((self.widget.status, self.widget.se_log), ('ready', 'ok,ok,'))
        TransitionMessage.objects.update(modified=timezone.now() - timedelta(days=30))
        self.assertEqual(cleanup_completed_transitions(), 2)
        self.assertFalse(TransitionMessage.objects.exists())
        self.widget.refresh_from_db()
        self.assertEqual((self.widget.status, self.widget.se_log), ('ready', 'ok,ok,'))

    def test_latest_failed_notification_stays_visible_after_successful_work_expires(self):
        first, second = self.start_and_notify(notification_failure=True)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(first.is_completed)
        self.assertFalse(first.ended_in_failure)
        self.assertTrue(second.is_completed)
        self.assertTrue(second.ended_in_failure)
        self.assertEqual(second.errors_count, 1)
        TransitionMessage.objects.update(modified=timezone.now() - timedelta(days=30))
        self.assertEqual(cleanup_completed_transitions(), 1)
        self.assertEqual(list(TransitionMessage.objects.values_list('pk', flat=True)), [second.pk])
        self.widget.refresh_from_db()
        self.assertEqual((self.widget.status, self.widget.se_log), ('ready', 'ok,'))

    def test_unfinished_and_recent_jobs_are_retained(self):
        pending = self.old_row()
        TransitionMessage.objects.filter(pk=pending.pk).update(
            is_completed=False, eligible_at=timezone.now() + timedelta(hours=1),
            deferral_count=2, errors_count=1,
        )
        keep = {pending.pk, self.old_row(days_ago=1).pk, self.old_row(failed=True, days_ago=1).pk}
        self.assertEqual(cleanup_completed_transitions(), 0)
        self.assertEqual(set(TransitionMessage.objects.values_list('pk', flat=True)), keep)

    def test_latest_failure_is_retained_for_each_business_process(self):
        keep = set()
        for process_name in ('approval', 'notification', 'archive'):
            self.old_row(failed=True, process_name=process_name)
            keep.add(self.old_row(failed=True, days_ago=20, process_name=process_name).pk)
        self.assertEqual(cleanup_completed_transitions(), 3)
        self.assertEqual(set(TransitionMessage.objects.values_list('pk', flat=True)), keep)

    def test_each_delete_rechecks_completion_age_and_failure_retention(self):
        delete = QuerySet.delete
        for change in ({'is_completed': False}, {'modified': timezone.now()}, {'ended_in_failure': True}):
            with self.subTest(change=change):
                TransitionMessage.objects.all().delete()
                row = self.old_row()
                delete_calls = []

                def change_before_delete(queryset):
                    delete_calls.append(row.pk)
                    TransitionMessage.objects.filter(pk=row.pk).update(**change)
                    return delete(queryset)

                with patch.object(QuerySet, 'delete', change_before_delete):
                    self.assertEqual(cleanup_completed_transitions(), 0)
                self.assertEqual(delete_calls, [row.pk])
                self.assertTrue(TransitionMessage.objects.filter(pk=row.pk).exists())
