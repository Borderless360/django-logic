"""Internal follow-up jobs expire without losing public request protection."""
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.db.models import QuerySet
from django.test import TestCase, override_settings
from django.utils import timezone

from django_logic import Process, ProcessManager
from django_logic.background import BackgroundEffect, BackgroundTransition, operation_options
from django_logic.background.exceptions import RequestConflict
from django_logic.background.models import TransitionMessage
from django_logic.background.runner import run_background_transition
from django_logic.background.safety_nets import cleanup_completed_transitions
from tests import dl_settings
from tests.background.models import Widget, bg_ok


class RetentionProcess(Process):
    process_name = 'retention'
    transitions = [
        BackgroundTransition('start', sources=['draft'], target='ready',
                             side_effects=[bg_ok], required_next='finish',
                             background_effects=[BackgroundEffect('notify', bg_ok)]),
        BackgroundTransition('finish', sources=['ready'], target='done', side_effects=[bg_ok]),
    ]


@override_settings(DJANGO_LOGIC=dl_settings(
    BACKGROUND_EXECUTION='pull', TRANSITION_MESSAGE_CLEANUP_DAYS=7,
))
class InternalJobCleanupTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        ProcessManager.bind_model_process(Widget, RetentionProcess, state_field='status')
        self.addCleanup(ProcessManager.unbind_model_process, Widget, RetentionProcess)
        self.widget = Widget.objects.create()
        self.parent_number = 1000

    def old_internal_row(self, kind='effect', *, failed=False, days_ago=30,
                         effect_name='notify', parent_action='start'):
        self.parent_number += 1
        effect = kind == 'effect'
        key = (f'django-logic:effect:{self.parent_number}:{effect_name}' if effect
               else f'django-logic:required-next:{self.parent_number}')
        row = TransitionMessage.objects.create(
            **TransitionMessage.instance_key(self.widget, 'retention'),
            field_name='status', transition_name='notify' if effect else 'finish',
            request_key=key, is_completed=True, ended_in_failure=failed,
            effect_name=effect_name if effect else '',
            parent_transition_name=parent_action if effect else '',
            parent_job_id=self.parent_number if effect else None,
        )
        past = timezone.now() - timedelta(days=days_ago)
        TransitionMessage.objects.filter(pk=row.pk).update(modified=past, completed_at=past)
        return row

    def test_actual_children_expire_and_repeated_parent_request_does_not_recreate_work(self):
        with operation_options(request_key='customer:retention:1') as submitted:
            self.widget.retention.start(value=1)
        parent = TransitionMessage.objects.get(pk=submitted.job_id)
        run_background_transition(parent.pk)
        children = list(TransitionMessage.objects.exclude(pk=parent.pk).order_by('pk'))
        self.assertEqual(len(children), 2)
        self.assertEqual({row.request_key for row in children}, {
            f'django-logic:effect:{parent.pk}:notify',
            f'django-logic:required-next:{parent.pk}',
        })
        for child in children:
            run_background_transition(child.pk)
            child.refresh_from_db()
            self.assertTrue(child.is_completed)
            self.assertFalse(child.ended_in_failure)
            self.assertTrue(child.request_key)
        self.widget.refresh_from_db()
        self.assertEqual((self.widget.status, self.widget.se_log), ('done', 'ok,ok,ok,'))
        TransitionMessage.objects.update(modified=timezone.now() - timedelta(days=30))

        self.assertEqual(cleanup_completed_transitions(), 2)
        with operation_options(request_key='customer:retention:1') as repeated:
            self.widget.retention.start(value=1)
        self.assertEqual(repeated.job_id, parent.pk)
        self.assertFalse(repeated.created)
        run_background_transition(parent.pk)
        self.assertEqual(list(TransitionMessage.objects.values_list('pk', flat=True)), [parent.pk])
        self.widget.refresh_from_db()
        self.assertEqual((self.widget.status, self.widget.se_log), ('done', 'ok,ok,ok,'))
        with operation_options(request_key='customer:retention:1'):
            with self.assertRaises(RequestConflict):
                self.widget.retention.start(value=2)

    def test_unfinished_and_recent_internal_jobs_are_retained(self):
        keep = set()
        for kind in ('effect', 'required'):
            pending = self.old_internal_row(kind)
            TransitionMessage.objects.filter(pk=pending.pk).update(
                is_completed=False, eligible_at=timezone.now() + timedelta(hours=1),
                deferral_count=2, errors_count=1,
            )
            keep.update([pending.pk, self.old_internal_row(kind, days_ago=1).pk])
        self.assertEqual(cleanup_completed_transitions(), 0)
        self.assertEqual(set(TransitionMessage.objects.values_list('pk', flat=True)), keep)

    def test_unknown_internal_namespaces_and_nonempty_fingerprints_are_retained(self):
        for key in ('django-logic:unknown:1', 'customer:django-logic:effect:1:notify'):
            unknown = self.old_internal_row()
            TransitionMessage.objects.filter(pk=unknown.pk).update(request_key=key)
        for kind in ('effect', 'required'):
            row = self.old_internal_row(kind)
            TransitionMessage.objects.filter(pk=row.pk).update(request_fingerprint='a' * 64)
        self.assertEqual(cleanup_completed_transitions(), 0)
        self.assertEqual(TransitionMessage.objects.count(), 4)

    def test_newest_failure_is_retained_for_each_effect_action_and_required_process(self):
        keep = set()
        for kind, effect, action in (
            ('effect', 'notify', 'start'), ('effect', 'notify', 'other'),
            ('effect', 'archive', 'start'), ('required', '', ''),
        ):
            self.old_internal_row(kind, failed=True, effect_name=effect, parent_action=action)
            newest = self.old_internal_row(kind, failed=True, days_ago=20,
                                           effect_name=effect, parent_action=action)
            keep.add(newest.pk)
        self.assertEqual(cleanup_completed_transitions(), 4)
        self.assertEqual(set(TransitionMessage.objects.values_list('pk', flat=True)), keep)

    def test_each_delete_rechecks_internal_key_completion_age_and_failure_retention(self):
        delete = QuerySet.delete
        for kind in ('effect', 'required'):
            for change in (
                {'request_key': 'customer:retained'},
                {'request_fingerprint': 'b' * 64},
                {'is_completed': False},
                {'modified': timezone.now()},
                {'ended_in_failure': True},
            ):
                with self.subTest(kind=kind, change=change):
                    TransitionMessage.objects.all().delete()
                    row = self.old_internal_row(kind)
                    delete_calls = []

                    def change_before_delete(queryset):
                        delete_calls.append(row.pk)
                        TransitionMessage.objects.filter(pk=row.pk).update(**change)
                        return delete(queryset)

                    with patch.object(QuerySet, 'delete', change_before_delete):
                        self.assertEqual(cleanup_completed_transitions(), 0)
                    self.assertEqual(delete_calls, [row.pk])
                    self.assertTrue(TransitionMessage.objects.filter(pk=row.pk).exists())
