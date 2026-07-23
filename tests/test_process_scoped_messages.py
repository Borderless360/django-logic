"""Issue #150 — TransitionMessage helpers scoped to the selected process.

Widget carries TWO independent state machines on one row: ``WidgetProcess``
on ``status`` (process_name ``'process'``) and ``WidgetAuditProcess`` on
``audit_status`` (process_name ``'audit_process'``). Both are driven into a
failed background attempt on the SAME instance, with the audit row created
LAST — so any helper that ignores ``process_name`` and just takes the newest
row returns the AUDIT row while the scenario is about the MAIN process.

These tests pin:

* the runner helpers (``uncompleted_message`` / ``latest_message`` /
  ``message_for``) return the right process's row when scoped by
  ``process_name``, and keep the legacy unscoped (newest-row) behaviour when
  it is omitted,
* ``ProcessScenario.retry_transition`` retries its OWN process's message,
* ``assert_error_recorded`` / ``assert_error_count`` /
  ``assert_transition_owner`` read the scenario's process's row,
* ``snapshot(process_name=...)`` captures the right TransitionMessage,
* the failure output's TransitionMessage block shows the scenario's own row.

Both processes are bound app-wide in ``tests/background/apps.py`` (the single
binding site), so no test-local ``ProcessManager`` bindings — and no teardown
purge — are needed here.
"""
from django_logic.testing import ProcessScenario, snapshot
from django_logic.testing.runner import (
    all_transitions,
    latest_message,
    message_for,
    run_background_sync,
    uncompleted_message,
)
from django_logic.testing.tracking import track
from tests.background.models import Widget, WidgetAuditProcess, WidgetProcess


_MAIN_OWNER = 'tests.background.models.WidgetProcess'
_AUDIT_OWNER = 'tests.background.models.WidgetAuditProcess'


