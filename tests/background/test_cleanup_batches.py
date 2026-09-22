"""Cleanup commits completed batches and rechecks rows before deleting them."""
from collections import deque
from datetime import timedelta
import os
from pathlib import Path
import signal
import tempfile
import time
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.db import connections, transaction
from django.db.models import QuerySet
from django.db.models.signals import post_delete
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from django_logic.background import pull, safety_nets
from django_logic.background.models import TransitionMessage
from tests import dl_settings
from tests.stability.base import requires_postgres


def _old_messages(count, *, alias='default'):
    old = timezone.now() - timedelta(days=8)
    rows = TransitionMessage.objects.using(alias).bulk_create([
        TransitionMessage(
            app_label='bg_tests', model_name='widget', instance_id=str(index),
            process_name='cleanup_test', transition_name='finished',
            queue_name='django_logic.cleanup_test', is_completed=True, completed_at=old,
        ) for index in range(count)
    ])
    message_ids = [row.pk for row in rows]
    TransitionMessage.objects.using(alias).filter(pk__in=message_ids).update(modified=old)
    return message_ids


@override_settings(DJANGO_LOGIC=dl_settings(TRANSITION_MESSAGE_CLEANUP_DAYS=7))
class CleanupBatchTests(TransactionTestCase):
    def test_a_later_delete_failure_preserves_earlier_commits(self):
        message_ids = _old_messages(5)

        def reject_delete(sender, instance, **kwargs):
            if instance.pk == message_ids[2]:
                raise RuntimeError('The later batch could not finish.')

        post_delete.connect(reject_delete, sender=TransitionMessage)
        try:
            with patch.object(safety_nets, '_CLEANUP_BATCH_SIZE', 2):
                with self.assertRaisesRegex(RuntimeError, 'later batch'):
                    safety_nets.cleanup_completed_transitions()
        finally:
            post_delete.disconnect(reject_delete, sender=TransitionMessage)
        self.assertEqual(
            list(TransitionMessage.objects.order_by('pk').values_list('pk', flat=True)),
            message_ids[2:],
        )
        with patch.object(safety_nets, '_CLEANUP_BATCH_SIZE', 2):
            self.assertEqual(safety_nets.cleanup_completed_transitions(), 3)
        self.assertFalse(TransitionMessage.objects.exists())

    def test_an_outer_transaction_still_controls_the_commit(self):
        message_ids = _old_messages(3)
        with transaction.atomic():
            with patch.object(safety_nets, '_CLEANUP_BATCH_SIZE', 2):
                self.assertEqual(safety_nets.cleanup_completed_transitions(), 3)
            transaction.set_rollback(True)
        self.assertEqual(
            list(TransitionMessage.objects.order_by('pk').values_list('pk', flat=True)), message_ids,
        )

    def test_delete_rechecks_completion_age_and_failure_retention(self):
        original_delete = QuerySet.delete
        for update in (
            {'is_completed': False},
            {'modified': timezone.now()},
            {'ended_in_failure': True},
        ):
            with self.subTest(update=update):
                TransitionMessage.objects.all().delete()
                message_id = _old_messages(1)[0]

                def change_then_delete(queryset):
                    TransitionMessage.objects.filter(pk=message_id).update(**update)
                    return original_delete(queryset)

                with patch.object(QuerySet, 'delete', change_then_delete):
                    self.assertEqual(safety_nets.cleanup_completed_transitions(), 0)
                self.assertTrue(TransitionMessage.objects.filter(pk=message_id).exists())


class _CleanupRouter:
    def db_for_read(self, model, **hints):
        return 'default'

    def db_for_write(self, model, **hints):
        return 'other' if model is TransitionMessage else None


@skipUnless('other' in settings.DATABASES, 'needs the second database alias')
@override_settings(DJANGO_LOGIC=dl_settings(TRANSITION_MESSAGE_CLEANUP_DAYS=7))
class CleanupWriteAliasTests(TransactionTestCase):
    databases = {'default', 'other'} if 'other' in settings.DATABASES else {'default'}

    def test_selection_deletion_and_batch_commits_use_the_write_database(self):
        default_ids = _old_messages(1)
        other_ids = _old_messages(5, alias='other')

        def reject_delete(sender, instance, using, **kwargs):
            if using == 'other' and instance.pk == other_ids[2]:
                raise RuntimeError('The later batch could not finish.')

        post_delete.connect(reject_delete, sender=TransitionMessage)
        try:
            with override_settings(DATABASE_ROUTERS=[_CleanupRouter()]), \
                    patch.object(safety_nets, '_CLEANUP_BATCH_SIZE', 2):
                with self.assertRaisesRegex(RuntimeError, 'later batch'):
                    safety_nets.cleanup_completed_transitions()
        finally:
            post_delete.disconnect(reject_delete, sender=TransitionMessage)
        self.assertEqual(
            list(TransitionMessage.objects.using('default').values_list('pk', flat=True)), default_ids,
        )
        self.assertEqual(
            list(TransitionMessage.objects.using('other').order_by('pk').values_list('pk', flat=True)),
            other_ids[2:],
        )


@requires_postgres
@skipUnless(hasattr(os, 'fork'), 'needs os.fork')
@override_settings(DJANGO_LOGIC=dl_settings(
    BACKGROUND_EXECUTION='pull', TRANSITION_MESSAGE_CLEANUP_DAYS=7,
))
class CleanupDeadlineTests(TransactionTestCase):
    def test_killing_a_later_batch_preserves_progress_and_the_next_pass_finishes(self):
        message_ids = _old_messages(5)
        attempts = {}
        pending = deque()
        with tempfile.TemporaryDirectory(prefix='dl_cleanup_') as directory:
            marker = Path(directory) / 'later_batch_started'

            def slow_delete(sender, instance, **kwargs):
                if instance.pk == message_ids[2]:
                    marker.write_text(str(os.getpid()))
                    time.sleep(5)

            post_delete.connect(slow_delete, sender=TransitionMessage)
            started = time.monotonic()
            try:
                with patch.object(safety_nets, '_CLEANUP_BATCH_SIZE', 2), \
                        patch.object(pull, 'SAFETY_NET_TIMEOUT_SECONDS', 0.5):
                    pull._start_attempt(None, attempts)
                    child_pid, attempt = next(iter(attempts.items()))
                    while attempts or pending:
                        self.assertLess(time.monotonic() - started, 3, 'Cleanup child did not stop.')
                        pull._harvest(attempts, pending, block=False)
                        time.sleep(0.01)
                self.assertEqual(int(marker.read_text()), child_pid)
                self.assertTrue(attempt.killed)
                self.assertEqual(attempt.exit_code, -signal.SIGKILL)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(child_pid, os.WNOHANG)
                self.assertEqual(
                    list(TransitionMessage.objects.order_by('pk').values_list('pk', flat=True)),
                    message_ids[2:],
                )
            finally:
                post_delete.disconnect(slow_delete, sender=TransitionMessage)
                for pid, attempt in attempts.items():
                    if not attempt.reaped:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        os.waitpid(pid, 0)
                connections.close_all()
            with patch.object(safety_nets, '_CLEANUP_BATCH_SIZE', 2):
                self.assertEqual(safety_nets.cleanup_completed_transitions(), 3)
            self.assertFalse(TransitionMessage.objects.exists())
