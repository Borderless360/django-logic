"""``ProcessScenario`` — write FSM tests that read like a business story.

Subclass it, point it at your Process + model, and drive the workflow with
``transition`` / ``background_transition`` / ``retry_transition``, asserting
behaviour (state, availability, side-effects, recorded errors) instead of
poking framework internals. Background transitions run **inline, without
Celery**. On failure you get an AI-readable timeline (and, opt-in, a
reproducible snapshot).

    class TestOrderFulfilment(ProcessScenario):
        process_class = OrderProcess
        model = Order
        state_field = 'status'

        def test_happy_path(self):
            order = self.create_instance(status='approved')
            self.background_transition(order, 'fulfil')
            self.assert_state(order, 'fulfilled')
            self.assert_side_effects_ran(['reserve_stock', 'call_courier'])
"""
from __future__ import annotations

from django.test import TransactionTestCase

from django_logic.testing.assertions import ScenarioAssertions
from django_logic.testing.output import format_failure
from django_logic.testing.runner import (
    all_transitions,
    rerun_message,
    run_background_sync,
    transitions_for,
    uncompleted_message,
)
from django_logic.testing.runner import latest_message
from django_logic.testing.snapshot import from_snapshot as _from_snapshot
from django_logic.testing.snapshot import snapshot as _snapshot
from django_logic.testing.tracking import track