class ProcessScopedMessageScenario(ProcessScenario):
    """Scenario scoped to the MAIN machine (WidgetProcess on ``status``),
    with the audit machine (``audit_process`` on ``audit_status``) driven
    out-of-band into a failed attempt on the same instance."""

    process_class = WidgetProcess
    model = Widget
    state_field = 'status'
    process_name = 'process'

    # --- fixtures ---------------------------------------------------------

    def _fail_fulfil(self, widget, message='fulfil down'):
        """Fail 'fulfil' on the MAIN process -> uncompleted TM
        (process_name='process'), status left at 'fulfilling'."""
        self.background_transition(
            widget, 'fulfil',
            fail_side_effect='bg_ok', fail_with=ValueError(message))

    @staticmethod
    def _fail_audit(widget, message='audit down'):
        """Fail 'audit' on the OTHER machine -> uncompleted TM
        (process_name='audit_process'), audit_status left at 'auditing'.

        Driven through the raw runner + tracker because this scenario is
        scoped to WidgetProcess; sync execution propagates the injected
        exception to the caller, so it is absorbed here."""
        with track(all_transitions(WidgetAuditProcess),
                   fail_side_effect='bg_audit_ok',
                   fail_with=ValueError(message)):
            try:
                run_background_sync(widget, 'audit_process', 'audit', {})
            except ValueError:
                pass
        widget.refresh_from_db()

    def _two_failed_processes(self):
        """One instance, both machines mid-flight. The audit TM is created
        SECOND, so it is the NEWEST row — an unscoped newest-row lookup
        returns the wrong (audit) row for this 'process'-scoped scenario."""
        widget = self.create_instance(status='draft', audit_status='clean')
        self._fail_fulfil(widget)
        self._fail_audit(widget)
        self.assertEqual(widget.status, 'fulfilling')
        self.assertEqual(widget.audit_status, 'auditing')
        return widget

    # --- (a) runner helpers -----------------------------------------------

    def test_helpers_scoped_by_process_name(self):
        widget = self._two_failed_processes()

        self.assertEqual(
            uncompleted_message(widget, process_name='process').transition_name,
            'fulfil')
        self.assertEqual(
            uncompleted_message(widget, process_name='audit_process').transition_name,
            'audit')

        self.assertEqual(
            latest_message(widget, process_name='process').transition_name,
            'fulfil')
        self.assertEqual(
            latest_message(widget, process_name='audit_process').transition_name,
            'audit')

        self.assertEqual(
            message_for(widget, 'fulfil', process_name='process').process_name,
            'process')
        self.assertEqual(
            message_for(widget, 'audit', process_name='audit_process').process_name,
            'audit_process')
        # Scoped to the WRONG process, the action's row must not be found.
        self.assertIsNone(message_for(widget, 'fulfil', process_name='audit_process'))
        self.assertIsNone(message_for(widget, 'audit', process_name='process'))

    def test_unscoped_helpers_keep_legacy_newest_row_behaviour(self):
        """process_name=None (the default) is the historical unscoped lookup
        — backward compatible for direct callers."""
        widget = self._two_failed_processes()
        # The audit row is the newest for this instance.
        self.assertEqual(uncompleted_message(widget).transition_name, 'audit')
        self.assertEqual(latest_message(widget).transition_name, 'audit')
        # message_for was always narrowed by action name.
        self.assertEqual(message_for(widget, 'fulfil').process_name, 'process')

    def test_scoped_helpers_across_completed_and_uncompleted_rows(self):
        widget = self._two_failed_processes()
        self.retry_transition(widget)  # completes the 'process' row
        self.assert_state(widget, 'fulfilled')

        # 'process': no uncompleted row left; latest is the completed fulfil.
        self.assertIsNone(uncompleted_message(widget, process_name='process'))
        tm = latest_message(widget, process_name='process')
        self.assertEqual(tm.transition_name, 'fulfil')
        self.assertTrue(tm.is_completed)

        # 'audit_process': its row is still in flight, untouched.
        audit_tm = uncompleted_message(widget, process_name='audit_process')
        self.assertIsNotNone(audit_tm)
        self.assertFalse(audit_tm.is_completed)

    # --- (b) retry_transition ---------------------------------------------

    def test_retry_transition_retries_only_its_own_process(self):
        widget = self._two_failed_processes()

        # The audit TM is newer; an unscoped lookup would pick it and then
        # refuse because WidgetProcess has no 'audit' transition. The scoped
        # scenario must retry its own 'fulfil' row.
        self.retry_transition(widget)  # no injection -> succeeds
        self.assert_state(widget, 'fulfilled')

        fulfil_tm = latest_message(widget, process_name='process')
        self.assertEqual(fulfil_tm.transition_name, 'fulfil')
        self.assertTrue(fulfil_tm.is_completed)

        # The audit machine is untouched: still mid-flight, still failed once.
        widget.refresh_from_db()
        self.assertEqual(widget.audit_status, 'auditing')
        audit_tm = uncompleted_message(widget, process_name='audit_process')
        self.assertIsNotNone(audit_tm)
        self.assertEqual(audit_tm.errors_count, 1)
        self.assertIn('audit down', audit_tm.last_error_message)

    # --- (c) error / owner assertions --------------------------------------

    def test_error_assertions_read_own_process_row(self):
        widget = self._two_failed_processes()

        # The NEWEST row is the audit one ('audit down'); the scoped scenario
        # must read its own fulfil row.
        self.assert_error_recorded(widget, 'fulfil down')
        # The other process's error must NOT satisfy the scoped assertion.
        with self.assertRaises(AssertionError):
            self.assert_error_recorded(widget, 'audit down')

    def test_error_count_reads_own_process_row(self):
        widget = self.create_instance(status='draft', audit_status='clean')
        self._fail_fulfil(widget)
        # Fail the fulfil retry too -> 'process' row has errors_count=2.
        self.retry_transition(
            widget, fail_side_effect='bg_ok', fail_with=ValueError('fulfil down'))
        self._fail_audit(widget)  # newest row, errors_count=1

        self.assert_error_count(widget, 2)  # own row, not the newer audit one
        with self.assertRaises(AssertionError):
            self.assert_error_count(widget, 1)

    def test_transition_owner_reads_own_process_row(self):
        widget = self._two_failed_processes()

        # latest_message branch: the newest TM records the AUDIT owner, but
        # the scoped scenario must see its own process's owner.
        self.assert_transition_owner(widget, _MAIN_OWNER)
        with self.assertRaises(AssertionError):
            self.assert_transition_owner(widget, _AUDIT_OWNER)

        # message_for branch, scoped as well.
        self.assert_transition_owner(widget, _MAIN_OWNER, transition_name='fulfil')

    # --- (d) snapshot -------------------------------------------------------

    def test_snapshot_captures_own_process_tm(self):
        widget = self._two_failed_processes()

        snap_main = snapshot(widget, state_field='status',
                             process_name='process')
        self.assertEqual(snap_main['state'], 'fulfilling')
        self.assertEqual(snap_main['transition_message']['transition_name'],
                         'fulfil')
        self.assertEqual(snap_main['transition_message']['process_name'],
                         'process')

        snap_audit = snapshot(widget, state_field='audit_status',
                              process_name='audit_process')
        self.assertEqual(snap_audit['state'], 'auditing')
        self.assertEqual(snap_audit['transition_message']['transition_name'],
                         'audit')
        self.assertEqual(snap_audit['transition_message']['process_name'],
                         'audit_process')

        # The scenario's own snapshot() threads its process scope.
        snap = self.snapshot(widget)
        self.assertEqual(snap['transition_message']['transition_name'], 'fulfil')

    # --- failure output (scenario._fail threads the scope) ------------------

    def test_failure_output_shows_own_process_tm(self):
        widget = self._two_failed_processes()
        with self.assertRaises(AssertionError) as ctx:
            self.assert_state(widget, 'fulfilled')  # actual is 'fulfilling'
        msg = str(ctx.exception)
        # The TransitionMessage block shows the scenario's own row, not the
        # newer audit row.
        self.assertIn('transition: fulfil', msg)
        self.assertIn('fulfil down', msg)
        self.assertNotIn('transition: audit', msg)
