"""Named engine contract: the re-raise / swallow exception asymmetry.

This is the contract the ``0.1.6 -> 0.2.0`` upgrade silently flipped (see
``fundamental problem.md`` §3 and the process-testing report §4.1). The engine
deliberately treats the four hook families asymmetrically at the *caller
boundary*:

* ``SideEffects`` — on failure, run ``fail_transition``, then **RE-RAISE** so
  the caller observes the failure (``commands.py`` ``SideEffects.execute``).
* ``Callbacks`` — best-effort; exceptions are **swallowed**.
* ``NextTransition`` — a follow-up's failure is **swallowed** (it must not
  bubble into the transition that triggered it).
* ``FailureSideEffects`` — a raising cleanup hook is **swallowed** and must not
  mask the original exception (which still re-raises).

Every test drives a real object through the real entrypoint and pins what
reaches the caller via ``expect_raises`` / ``assert_raised`` /
``assert_not_raised``. These are the tests that fail loudly if a future engine
change reverts ``SideEffects.execute`` to swallow — before it reaches
production, not during it.
"""
from django_logic.testing import JourneyStep, ProcessScenario
from tests.background.models import (
    SYNC_ORDER,
    Widget,
    WidgetSyncProcess,
)


class SideEffectReRaiseContract(ProcessScenario):
    """SideEffects re-raise: a failing side-effect surfaces to the caller."""

    process_class = WidgetSyncProcess
    model = Widget
    state_field = 'status'
    process_name = 'sync_proc'

    def test_side_effect_failure_reraises_to_caller(self):
        # Inject a failure into reject's side-effect. The engine must write
        # failed_state, run the failure hooks, AND re-raise the exception to
        # the caller of the entrypoint.
        widget = self.create_instance(status='draft')
        self.transition(
            widget, 'reject',
            fail_side_effect='se_reject_attempt', fail_with=ValueError('reject boom'),
            expect_raises=ValueError,
        )
        # The caller saw the exception (expect_raises above) AND the object
        # landed in its failed_state with both failure hook families run.
        self.assert_raised(ValueError, match='reject boom')
        self.assert_state(widget, 'rejection_failed')
        self.assert_failure_side_effects_ran(['fse_cleanup'])
        self.assert_failure_callbacks_ran(['fcb_on_fail'])

    def test_genuine_side_effect_exception_reaches_caller_without_injection(self):
        # capture_fail's side-effect (sync_boom) always raises — no injection.
        # The genuinely-raising hook must still reach the caller.
        widget = self.create_instance(status='draft')
        self.transition(widget, 'capture_fail', expect_raises=ValueError)
        self.assert_raised(ValueError, match='sync boom')
        self.assert_state(widget, 'capture_failed')

    def test_journey_marks_the_failure_as_reaching_the_caller(self):
        # The journey step's ``failed`` flag records that an exception reached
        # the caller. This one assertion detects a swallow-vs-reraise flip:
        # under a swallow regression, failed would be False and this fails.
        widget = self.create_instance(status='draft')
        self.transition(
            widget, 'reject',
            fail_side_effect='se_reject_attempt', fail_with=ValueError('boom'),
            expect_raises=ValueError,
        )
        self.assert_journey([
            JourneyStep(
                action='reject',
                before='draft',
                after='rejection_failed',
                side_effects=[],          # the attempt raised, so it never "ran"
                callbacks=[],
                failed=True,              # <- the re-raise pin
            ),
        ])

    def test_failure_side_effect_that_raises_does_not_mask_the_original(self):
        # reject_bad_cleanup: side-effect raises ValueError, then its
        # failure_side_effect (sync_fse_boom) ALSO raises RuntimeError. The
        # cleanup failure must be swallowed and must NOT replace the original
        # ValueError, which still re-raises. The failure callback still runs.
        widget = self.create_instance(status='draft')
        self.transition(widget, 'reject_bad_cleanup', expect_raises=ValueError)
        self.assert_raised(ValueError, match='sync boom')  # original, not RuntimeError
        self.assert_state(widget, 'rbc_failed')
        self.assert_failure_callbacks_ran(['fcb_rbc'])


class SwallowContract(ProcessScenario):
    """Callbacks / NextTransition swallow: a best-effort failure must NOT
    reach the caller, and the object keeps the state it legitimately reached."""

    process_class = WidgetSyncProcess
    model = Widget
    state_field = 'status'
    process_name = 'sync_proc'

    def test_callback_failure_is_swallowed_and_target_kept(self):
        # boom_callback's callback raises. The target is already written, so
        # the exception is swallowed and the object keeps its target state.
        widget = self.create_instance(status='draft')
        self.transition(widget, 'boom_callback', expect_raises=False)
        self.assert_not_raised()
        self.assert_state(widget, 'boom_done')

    def test_next_transition_failure_is_swallowed_and_parent_kept(self):
        # approve completes (target 'approved') then chains into notify. Inject
        # a failure into notify's side-effect (se_c): the follow-up fails, but
        # NextTransition swallows it — the parent keeps 'approved' and the
        # caller sees no exception.
        widget = self.create_instance(status='draft')
        self.transition(
            widget, 'approve',
            fail_side_effect='se_c', fail_with=ValueError('notify boom'),
            expect_raises=False,
        )
        self.assert_not_raised()
        # The parent kept its target — the load-bearing proof the follow-up's
        # failure was swallowed (a re-raise would have failed the whole drive).
        self.assert_state(widget, 'approved')
        self.assert_side_effects_ran(['se_a', 'se_b'])  # approve's own ran
        # (se_c is the injected target, so it never records as "ran"
        # regardless of engine behaviour — not asserted; the kept 'approved'
        # state above is what proves notify failed and was swallowed.)

    def test_failure_hooks_run_in_order_across_the_boundary(self):
        # Independent of the caller boundary: failure_side_effects run before
        # failure_callbacks. Pinned via the cross-hook SYNC_ORDER sink.
        SYNC_ORDER.clear()
        widget = self.create_instance(status='draft')
        self.transition(
            widget, 'reject',
            fail_side_effect='se_reject_attempt', fail_with=ValueError('boom'),
            expect_raises=ValueError,
        )
        self.assertEqual(SYNC_ORDER, ['fse:cleanup', 'fcb:on_fail'])
