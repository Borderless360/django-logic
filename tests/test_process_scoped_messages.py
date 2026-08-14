"""Every TransitionMessage helper must be scoped to the process you asked about.

Widget carries two independent state machines on one row: ``WidgetProcess`` on
``status`` and ``WidgetAuditProcess`` on ``audit_status``. Both are driven into
a failed background attempt on the same instance, and the audit row is saved
last. So a helper that ignores ``process_name`` and takes the newest row hands
back the audit row while the scenario is about the main process.

Both processes are bound in ``tests/background/apps.py``, so this module needs
no binding of its own and no teardown.
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
    """Scoped to WidgetProcess on ``status``. The audit machine is driven into
    a failed attempt on the same instance from outside the scenario."""

    process_class = WidgetProcess
    model = Widget
    state_field = 'status'
    process_name = 'process'

    # --- fixtures ---------------------------------------------------------

    def _fail_fulfil(self, widget, message='fulfil down'):
        """Fail 'fulfil' on the main process. Its row stays uncompleted and
        status stays at 'fulfilling'."""
        self.background_transition(
            widget, 'fulfil',
            fail_side_effect='bg_ok', fail_with=ValueError(message))

    @staticmethod
    def _fail_audit(widget, message='audit down'):
        """Fail 'audit' on the other machine. Its row stays uncompleted and
        audit_status stays at 'auditing'.

        This calls the runner directly, because the scenario is scoped to
        WidgetProcess. Sync execution re-raises the injected error, so catch
        it here."""
        with track(all_transitions(WidgetAuditProcess),
                   fail_side_effect='bg_audit_ok',
                   fail_with=ValueError(message)):
            try:
                run_background_sync(widget, 'audit_process', 'audit', {})
            except ValueError:
                pass
        widget.refresh_from_db()

    def _two_failed_processes(self):
        """One instance, both machines in progress. The audit row is saved
        second, so an unscoped newest-row lookup returns the wrong row."""
        widget = self.create_instance(status='draft', audit_status='clean')
        self._fail_fulfil(widget)
        self._fail_audit(widget)
        self.assertEqual(widget.status, 'fulfilling')
        self.assertEqual(widget.audit_status, 'auditing')
        return widget

    # --- runner helpers ---------------------------------------------------

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
        # Scoped to the other process, the row must not be found.
        self.assertIsNone(message_for(widget, 'fulfil', process_name='audit_process'))
        self.assertIsNone(message_for(widget, 'audit', process_name='process'))

    def test_helpers_require_a_process_name(self):
        """The scope is required. An unscoped lookup on a two-process model
        used to return the other machine's row."""
        widget = self._two_failed_processes()
        for call in (lambda: uncompleted_message(widget),
                     lambda: latest_message(widget),
                     lambda: message_for(widget, 'fulfil')):
            with self.subTest(call=call):
                with self.assertRaises(TypeError):
                    call()

    def test_scoped_helpers_across_completed_and_uncompleted_rows(self):
        widget = self._two_failed_processes()
        self.retry_transition(widget)  # completes the 'process' row
        self.assert_state(widget, 'fulfilled')

        # 'process' has no uncompleted row left; the latest is the completed one.
        self.assertIsNone(uncompleted_message(widget, process_name='process'))
        transition_message = latest_message(widget, process_name='process')
        self.assertEqual(transition_message.transition_name, 'fulfil')
        self.assertTrue(transition_message.is_completed)

        # The audit row is still uncompleted and untouched.
        audit_message = uncompleted_message(widget, process_name='audit_process')
        self.assertIsNotNone(audit_message)
        self.assertFalse(audit_message.is_completed)

    # --- retry_transition -------------------------------------------------

    def test_retry_transition_retries_only_its_own_process(self):
        widget = self._two_failed_processes()

        # The audit row is newer. An unscoped lookup would pick it and then
        # refuse, because WidgetProcess has no 'audit' transition.
        self.retry_transition(widget)  # no injected failure
        self.assert_state(widget, 'fulfilled')

        fulfil_message = latest_message(widget, process_name='process')
        self.assertEqual(fulfil_message.transition_name, 'fulfil')
        self.assertTrue(fulfil_message.is_completed)

        # The audit machine is untouched: still uncompleted, still one error.
        widget.refresh_from_db()
        self.assertEqual(widget.audit_status, 'auditing')
        audit_message = uncompleted_message(widget, process_name='audit_process')
        self.assertIsNotNone(audit_message)
        self.assertEqual(audit_message.errors_count, 1)
        self.assertIn('audit down', audit_message.last_error_message)

    # --- error and owner assertions ---------------------------------------

    def test_error_assertions_read_own_process_row(self):
        widget = self._two_failed_processes()

        # The newest row is the audit one, but the scenario reads its own.
        self.assert_error_recorded(widget, 'fulfil down')
        # The other process's error must NOT satisfy the scoped assertion.
        with self.assertRaises(AssertionError):
            self.assert_error_recorded(widget, 'audit down')

    def test_error_count_reads_own_process_row(self):
        widget = self.create_instance(status='draft', audit_status='clean')
        self._fail_fulfil(widget)
        # Fail the retry too, so the 'process' row reaches errors_count=2.
        self.retry_transition(
            widget, fail_side_effect='bg_ok', fail_with=ValueError('fulfil down'))
        self._fail_audit(widget)  # newest row, errors_count=1

        self.assert_error_count(widget, 2)  # its own row, not the newer one
        with self.assertRaises(AssertionError):
            self.assert_error_count(widget, 1)

    def test_transition_owner_reads_own_process_row(self):
        widget = self._two_failed_processes()

        # The newest row records the audit owner, but the scenario must read
        # the owner on its own process's row.
        self.assert_transition_owner(widget, _MAIN_OWNER)
        with self.assertRaises(AssertionError):
            self.assert_transition_owner(widget, _AUDIT_OWNER)

        # The lookup by transition name is scoped the same way.
        self.assert_transition_owner(widget, _MAIN_OWNER, transition_name='fulfil')

    # --- snapshot ----------------------------------------------------------

    def test_snapshot_captures_own_process_row(self):
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

        # The scenario's own snapshot() passes its process scope through.
        snap = self.snapshot(widget)
        self.assertEqual(snap['transition_message']['transition_name'], 'fulfil')

    # --- failure output ----------------------------------------------------

    def test_failure_output_shows_own_process_row(self):
        widget = self._two_failed_processes()
        with self.assertRaises(AssertionError) as ctx:
            self.assert_state(widget, 'fulfilled')  # the real state is 'fulfilling'
        message = str(ctx.exception)
        # The TransitionMessage block shows the scenario's own row, not the
        # newer audit row.
        self.assertIn('transition: fulfil', message)
        self.assertIn('fulfil down', message)
        self.assertNotIn('transition: audit', message)
