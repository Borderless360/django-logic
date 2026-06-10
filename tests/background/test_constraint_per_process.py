"""R5 regression — the in-flight concurrency guard is per PROCESS, not
per instance.

0.4 changed TransitionMessage's partial unique constraint from
(app_label, model_name, instance_id) to (app_label, model_name,
instance_id, process_name) WHERE is_completed=False (migration 0006,
constraint 'dl_bg_one_uncompleted_per_process'). Two independent state
machines bound to different fields of the same model row (here
WidgetProcess on Widget.status and WidgetAuditProcess on
Widget.audit_status) may now both have background work in flight.

These tests pin:
* (a) cross-process independence — an uncompleted row on one process no
  longer blocks another process on the same instance,
* (b) same-process duplicates are still rejected with AlreadyInProgress
  and the cache lock is released,
* (c) the DB constraint itself, exercised with direct row inserts,
* (d) the DEFAULT_QUEUE fallback for transitions that declare no queue=
  (WidgetAuditProcess.audit deliberately omits it).
"""
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase, override_settings

from django_logic.background.dispatch import sync_execution
from django_logic.background.exceptions import AlreadyInProgress
from django_logic.background.models import TransitionMessage
from django_logic.state import State
from tests.background.models import Widget


_SYNC_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 5,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}


def _make_inflight_process_tm(widget):
    """Simulate phase 1 of WidgetProcess.fulfil left in flight: an
    uncompleted TransitionMessage on process 'process' plus the
    in_progress_state on the instance."""
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


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class R5IndependentProcessesTests(TestCase):
    """R5 (a): with 'process' work in flight, the independent
    'audit_process' state machine on the same instance still runs."""

    def test_other_process_proceeds_while_one_is_in_flight(self):
        widget = Widget.objects.create()
        process_tm = _make_inflight_process_tm(widget)

        # Pre-fix (instance-wide constraint) this raised AlreadyInProgress
        # because the audit TM collided with the uncompleted 'process' row.
        with sync_execution():
            widget.audit_process.audit()

        widget.refresh_from_db()
        self.assertEqual(widget.audit_status, 'audited')
        self.assertIn('audit_ok,', widget.se_log)

        audit_tm = TransitionMessage.objects.get(process_name='audit_process')
        self.assertTrue(audit_tm.is_completed)
        self.assertEqual(audit_tm.instance_id, str(widget.pk))

        # The 'process' row stays in flight, untouched by the audit run.
        process_tm.refresh_from_db()
        self.assertFalse(process_tm.is_completed)
        self.assertEqual(process_tm.errors_count, 0)
        self.assertEqual(widget.status, 'fulfilling')


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class R5SameProcessDuplicateTests(TransactionTestCase):
    """R5 (b): the constraint still rejects a SECOND background
    transition on the SAME process, via AlreadyInProgress, and phase 1
    releases the cache lock on the way out."""

    def test_same_process_duplicate_raises_already_in_progress(self):
        widget = Widget.objects.create()
        _make_inflight_process_tm(widget)

        # Put the instance back on a declared source so the source gate
        # passes and the failure we observe is unambiguously the
        # constraint, not source validation.
        widget.status = 'draft'
        widget.save(update_fields=['status'])

        with sync_execution():
            with self.assertRaises(AlreadyInProgress) as ctx:
                widget.process.fulfil()

        # The message names the conflicting process.
        self.assertIn("process 'process'", str(ctx.exception))

        # No second row was created for this instance+process.
        self.assertEqual(
            TransitionMessage.objects.filter(
                app_label='bg_tests',
                model_name='widget',
                instance_id=str(widget.pk),
                process_name='process',
            ).count(),
            1,
        )

        # The in_progress_state write of the failed attempt rolled back
        # with phase 1's atomic block — the instance is where we left it.
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'draft')

        # The cache lock taken for the phase-1 critical section was
        # released in the finally — the instance is not stranded locked.
        self.assertFalse(State(widget, 'status', 'process').is_locked())


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class R5ConstraintDbLevelTests(TransactionTestCase):
    """R5 (c): the partial unique constraint itself, pinned at the DB
    level with direct inserts (no phase-1 machinery)."""

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
            # atomic() so the broken transaction state is contained and
            # the assertions below can keep querying.
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
        # Same keys again, uncompleted — allowed, the constraint only
        # covers is_completed=False rows.
        TransitionMessage.objects.create(**self._ROW)
        self.assertEqual(TransitionMessage.objects.count(), 2)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class R5DefaultQueueFallbackTests(TestCase):
    """R5 (d): WidgetAuditProcess.audit declares no queue= — phase 1
    records DJANGO_LOGIC['DEFAULT_QUEUE'] on the TransitionMessage,
    resolved lazily at dispatch time."""

    def test_tm_records_builtin_default_queue(self):
        widget = Widget.objects.create()
        with sync_execution():
            widget.audit_process.audit()
        tm = TransitionMessage.objects.get(process_name='audit_process')
        self.assertTrue(tm.is_completed)
        self.assertEqual(tm.queue_name, 'django_logic')

    def test_tm_records_overridden_default_queue(self):
        widget = Widget.objects.create()
        with override_settings(
            DJANGO_LOGIC={**_SYNC_SETTINGS, 'DEFAULT_QUEUE': 'custom.q'}
        ):
            with sync_execution():
                widget.audit_process.audit()
        tm = TransitionMessage.objects.get(process_name='audit_process')
        self.assertTrue(tm.is_completed)
        self.assertEqual(tm.queue_name, 'custom.q')
