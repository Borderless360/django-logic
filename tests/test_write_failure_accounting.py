"""Failure accounting on the write path.

A failing state write counts as an error, rolls back its own attempt,
and never lies about what it wrote. The attempt-start stamp survives a
crashed attempt. Each pin was written after a consumer hit the defect.
"""
from django.core.exceptions import ImproperlyConfigured
from django.core.cache import cache
from django.db.models.signals import pre_save
from django.test import TestCase, override_settings

from django_logic import Process, ProcessManager, Transition
from django_logic.background import BackgroundTransition
from django_logic.background.models import TransitionMessage
from django_logic.background.safety_nets import retry_pending
from django_logic.logger import TransitionEventType
from tests.models import Invoice, MtiChild, MtiParent


def _noop(instance, **kwargs):
    pass


class _BindCleanup:
    """Unbind whatever the test bound, so the global registry stays clean."""

    _bound: tuple = ()

    def tearDown(self):
        ProcessManager.bindings = [
            b for b in ProcessManager.bindings
            if b.process_class not in self._bound
        ]
        for proc in self._bound:
            if proc.process_name in vars(Invoice):
                delattr(Invoice, proc.process_name)
        super().tearDown()


# --- A failing state write must count as an error ------------------------

def _write_a_sibling_row(instance, **kwargs):
    """A side-effect that writes a row, so the rollback of the attempt
    savepoint can be asserted. A ``_noop`` side-effect leaves nothing to roll
    back, which made this pin prove nothing."""
    Invoice.objects.create(status='sibling')


class RejectedTargetWriteProcess(Process):
    process_name = 'rejected_target_proc'
    transitions = [
        BackgroundTransition(
            'go', sources=['draft'], target='rejected_target',
            in_progress_state='rt_running', failed_state='rt_failed',
            side_effects=[_write_a_sibling_row],
        ),
    ]


class RejectedStateWriteTests(_BindCleanup, TestCase):
    """A state write the database refuses used to escape the outer atomic block
    and roll back record_error with it. errors_count stayed 0, so the starter
    sent the row to the queue again and the side-effects re-ran forever."""

    _bound = (RejectedTargetWriteProcess,)

    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, RejectedTargetWriteProcess, state_field='status')
        cache.clear()

        def veto(sender, instance, **kwargs):
            if instance.status == 'rejected_target':
                raise ValueError('the database refuses this state')

        self._veto = veto
        pre_save.connect(veto, sender=Invoice)
        self.addCleanup(pre_save.disconnect, veto, sender=Invoice)

    @override_settings(DJANGO_LOGIC={
        'BACKGROUND_EXECUTION': 'sync',
        'TRANSITION_MESSAGE_MAX_ERRORS': 3,
        'TRANSITION_MESSAGE_RETRY_MINUTES': 0,
    })
    def test_rejected_target_write_counts_an_error_and_terminates(self):
        inv = Invoice.objects.create(status='draft')
        with self.assertRaises(ValueError):
            inv.rejected_target_proc.go()

        row = TransitionMessage.objects.get(instance_id=str(inv.pk))
        # The attempt counted as an error. It used to stay at 0.
        self.assertEqual(row.errors_count, 1)
        self.assertFalse(row.is_completed)

        # And the retry loop terminates instead of running forever.
        for _ in range(5):
            try:
                retry_pending()
            except ValueError:
                pass
        row.refresh_from_db()
        self.assertTrue(row.is_completed)
        self.assertEqual(row.errors_count, 3)

    @override_settings(DJANGO_LOGIC={
        'BACKGROUND_EXECUTION': 'sync',
        'TRANSITION_MESSAGE_MAX_ERRORS': 3,
        'TRANSITION_MESSAGE_RETRY_MINUTES': 0,
    })
    def test_target_write_rolls_back_its_own_attempt(self):
        """The target write lives inside the attempt savepoint, so a rejected
        write leaves nothing behind.

        The side-effect's own row is what proves it. Asserting on the instance's
        state proved nothing, because the veto blocks the target write whether
        or not a savepoint contains it.
        """
        inv = Invoice.objects.create(status='draft')
        with self.assertRaises(ValueError):
            inv.rejected_target_proc.go()
        inv.refresh_from_db()
        # Never the target; still the in_progress_state that enqueue wrote.
        self.assertEqual(inv.status, 'rt_running')
        # The attempt was all-or-nothing: the side-effect's write is gone.
        self.assertFalse(
            Invoice.objects.filter(status='sibling').exists(),
            'the failed attempt left a side-effect write behind, so the target '
            'write is not inside the attempt savepoint',
        )


