"""Process-LEVEL conditions & permissions (``Process.is_valid``).

Distinct from *transition-level* guards: a class-level ``conditions`` /
``permissions`` list gates the WHOLE process at once — its own transitions and
every nested process's transitions — because ``_iter_available_with_owner``
short-circuits the entire subtree when ``is_valid(user)`` is False
(``process.py``). The migration to journey tests dropped this coverage
entirely; disabling ``is_valid`` used to pass the whole suite. These tests
restore it in both directions (allowed / blocked) and include the
nested-inheritance case, on a real persisted instance.

``WidgetProcGuardProcess`` (bound as ``proc_guard``) has:
  conditions=[process_gate_open]      # instance flagged 'gate_open'
  permissions=[process_requires_staff] # a staff user
  transitions=[go]                     # its own
  nested_processes=[GuardedInnerProcess]  # inner_go (no guards of its own)
"""
from django.contrib.auth import get_user_model

from django_logic.exceptions import TransitionNotAllowed
from django_logic.testing import ProcessScenario
from tests.background.models import Widget, WidgetProcGuardProcess


class ProcessLevelGuardScenario(ProcessScenario):
    process_class = WidgetProcGuardProcess
    model = Widget
    state_field = 'status'
    process_name = 'proc_guard'

    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.staff = User.objects.create(username='pg_staff', is_staff=True)
        self.customer = User.objects.create(username='pg_customer', is_staff=False)

    # --- allowed direction ------------------------------------------------

    def test_open_gate_and_staff_allows_own_transition(self):
        widget = self.create_instance(status='draft', kwargs_seen=['gate_open'])
        self.assert_available(widget, ['go', 'inner_go'], user=self.staff)
        self.transition(widget, 'go', user=self.staff)
        self.assert_state(widget, 'gone')
        self.assertIn('go,', widget.se_log)

    def test_open_gate_and_staff_allows_nested_transition(self):
        # inner_go is declared on the nested GuardedInnerProcess; it is only
        # reachable because the PARENT's process-level guard passed.
        widget = self.create_instance(status='draft', kwargs_seen=['gate_open'])
        self.transition(widget, 'inner_go', user=self.staff)
        self.assert_state(widget, 'inner_gone')
        self.assertIn('inner_go,', widget.se_log)

    # --- blocked by process-level CONDITION -------------------------------

    def test_closed_gate_hides_own_and_nested_transitions(self):
        widget = self.create_instance(status='draft', kwargs_seen=[])  # gate closed
        # The process-level condition fails, so NOTHING is available — not the
        # class's own transition, not the nested one.
        self.assert_not_available(widget, ['go', 'inner_go'], user=self.staff)

    def test_closed_gate_blocks_drive_with_object_unchanged(self):
        widget = self.create_instance(status='draft', kwargs_seen=[])
        self.transition(widget, 'go', user=self.staff,
                        expect_raises=TransitionNotAllowed)
        # No state write, no side-effect: the process-level condition rejected
        # the call at resolve time.
        self.assert_state(widget, 'draft')
        self.assertNotIn('go,', widget.se_log)
        self.assert_state_trace([])

    def test_closed_gate_blocks_nested_drive_with_object_unchanged(self):
        widget = self.create_instance(status='draft', kwargs_seen=[])
        self.transition(widget, 'inner_go', user=self.staff,
                        expect_raises=TransitionNotAllowed)
        self.assert_state(widget, 'draft')
        self.assertNotIn('inner_go,', widget.se_log)

    # --- blocked by process-level PERMISSION ------------------------------

    def test_non_staff_is_blocked_even_with_open_gate(self):
        widget = self.create_instance(status='draft', kwargs_seen=['gate_open'])
        # Condition passes, but the process-level permission fails for a
        # non-staff user — again gating both the own and the nested transition.
        self.assert_not_available(widget, ['go', 'inner_go'], user=self.customer)

    def test_non_staff_drive_blocked_with_object_unchanged(self):
        widget = self.create_instance(status='draft', kwargs_seen=['gate_open'])
        self.transition(widget, 'go', user=self.customer,
                        expect_raises=TransitionNotAllowed)
        self.assert_state(widget, 'draft')
        self.assertNotIn('go,', widget.se_log)
