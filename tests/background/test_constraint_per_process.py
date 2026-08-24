"""The uncompleted-row guard is per process, not per instance.

TransitionMessage's partial unique constraint covers (app_label, model_name,
instance_id, process_name) where is_completed is false. Two state machines bound
to different fields of the same model row — here WidgetProcess on Widget.status
and WidgetAuditProcess on Widget.audit_status — may both have background work in
progress.

These tests pin:
* an uncompleted row on one process does not block another process on the same
  instance,
* a second background transition on the same process still raises
  AlreadyInProgress, and the cache lock is released,
* the constraint itself, under direct row inserts,
* the DEFAULT_QUEUE fallback for a transition that declares no queue
  (WidgetAuditProcess.audit deliberately omits it).
"""
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase, override_settings

from django_logic.background import sync_execution
from django_logic.background.exceptions import AlreadyInProgress
from django_logic.background.models import TransitionMessage
from django_logic.state import State
from tests.background.models import Widget
from tests import dl_settings


def _make_uncompleted_fulfil_row(widget):
    """Write what enqueue leaves behind for WidgetProcess.fulfil: the
    in_progress_state on the instance and an uncompleted TransitionMessage."""
    widget.status = 'fulfilling'
    widget.save(update_fields=['status'])
    return TransitionMessage.objects.create(
        app_label='bg_tests',
        model_name='widget',
        instance_id=str(widget.pk),
        process_name='process',
        field_name='status',
        transition_name='fulfil',
        queue_name='django_logic.critical',
    )


class IndependentProcessesTests(TestCase):
    """With 'process' work uncompleted, the independent 'audit_process' state
    machine on the same instance still runs."""

    def test_other_process_proceeds_while_one_is_uncompleted(self):
        widget = Widget.objects.create()
        process_row = _make_uncompleted_fulfil_row(widget)

        # With the old instance-wide constraint this raised AlreadyInProgress,
        # because the audit row collided with the uncompleted 'process' row.
        with sync_execution():
            widget.audit_process.audit()

        widget.refresh_from_db()
        self.assertEqual(widget.audit_status, 'audited')
        self.assertIn('audit_ok,', widget.se_log)

        audit_row = TransitionMessage.objects.get(process_name='audit_process')
        self.assertTrue(audit_row.is_completed)
        self.assertEqual(audit_row.instance_id, str(widget.pk))

        # The 'process' row stays uncompleted, untouched by the audit run.
        process_row.refresh_from_db()
        self.assertFalse(process_row.is_completed)
        self.assertEqual(process_row.errors_count, 0)
        self.assertEqual(widget.status, 'fulfilling')


class SameProcessDuplicateTests(TransactionTestCase):
    """The constraint still rejects a second background transition on the same
    process, and enqueue releases the cache lock on the way out."""

    def test_same_process_duplicate_raises_already_in_progress(self):
        widget = Widget.objects.create()
        _make_uncompleted_fulfil_row(widget)

        # Put the instance back on a declared source, so the failure we see is
        # the constraint and not the source gate.
        widget.status = 'draft'
        widget.save(update_fields=['status'])

        with sync_execution():
            with self.assertRaises(AlreadyInProgress) as ctx:
                widget.process.fulfil()

        # The exception names the conflicting process.
        self.assertIn("process 'process'", str(ctx.exception))

        # No second row was created for this instance and process.
        self.assertEqual(
            TransitionMessage.objects.filter(
                app_label='bg_tests',
                model_name='widget',
                instance_id=str(widget.pk),
                process_name='process',
            ).count(),
            1,
        )

        # The failed attempt's in_progress_state write rolled back with the
        # enqueue transaction, so the instance is where we left it.
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'draft')

        # Enqueue releases the cache lock on the way out, so the instance is
        # not left locked.
        self.assertFalse(State(widget, 'status', 'process').is_locked())


class ConstraintAtDatabaseLevelTests(TransactionTestCase):
    """The partial unique constraint itself, pinned with direct inserts and no
    engine code in the way."""

    _ROW = {
        'app_label': 'bg_tests',
        'model_name': 'widget',
        'instance_id': '42',
        'process_name': 'process',
        'transition_name': 'fulfil',
        'queue_name': 'django_logic.critical',
    }

    def test_duplicate_uncompleted_same_process_violates_constraint(self):
        TransitionMessage.objects.create(**self._ROW)
        with self.assertRaises(IntegrityError):
            # atomic() contains the broken transaction, so the assertions
            # below can still query.
            with transaction.atomic():
                TransitionMessage.objects.create(**self._ROW)
        self.assertEqual(TransitionMessage.objects.count(), 1)

    def test_differing_process_name_inserts_fine(self):
        TransitionMessage.objects.create(**self._ROW)
        TransitionMessage.objects.create(
            **{**self._ROW, 'process_name': 'audit_process',
               'transition_name': 'audit', 'queue_name': 'django_logic'}
        )
        self.assertEqual(
            TransitionMessage.objects.filter(
                instance_id='42', is_completed=False
            ).count(),
            2,
        )

    def test_constraint_is_partial_completed_rows_do_not_block(self):
        TransitionMessage.objects.create(**{**self._ROW, 'is_completed': True})
        # The same keys again, uncompleted — allowed, because the constraint
        # only covers is_completed=False rows.
        TransitionMessage.objects.create(**self._ROW)
        self.assertEqual(TransitionMessage.objects.count(), 2)


class DefaultQueueFallbackTests(TestCase):
    """WidgetAuditProcess.audit declares no queue, so enqueue records
    DJANGO_LOGIC['DEFAULT_QUEUE'] on the row."""

    def test_row_records_builtin_default_queue(self):
        widget = Widget.objects.create()
        with sync_execution():
            widget.audit_process.audit()
        audit_row = TransitionMessage.objects.get(process_name='audit_process')
        self.assertTrue(audit_row.is_completed)
        self.assertEqual(audit_row.queue_name, 'django_logic')

    def test_row_records_overridden_default_queue(self):
        widget = Widget.objects.create()
        with override_settings(
            DJANGO_LOGIC=dl_settings(DEFAULT_QUEUE='custom.q')
        ):
            with sync_execution():
                widget.audit_process.audit()
        audit_row = TransitionMessage.objects.get(process_name='audit_process')
        self.assertTrue(audit_row.is_completed)
        self.assertEqual(audit_row.queue_name, 'custom.q')
