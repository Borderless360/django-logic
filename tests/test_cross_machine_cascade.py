"""Named engine contract: the cross-machine failure cascade.

``fundamental problem.md`` §3 documents the anti-pattern that silently changed
behaviour between 0.1.6 and 0.2.0: an outer transition's *side-effect* drives a
transition on a **different** state machine (another instance) and lets that
inner failure propagate. This test pins every leg of the resulting cascade as
ONE journey, so an engine change of that magnitude fails here first:

  1. the inner machine lands in its ``failed_state`` with its failure hooks run;
  2. the exception propagates out of the inner transition;
  3. the outer transition's ``fail_transition`` runs — outer lands in its own
     ``failed_state`` with its failure hooks run;
  4. the outer side-effects declared AFTER the nested call are SKIPPED;
  5. the outer success callbacks are SKIPPED;
  6. the exception reaches the CALLER of the outer transition.

(The fan-out pattern in ``docs/recipes/nested-processes.md`` is the correct way
to model parent→children work; this test does not endorse the anti-pattern, it
locks its real behaviour so a regression can't move it unnoticed.)
"""
from django_logic.testing import JourneyStep, ProcessScenario
from tests.background import models
from tests.background.models import (
    CascadeOuterProcess,
    Widget,
)


class CrossMachineCascadeContract(ProcessScenario):
    process_class = CascadeOuterProcess
    model = Widget
    state_field = 'status'
    process_name = 'cascade_outer'

    def setUp(self):
        super().setUp()
        models.CASCADE_ORDER.clear()

    def test_inner_failure_cascades_and_reaches_the_caller(self):
        outer = self.create_instance(status='draft')
        inner = self.create_instance(status='draft')

        # Drive the outer machine; its second side-effect drives the inner
        # machine, which fails. The exception must reach us (expect_raises).
        self.transition(
            outer, 'outer_fulfil', inner_pk=inner.pk, expect_raises=ValueError,
        )
        self.assert_raised(ValueError, match='inner machine failed')

        # (1)+(2) inner machine is contained in ITS failed_state, ITS failure
        # callback ran.
        inner.refresh_from_db()
        self.assertEqual(inner.status, 'inner_failed')
        self.assertIn('inner_fcb,', inner.cb_log)

        # (3) outer machine landed in ITS failed_state; outer failure callback
        # ran (tracked, since it lives on the driven process).
        self.assert_state(outer, 'outer_failed')
        self.assert_failure_callbacks_ran(['cascade_outer_fcb'])

        # (4) the side-effect BEFORE the nested call ran; the one AFTER it was
        # skipped when the nested call raised.
        outer.refresh_from_db()
        self.assertIn('outer_before,', outer.se_log)
        self.assertNotIn('outer_after,', outer.se_log)
        self.assert_side_effects_ran(['cascade_outer_before'])
        self.assert_side_effects_not_ran(['cascade_outer_after'])

        # (5) the outer SUCCESS callback never ran.
        self.assertNotIn('outer_cb,', outer.cb_log)

        # The full ordered cascade, across both machines. The inner machine
        # fails and cleans up entirely before control returns to the outer
        # machine's failure path.
        self.assertEqual(models.CASCADE_ORDER, [
            'outer:before',
            'outer:call_inner',
            'inner:side_effect',
            'inner:failure_callback',
            'outer:failure_callback',
        ])

    def test_journey_pins_the_whole_cascade(self):
        outer = self.create_instance(status='draft')
        inner = self.create_instance(status='draft')
        self.transition(
            outer, 'outer_fulfil', inner_pk=inner.pk, expect_raises=ValueError,
        )
        # One assertion locks the outer machine's observable transformation:
        # draft -> outer_failed, only the pre-nested side-effect ran, no
        # success callback, and the failure DID reach the caller (failed=True).
        self.assert_journey([
            JourneyStep(
                action='outer_fulfil',
                before='draft',
                after='outer_failed',
                side_effects=['cascade_outer_before'],
                callbacks=[],
                failed=True,
            ),
        ])