class ProcessScenario(ScenarioAssertions, TransactionTestCase):
    """Base class for scenario-based Process tests (no Celery required)."""

    process_class = None        # type[Process]
    model = None                # type[Model]
    state_field = 'status'
    process_name = 'process'
    snapshot_on_failure = False

    def setUp(self):
        super().setUp()
        self._timeline: list[dict] = []
        self._last_tracker = None

    # --- internals -------------------------------------------------------

    def _process(self, instance):
        return getattr(instance, self.process_name)

    def _record(self, label, outcome, detail=''):
        self._timeline.append({'label': label, 'outcome': outcome, 'detail': detail})

    def _record_assert(self, label, ok, detail=''):
        self._record(label, 'OK' if ok else 'FAILED', detail)

    def _snapshot_if_enabled(self, instance):
        if not self.snapshot_on_failure or instance is None:
            return None
        try:
            return _snapshot(instance, state_field=self.state_field,
                             process_name=self.process_name)
        except Exception:
            return None

    def _fail(self, message, instance=None):
        tm = latest_message(instance) if instance is not None else None
        self.fail(format_failure(message, self._timeline, tm=tm,
                                 snapshot=self._snapshot_if_enabled(instance)))

    def _state(self, instance):
        return getattr(instance, self.state_field)

    # --- instance creation ----------------------------------------------

    def create_instance(self, **kwargs):
        """Create a model instance (state via the ``state_field`` kwarg).
        Override for factories / related setup."""
        instance = self.model.objects.create(**kwargs)
        self._record('create_instance', 'OK',
                     f'{self.model.__name__}(pk={instance.pk}, '
                     f'{self.state_field}={self._state(instance)!r})')
        return instance

    def from_snapshot(self, data_or_path):
        """Rebuild an instance (and its TransitionMessage) from a snapshot."""
        instance = _from_snapshot(data_or_path, model=self.model)
        self._record('from_snapshot', 'OK',
                     f'{self.model.__name__}(pk={instance.pk}, '
                     f'{self.state_field}={self._state(instance)!r})')
        return instance

    def snapshot(self, instance):
        return _snapshot(instance, state_field=self.state_field,
                         process_name=self.process_name)

    # --- driving the process --------------------------------------------

    def transition(self, instance, action, *, fail_side_effect=None,
                   fail_with=None, **kwargs):
        """Run a synchronous transition through the normal process entrypoint."""
        return self._drive(instance, action, background=False,
                           fail_side_effect=fail_side_effect, fail_with=fail_with,
                           kwargs=kwargs)

    def background_transition(self, instance, action, *, fail_side_effect=None,
                             fail_with=None, **kwargs):
        """Run a BackgroundTransition's phase 1 + phase 2 inline (no Celery).

        ``fail_side_effect`` / ``fail_with`` make the named side-effect raise,
        exercising the real failure path; the injected exception is absorbed so
        you can assert on the recorded error."""
        return self._drive(instance, action, background=True,
                           fail_side_effect=fail_side_effect, fail_with=fail_with,
                           kwargs=kwargs)

    def retry_transition(self, instance, *, fail_side_effect=None, fail_with=None):
        """Re-run the instance's uncompleted transition inline — what the
        periodic starter would do."""
        tm = uncompleted_message(instance)
        if tm is None:
            self._record('retry_transition', 'FAILED', 'no uncompleted TransitionMessage')
            self._fail('retry_transition(): no uncompleted TransitionMessage for '
                       'this instance — nothing to retry.', instance=instance)
        if not transitions_for(self.process_class, tm.transition_name):
            self._record('retry_transition', 'FAILED',
                         f'no transition named {tm.transition_name!r}')
            self._fail(f'retry_transition(): the uncompleted TransitionMessage '
                       f'names {tm.transition_name!r}, which does not exist on '
                       f'{self.process_class.__name__}.', instance=instance)
        before = self._state(instance)
        # Track the WHOLE process tree, not just the retried action — the
        # retry can run follow-up transitions (next_transition, callbacks)
        # whose hooks the assertions must see (issue #96).
        with track(all_transitions(self.process_class),
                   fail_side_effect=fail_side_effect,
                   fail_with=fail_with) as tracker:
            raised = self._call(lambda: rerun_message(tm.pk))
        self._finish('retry_transition', instance, tracker, raised, before)
        return instance

    # --- shared execution path ------------------------------------------

    def _drive(self, instance, action, *, background, fail_side_effect, fail_with, kwargs):
        if not transitions_for(self.process_class, action):
            self._record(f'{"background_" if background else ""}transition({action!r})',
                         'FAILED', 'no such transition')
            self._fail(f'No transition named {action!r} on '
                       f'{self.process_class.__name__} (or its nested processes).',
                       instance=instance)
        before = self._state(instance)
        # Track the WHOLE process tree, not just the named action — one drive
        # can also execute next_transition follow-ups and callback-triggered
        # transitions, and their hooks must be visible to the side-effect
        # assertions (issue #96).
        with track(all_transitions(self.process_class),
                   fail_side_effect=fail_side_effect,
                   fail_with=fail_with) as tracker:
            if background:
                raised = self._call(
                    lambda: run_background_sync(instance, self.process_name, action, kwargs))
            else:
                raised = self._call(
                    lambda: getattr(self._process(instance), action)(**kwargs))
        label = f'{"background_" if background else ""}transition({action!r})'
        self._finish(label, instance, tracker, raised, before)
        return instance

    @staticmethod
    def _call(fn):
        try:
            fn()
            return None
        except Exception as exc:  # noqa: BLE001 — re-raised below unless injected
            return exc

    def _finish(self, label, instance, tracker, raised, before):
        self._last_tracker = tracker
        instance.refresh_from_db()
        after = self._state(instance)
        detail = f'{self.state_field}: {before} -> {after}'
        if tracker.side_effects_ran:
            detail += f'; ran={tracker.side_effects_ran}'
        if tracker.failed_side_effect:
            detail += f'; failed={tracker.failed_side_effect}'
        # An exception is expected only when it's the one we injected; anything
        # else is a real, unexpected failure and fails the test loudly.
        if raised is not None and raised is not tracker.injected_exception:
            self._record(label, 'FAILED', f'{type(raised).__name__}: {raised}')
            self._fail(f'{label} raised unexpectedly: '
                       f'{type(raised).__name__}: {raised}', instance=instance)
        # A requested injection that never fired silently turns a failure
        # test into a happy-path run (issue #94). track() already rejects
        # names that exist nowhere; this catches a hook that exists but did
        # not execute during this drive (e.g. wrong action, gated earlier).
        if (tracker.requested_fail_side_effect is not None
                and tracker.failed_side_effect is None and raised is None):
            self._record(label, 'FAILED',
                         f'fail_side_effect={tracker.requested_fail_side_effect!r} '
                         f'never fired')
            self._fail(
                f'{label}: fail_side_effect='
                f'{tracker.requested_fail_side_effect!r} never fired — no '
                f'side-effect with that name executed during this drive, so '
                f'the transition completed as a happy path instead of the '
                f'failure scenario this test intends.', instance=instance)
        self._record(label, 'OK' if raised is None else 'FAILED(injected)', detail)