# --- The attempt-start stamp survives a crashed attempt ---------------------

def _raise_slow(instance, **kwargs):
    raise ValueError('slow boom')


def _die(instance, **kwargs):
    raise SystemExit('worker killed mid-attempt')


class StampSurvivesCrashProcess(Process):
    process_name = 'wd_crash_proc'
    transitions = [
        BackgroundTransition(
            'go', sources=['draft'], target='wd2_done',
            in_progress_state='wd2_running', failed_state='wd2_failed',
            side_effects=[_die],
        ),
    ]


@override_settings(DJANGO_LOGIC={
    'BACKGROUND_EXECUTION': 'sync',
    'TRANSITION_MESSAGE_MAX_ERRORS': 5,
})
class AttemptStampDurabilityTests(_BindCleanup, TestCase):
    _bound = (StampSurvivesCrashProcess,)

    def setUp(self):
        super().setUp()
        cache.clear()

    def test_attempt_stamp_survives_the_attempt_rolling_back(self):
        """started_at is committed before the attempt, so a worker that dies
        part way through stays visible to the stuck report and the retry
        classification. It used to vanish with the transaction."""
        ProcessManager.bind_model_process(
            Invoice, StampSurvivesCrashProcess, state_field='status')
        inv = Invoice.objects.create(status='draft')
        with self.assertRaises(SystemExit):
            inv.wd_crash_proc.go()

        row = TransitionMessage.objects.get(instance_id=str(inv.pk))
        self.assertIsNotNone(row.started_at)   # survived the rollback
        self.assertEqual(row.errors_count, 0)  # the attempt recorded nothing


# --- self-review of the 0.12.0 fixes themselves ---------------------------

class RejectedFailedStateWriteProcess(Process):
    """A transition whose failed_state the database refuses."""
    process_name = 'rej_failed_proc'
    transitions = [
        Transition('go', sources=['draft'], target='rf_done',
                   failed_state='rf_refused',
                   side_effects=[_raise_slow]),
    ]


class FailedStateWriteHonestyTests(_BindCleanup, TestCase):
    """The savepoints added for #178 must not lie about what they wrote."""

    _bound = (RejectedFailedStateWriteProcess,)

    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, RejectedFailedStateWriteProcess, state_field='status')
        cache.clear()

        def veto(sender, instance, **kwargs):
            if instance.status == 'rf_refused':
                raise ValueError('the database refuses failed_state')

        pre_save.connect(veto, sender=Invoice)
        self.addCleanup(pre_save.disconnect, veto, sender=Invoice)

    def test_a_rejected_failed_state_write_does_not_log_set_state(self):
        """The SET_STATE line is the state-change record the trace and
        log-based assertions read; emitting it for a write that never landed
        would be a false entry. (Caught reviewing 0.12.0's own diff.)"""
        inv = Invoice.objects.create(status='draft')
        with self.assertLogs('django-logic', level='INFO') as logs:
            with self.assertRaises(ValueError) as ctx:
                inv.rej_failed_proc.go()

        # The ORIGINAL failure propagates, not the write's own exception.
        self.assertEqual(str(ctx.exception), 'slow boom')
        # TransitionEventType.SET_STATE.value, not a hand-typed 'Set state':
        # the engine logs 'Set State', so the lowercase literal matched
        # nothing and this assertion passed against an empty list however
        # false the log was (caught by Cursor Bugbot on this very PR — the
        # same "test that proves nothing" shape as the fake MTI test).
        set_state_lines = [
            line for line in logs.output
            if 'rf_refused' in line and TransitionEventType.SET_STATE.value in line
        ]
        self.assertEqual(set_state_lines, [], f'false SET_STATE: {set_state_lines}')
        # And the failure was reported.
        self.assertTrue(any('could not write failed_state' in line
                            for line in logs.output))


