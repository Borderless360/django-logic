"""Operator reports distinguish initial waiting from later attempts."""
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from django_logic.background import transition_statistics
from django_logic.background.models import TransitionMessage
from django_logic.testing import open_transition_message
from tests.background.models import Widget


_NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


class TransitionStatisticsTests(TestCase):
    def _row(self, **updates):
        widget = Widget.objects.create(status='fulfilling')
        message = open_transition_message(widget, 'process', 'fulfil', queue_name='diagnostics')
        TransitionMessage.objects.filter(pk=message.pk).update(created=_NOW - timedelta(days=1), **updates)
        return message

    def test_initial_wait_does_not_include_later_retries_or_deferrals(self):
        for seconds in range(1, 11):
            self._row(
                first_started_at=_NOW - timedelta(days=1) + timedelta(seconds=seconds),
                started_at=_NOW, duration_ms=seconds * 10,
                is_completed=True, completion_kind='succeeded',
                start_attempt_count=4 if seconds == 10 else 1,
                errors_count=1 if seconds == 10 else 0,
                deferral_count=2 if seconds == 10 else 0,
            )
        before = list(TransitionMessage.objects.order_by('pk').values())

        groups = transition_statistics(since=_NOW - timedelta(days=2))

        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group['initial_wait_ms'], {'samples': 10, 'p50': 5000, 'p90': 9000, 'max': 10000})
        self.assertEqual(group['last_attempt_duration_ms'], {'samples': 10, 'p50': 50, 'p90': 90, 'max': 100})
        self.assertEqual({key: group[key] for key in ('jobs', 'succeeded', 'pending', 'failed')},
                         {'jobs': 10, 'succeeded': 10, 'pending': 0, 'failed': 0})
        self.assertEqual((group['errors'], group['deferrals'], group['recorded_starts'], group['repeated_starts']),
                         (1, 2, 13, 3))
        self.assertEqual(list(TransitionMessage.objects.order_by('pk').values()), before)

    def test_missing_history_is_not_replaced_with_the_latest_start_or_zero(self):
        self._row(started_at=_NOW, is_completed=True)
        group = transition_statistics(since=_NOW - timedelta(days=2))[0]
        self.assertEqual(group['initial_wait_ms'], {'samples': 0, 'p50': None, 'p90': None, 'max': None})
        self.assertEqual(group['last_attempt_duration_ms']['samples'], 0)
        self.assertEqual(group['other_completed'], 1)
        self.assertEqual(group['succeeded'], 0)

    def test_window_queue_and_operation_filters_do_not_mix_distinct_work(self):
        self._row()
        self._row(is_completed=True, ended_in_failure=True, errors_count=2)
        effect = self._row(effect_name='tags', parent_transition_name='fulfil')
        self._row(queue_name='elsewhere')
        old = self._row()
        TransitionMessage.objects.filter(pk=old.pk).update(created=_NOW - timedelta(days=3))
        groups = transition_statistics(since=_NOW - timedelta(days=2), queues=['diagnostics'])
        self.assertEqual(len(groups), 2)
        regular, effect_group = groups
        self.assertEqual((regular['jobs'], regular['pending'], regular['failed'], regular['errors']), (2, 1, 1, 2))
        self.assertEqual((effect_group['effect_name'], effect_group['jobs']), ('tags', 1))
        self.assertTrue(TransitionMessage.objects.filter(pk=effect.pk, is_completed=False).exists())
        self.assertEqual(transition_statistics(since=_NOW - timedelta(days=2), queues=[]), [])

    def test_command_reports_machine_readable_counts_without_running_or_changing_jobs(self):
        message = self._row(errors_count=1, last_error_dt=_NOW)
        before = list(TransitionMessage.objects.values())
        output = StringIO()
        with patch('django_logic.management.commands.dl_transitions.timezone.now', return_value=_NOW):
            call_command('dl_transitions', stats=True, since='7d', json=True, queues='diagnostics', stdout=output)
        result = json.loads(output.getvalue())
        self.assertEqual(result['groups'][0]['pending'], 1)
        self.assertIn('diagnostics', result['worker_presence'])
        self.assertTrue(any('not an exact retry count' in note for note in result['notes']))
        self.assertEqual(list(TransitionMessage.objects.values()), before)
        with self.assertRaisesMessage(CommandError, '--stats cannot be combined with --send'):
            call_command('dl_transitions', stats=True, send=message.pk)
        self.assertEqual(list(TransitionMessage.objects.values()), before)

    def test_same_named_effects_from_different_parent_actions_stay_separate(self):
        for parent, duration in (('submit', 10), ('cancel', 90)):
            self._row(
                transition_name='django-logic:effect:' + 'f' * 64,
                parent_transition_name=parent, effect_name='notify',
                is_completed=True, completion_kind='succeeded', duration_ms=duration,
            )
        before = list(TransitionMessage.objects.order_by('pk').values())
        groups = transition_statistics(since=_NOW - timedelta(days=2))
        self.assertEqual(
            [(group['parent_transition_name'], group['effect_name'], group['jobs'],
              group['last_attempt_duration_ms']['p50']) for group in groups],
            [('cancel', 'notify', 1, 90), ('submit', 'notify', 1, 10)],
        )
        for as_json in (False, True):
            output = StringIO()
            with patch('django_logic.management.commands.dl_transitions.timezone.now', return_value=_NOW):
                call_command('dl_transitions', stats=True, since='7d', json=as_json, stdout=output)
            if as_json:
                self.assertEqual(json.loads(output.getvalue())['groups'], groups)
            else:
                self.assertIn('.process.submit/notify effect=notify jobs=1 ', output.getvalue())
                self.assertIn('.process.cancel/notify effect=notify jobs=1 ', output.getvalue())
                self.assertNotIn('django-logic:effect:', output.getvalue())
        self.assertEqual(list(TransitionMessage.objects.order_by('pk').values()), before)

    def test_command_rejects_an_invalid_window_and_requires_stats_for_its_options(self):
        for since in ('0d', '-1d', 'yesterday', '1.5d', '9999999999999999999999999999w'):
            with self.subTest(since=since), self.assertRaises(CommandError):
                call_command('dl_transitions', stats=True, since=since, stdout=StringIO())
        with self.assertRaisesMessage(CommandError, '--since and --json require --stats'):
            call_command('dl_transitions', since='7d')
