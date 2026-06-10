"""Business-level assertions for :class:`ProcessScenario`.

Each assertion reads from the DB / the last tracked transition and, on failure,
raises with the AI-readable timeline (and optionally a snapshot) attached.
Mixed into ``ProcessScenario``; relies on the host providing ``state_field``,
``process_name``, ``_timeline``, ``_last_tracker``, ``_fail`` and ``_process``.
"""
from __future__ import annotations

from django_logic.testing.runner import latest_message


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