class FailureErrorAccumulationTests(TestCase):
    """record_failure_side_effect_error must not erase an earlier note.

    Overwriting meant whichever note came second silently erased the
    other. (Caught reviewing 0.12.0's own diff.)
    """

    def test_two_recorded_problems_both_survive(self):
        transition_message = TransitionMessage.objects.create(
            app_label='tests', model_name='invoice', instance_id='1',
            process_name='acc_proc', transition_name='go', queue_name='q')

        transition_message.record_failure_side_effect_error(
            ValueError('write refused'), label='failed_state write')
        transition_message.record_failure_side_effect_error(
            RuntimeError('cleanup broke'), label='failed_state write')

        transition_message.refresh_from_db()
        self.assertIn('failed_state write: ValueError: write refused',
                      transition_message.failure_side_effect_error)
        self.assertIn('failed_state write: RuntimeError: cleanup broke',
                      transition_message.failure_side_effect_error)


# (StrandedRecoveryHonestyTests retired in 0.12.0 with recover_stranded_states
# itself — in_progress_state is background-only now, so no record-less
# stranding exists for a sweep to recover or misreport.)


class ShadowValidatorInstanceAttrTests(TestCase):
    def test_an_action_named_state_is_rejected(self):
        """`state` is set on the INSTANCE by Process.__init__, so
        hasattr(cls, 'state') is False and the class-only check accepted it —
        while at runtime the transition was unreachable. It is also the first
        example the validator's own docstring cites."""
        for name in ('state', 'instance', 'field_name'):
            with self.subTest(action_name=name):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    type(f'Shadow_{name}', (Process,), {
                        'process_name': f'shadow_{name}_proc',
                        'transitions': [
                            Transition(name, sources=['draft'], target='approved'),
                        ],
                    })
                self.assertIn(name, str(ctx.exception))


class MtiBindingTests(TestCase):
    """A multi-table-inheritance child may bind the same process_name as its
    parent: setattr installs the child's OWN accessor, shadowing the parent's,
    and each model drives its own process. The MRO collision check rejected
    that working shape until it learned to ignore accessors django-logic
    itself installed.

    Caught reviewing the regression-fix commits — and the first version of
    this test was a fake: it asserted an isinstance() on a NON-MTI model and
    passed with the fix removed. It now binds a real parent/child pair and
    drives the child, so removing the fix fails it.
    """

    _procs: tuple = ()

    def tearDown(self):
        ProcessManager.bindings = [
            b for b in ProcessManager.bindings if b.process_class not in self._procs
        ]
        for model in (MtiParent, MtiChild):
            for proc in self._procs:
                if proc.process_name in vars(model):
                    delattr(model, proc.process_name)
        super().tearDown()

    def test_child_may_reuse_the_parents_process_name(self):
        class ParentFlow(Process):
            process_name = 'mti_flow'
            transitions = [
                Transition('go', sources=['draft'], target='parent_done'),
            ]

        class ChildFlow(Process):
            process_name = 'mti_flow'          # same name, MTI child
            transitions = [
                Transition('go', sources=['draft'], target='child_done'),
            ]

        self._procs = (ParentFlow, ChildFlow)
        ProcessManager.bind_model_process(
            MtiParent, ParentFlow, state_field='status')
        # This is the call that raised before the fix.
        ProcessManager.bind_model_process(
            MtiChild, ChildFlow, state_field='status')

        cache.clear()
        child = MtiChild.objects.create(status='draft')
        child.mti_flow.go()
        child.refresh_from_db()
        # The child ran ITS process, not the parent's.
        self.assertEqual(child.status, 'child_done')

        parent = MtiParent.objects.create(status='draft')
        parent.mti_flow.go()
        parent.refresh_from_db()
        self.assertEqual(parent.status, 'parent_done')

    def test_a_real_attribute_clash_is_still_rejected(self):
        class Clashing(Process):
            process_name = 'save'          # Model.save lives on the MRO
            transitions = [
                Transition('go', sources=['draft'], target='approved'),
            ]

        self._procs = (Clashing,)
        with self.assertRaises(ImproperlyConfigured) as ctx:
            ProcessManager.bind_model_process(
                Invoice, Clashing, state_field='status')
        self.assertIn('already names something', str(ctx.exception))

    def test_a_model_method_named_like_the_process_is_rejected(self):
        class HasMethod(Process):
            process_name = 'clashing_method'
            transitions = [
                Transition('go', sources=['draft'], target='approved'),
            ]

        self._procs = (HasMethod,)
        Invoice.clashing_method = lambda self: 'business logic'
        self.addCleanup(lambda: delattr(Invoice, 'clashing_method'))
        with self.assertRaises(ImproperlyConfigured) as ctx:
            ProcessManager.bind_model_process(
                Invoice, HasMethod, state_field='status')
        self.assertIn('already names something', str(ctx.exception))
