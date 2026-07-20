"""Scenario-based testing for Django Logic processes.

Test FSM workflows the way they read in a business document — state
transitions, conditions, permissions, side-effects, background jobs, failures
and retries — as ordinary unit tests, **without a Celery broker**.

    from django_logic.testing import ProcessScenario

    class TestOrders(ProcessScenario):
        process_class = OrderProcess
        model = Order
        state_field = 'status'

        def test_happy_path(self):
            order = self.create_instance(status='approved')
            self.background_transition(order, 'fulfil')
            self.assert_state(order, 'fulfilled')

``snapshot()`` / ``from_snapshot()`` capture a production instance's state as
JSON and rebuild it in a test, turning a production bug into a regression test.

See ``docs/design/TESTING_SCENARIOS.md`` for the full design.
"""
from django_logic.testing.idempotency import assert_idempotent
from django_logic.testing.scenario import ProcessScenario, JourneyStep
from django_logic.testing.snapshot import from_snapshot, snapshot, to_json
from django_logic.testing.tracking import ExecutionTracker

__all__ = [
    'ProcessScenario',
    'JourneyStep',
    'snapshot',
    'from_snapshot',
    'to_json',
    'ExecutionTracker',
    'assert_idempotent',
]
