"""Business-level assertions for :class:`ProcessScenario`.

Each assertion reads from the DB / the last tracked transition and, on failure,
raises with the AI-readable timeline (and optionally a snapshot) attached.
Mixed into ``ProcessScenario``; relies on the host providing ``state_field``,
``process_name``, ``_timeline``, ``_last_tracker``, ``_fail`` and ``_process``.
"""
from __future__ import annotations

from django_logic.testing.runner import latest_message, message_for


class ScenarioAssertions:
    # --- state -----------------------------------------------------------

    def assert_state(self, instance, expected):
        instance.refresh_from_db()
        actual = getattr(instance, self.state_field)
        if actual != expected:
            try:
                avail = self._process(instance).get_available_actions()
            except Exception:
                avail = '(unknown)'
            self._record_assert(f"assert_state({expected!r})", ok=False)
            self._fail(
                f'Expected state {expected!r}, but {self.state_field}={actual!r}.\n'
                f'  Available actions from {actual!r}: {avail}',
                instance=instance,
            )
        self._record_assert(f"assert_state({expected!r})", ok=True,
                            detail=f'{self.state_field}={actual!r}')

    # --- availability ----------------------------------------------------

    def _available(self, instance, user=None):
        return self._process(instance).get_available_actions(user=user)

    def assert_available(self, instance, actions, user=None):
        avail = self._available(instance, user=user)
        missing = [a for a in actions if a not in avail]
        if missing:
            self._record_assert(f'assert_available({actions})', ok=False)
            self._fail(
                f'Expected actions {missing} to be available'
                f'{" for user" if user is not None else ""}, '
                f'but available actions are {sorted(avail)}.',
                instance=instance,
            )
        self._record_assert(f'assert_available({actions})', ok=True)

    def assert_not_available(self, instance, actions, user=None):
        avail = self._available(instance, user=user)
        present = [a for a in actions if a in avail]
        if present:
            self._record_assert(f'assert_not_available({actions})', ok=False)
            self._fail(
                f'Expected actions {present} to be UNavailable'
                f'{" for user" if user is not None else ""}, '
                f'but they are available (all available: {sorted(avail)}).',
                instance=instance,
            )
        self._record_assert(f'assert_not_available({actions})', ok=True)

    # --- side-effect / callback tracking ---------------------------------

    def _tracker(self):
        if self._last_tracker is None:
            self.fail('No tracked transition yet — call transition() / '
                      'background_transition() before asserting side effects.')
        return self._last_tracker

    def assert_side_effects_ran(self, names):
        ran = self._tracker().side_effects_ran
        missing = [n for n in names if n not in ran]
        if missing:
            self._record_assert(f'assert_side_effects_ran({names})', ok=False)
            self._fail(f'Expected side-effects {missing} to have run; '
                       f'side-effects that ran: {ran}.')
        self._record_assert(f'assert_side_effects_ran({names})', ok=True)

    def assert_side_effects_not_ran(self, names):
        ran = self._tracker().side_effects_ran
        present = [n for n in names if n in ran]
        if present:
            self._record_assert(f'assert_side_effects_not_ran({names})', ok=False)
            self._fail(f'Expected side-effects {present} NOT to have run; '
                       f'side-effects that ran: {ran}.')
        self._record_assert(f'assert_side_effects_not_ran({names})', ok=True)

    def assert_callbacks_ran(self, names):
        ran = self._tracker().callbacks_ran
        missing = [n for n in names if n not in ran]
        if missing:
            self._record_assert(f'assert_callbacks_ran({names})', ok=False)
            self._fail(f'Expected callbacks {missing} to have run; '
                       f'callbacks that ran: {ran}.')
        self._record_assert(f'assert_callbacks_ran({names})', ok=True)

    def assert_failure_side_effects_ran(self, names):
        ran = self._tracker().failure_side_effects_ran
        missing = [n for n in names if n not in ran]
        if missing:
            self._record_assert(f'assert_failure_side_effects_ran({names})', ok=False)
            self._fail(f'Expected failure side-effects {missing} to have run; '
                       f'failure side-effects that ran: {ran}.')
        self._record_assert(f'assert_failure_side_effects_ran({names})', ok=True)

    def assert_failure_callbacks_ran(self, names):
        ran = self._tracker().failure_callbacks_ran
        missing = [n for n in names if n not in ran]
        if missing:
            self._record_assert(f'assert_failure_callbacks_ran({names})', ok=False)
            self._fail(f'Expected failure callbacks {missing} to have run; '
                       f'failure callbacks that ran: {ran}.')
        self._record_assert(f'assert_failure_callbacks_ran({names})', ok=True)

    # --- background error state ------------------------------------------

    def assert_error_recorded(self, instance, contains):
        tm = latest_message(instance)
        if tm is None or contains not in (tm.last_error_message or ''):
            self._record_assert(f'assert_error_recorded({contains!r})', ok=False)
            self._fail(
                f'Expected a recorded error containing {contains!r}, but '
                + ('no TransitionMessage exists for this instance.'
                   if tm is None else f'last_error={tm.last_error_message!r}.'),
                instance=instance,
            )
        self._record_assert(f'assert_error_recorded({contains!r})', ok=True)

    def assert_error_count(self, instance, expected):
        tm = latest_message(instance)
        actual = None if tm is None else tm.errors_count
        if actual != expected:
            self._record_assert(f'assert_error_count({expected})', ok=False)
            self._fail(
                f'Expected errors_count={expected}, but '
                + ('no TransitionMessage exists.' if tm is None else f'got {actual}.'),
                instance=instance,
            )
        self._record_assert(f'assert_error_count({expected})', ok=True)

    # --- caller-boundary exception (re-raise / swallow contract) ---------

    def assert_raised(self, exc_type=None, *, match=None):
        """Assert the last drive propagated an exception to the CALLER of the
        entrypoint — the SideEffects re-raise contract.

        ``exc_type`` optionally constrains the exception class (or tuple of
        classes); ``match`` optionally requires a substring of ``str(exc)``.
        Reads the exception the harness captured, so it pins that a failing
        side-effect surfaces to the caller — a swallow-vs-reraise regression
        (the 0.1.6->0.2.0 flip) makes this assertion fail.
        """
        raised = self._last_raised
        if raised is None:
            self._record_assert('assert_raised', ok=False, detail='nothing raised')
            self._fail(
                'Expected the drive to propagate an exception to the caller, '
                'but nothing was raised — a failure that must re-raise was '
                'swallowed, or no failure occurred.')
        if exc_type is not None and not isinstance(raised, exc_type):
            self._record_assert('assert_raised', ok=False,
                                detail=f'{type(raised).__name__}')
            self._fail(
                f'Expected the drive to raise {exc_type}, but it raised '
                f'{type(raised).__name__}: {raised}.')
        if match is not None and match not in str(raised):
            self._record_assert('assert_raised', ok=False,
                                detail=f'{raised!r} lacks {match!r}')
            self._fail(
                f'Expected the raised exception to contain {match!r}, but it '
                f'was {type(raised).__name__}: {raised}.')
        self._record_assert('assert_raised', ok=True,
                            detail=f'{type(raised).__name__}: {raised}')

    def assert_not_raised(self):
        """Assert the last drive did NOT propagate an exception to the caller
        — the swallow contract (Callbacks / NextTransition /
        FailureSideEffects). A regression that starts re-raising a best-effort
        failure makes this assertion fail."""
        raised = self._last_raised
        if raised is not None:
            self._record_assert('assert_not_raised', ok=False,
                                detail=f'{type(raised).__name__}: {raised}')
            self._fail(
                f'Expected the failure to be swallowed at the caller boundary, '
                f'but {type(raised).__name__} propagated to the caller: '
                f'{raised}.')
        self._record_assert('assert_not_raised', ok=True)

    # --- object journey --------------------------------------------------

    def assert_state_trace(self, expected):
        """Assert the ordered sequence of states the object passed through
        during the last drive (in_progress -> target, plus next_transition
        follow-ups and failed_state writes).

        This is the direct expression of *how the object changed as it went
        through the action* — e.g. a background chain drive yields
        ``['fulfilling', 'fulfilled', 'exporting', 'exported']``. Captured
        by ``track()`` wrapping ``State.set_state``.
        """
        trace = list(self._tracker().state_trace)
        if trace != expected:
            self._record_assert(f'assert_state_trace({expected})', ok=False,
                                detail=f'got {trace}')
            self._fail(
                f'Expected the object to pass through states {expected}, '
                f'but the recorded state trace is {trace}.',
                instance=None,
            )
        self._record_assert(f'assert_state_trace({expected})', ok=True,
                            detail=f'got {trace}')

    def assert_journey(self, expected_steps):
        """Assert the full ordered journey the object took across all drives
        in this test. Each ``JourneyStep`` pins one drive's observable
        transformation (action, before -> after, side-effects, callbacks,
        failed). One assertion locks the whole end-to-end behaviour."""
        from django_logic.testing.scenario import JourneyStep
        actual = self._journey
        if len(actual) != len(expected_steps):
            self._record_assert(f'assert_journey({len(expected_steps)} steps)',
                                ok=False, detail=f'got {len(actual)} steps')
            self._fail(
                f'Expected a journey of {len(expected_steps)} steps, but '
                f'the recorded journey has {len(actual)} steps.',
                instance=None,
            )
        for i, (exp, got) in enumerate(zip(expected_steps, actual), 1):
            if isinstance(exp, JourneyStep):
                if not exp.matches(got):
                    self._record_assert(f'assert_journey(step {i})', ok=False,
                                        detail=f'expected {exp}, got {got}')
                    self._fail(
                        f'Journey step {i} did not match.\n'
                        f'  expected: {exp}\n'
                        f'  got:      {got}',
                        instance=None,
                    )
            else:
                # Allow a plain dict for ergonomics.
                exp_step = JourneyStep(**exp)
                if not exp_step.matches(got):
                    self._record_assert(f'assert_journey(step {i})', ok=False,
                                        detail=f'expected {exp_step}, got {got}')
                    self._fail(
                        f'Journey step {i} did not match.\n'
                        f'  expected: {exp_step}\n'
                        f'  got:      {got}',
                        instance=None,
                    )
        self._record_assert(f'assert_journey({len(expected_steps)} steps)',
                            ok=True)

    # --- background owner (phase-2 restore discriminator) ---------------

    def assert_transition_owner(self, instance, owner, *, transition_name=None):
        """Assert the recorded ``owning_process_class`` on the instance's
        latest ``TransitionMessage`` (or the one named ``transition_name``
        when given). Pins the phase-2 owner discriminator — critical for
        chained/nested background transitions, where the follow-up must
        record its OWN owner, not the predecessor's.
        """
        if transition_name is not None:
            tm = message_for(instance, transition_name)
            label = f'assert_transition_owner({transition_name!r}, {owner!r})'
        else:
            tm = latest_message(instance)
            label = f'assert_transition_owner({owner!r})'
        actual = None if tm is None else tm.owning_process_class
        if actual != owner:
            self._record_assert(label, ok=False,
                                detail=f'got {actual!r}')
            if transition_name is None:
                where = 'the latest TransitionMessage'
            else:
                where = f'TransitionMessage for {transition_name!r}'
            suffix = '' if tm is not None else ' (no TransitionMessage exists.)'
            self._fail(
                f'Expected owning_process_class={owner!r} on {where}, '
                f'but got {actual!r}.{suffix}',
                instance=instance,
            )
        self._record_assert(label, ok=True, detail=f'got {actual!r}')
