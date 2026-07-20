"""Sync/background parity contract matrix (#111).

The recurring consumer-bug class is divergence: the same hook behaving
differently when its transition flips between Transition / Action /
BackgroundTransition / BackgroundAction. This module runs ONE set of
behavioral assertions against all four transition classes on the same
model, and declares the intended differences explicitly — so any NEW
asymmetry fails a test by construction.

Contracts pinned per class:
* hook kwargs — identical values AND Python types (the #108 typed
  round-trip) plus a live ``user``;
* ``request`` — reaches sync hooks, never background hooks (dropped at
  phase-1 serialization: the one deliberate asymmetry);
* serialization is IN THE LOOP for every background drive — an
  unserializable kwarg fails at phase 1, which is also the realism pin
  for the testing framework's inline background execution;
* failure routing — sync raises to the caller and routes failed_state
  immediately; background absorbs at the caller and routes after
  retries are exhausted;
* callbacks observe the target state in all four classes;
* hook ordering — side-effects run while the persisted state is still
  the source, callbacks only after the target write is observable; on
  terminal failure, failed_state is written first, then
  failure_side_effects, then failure_callbacks — the same sequence
  sync and background;
* ``next_transition`` — the same background follow-up runs with the
  same typed kwargs and a live ``user`` whether the parent is sync or
  background, and never sees ``request``; the declared difference: a
  sync ``Action`` never chains, a ``BackgroundAction`` does.
"""
from datetime import datetime, timezone as tz
from decimal import Decimal
from uuid import UUID

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from django_logic import Action, Process, Transition
from django_logic.background import BackgroundAction, BackgroundTransition
from django_logic.exceptions import TransitionNotAllowed
from django_logic.process import ProcessManager
from tests.background.models import Widget

_SYNC_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 3,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}

TYPED_KWARGS = dict(
    when=datetime(2026, 6, 4, 12, 30, 0, tzinfo=tz.utc),
    amount=Decimal('19.99'),
    some_id=UUID('12345678-1234-5678-1234-567812345678'),
    pair=(1, 'two'),
    tags={'a', 'b'},
    flag=True,
    note='x',
)
_ENGINE_KEYS = {'tr_id', 'root_id', 'parent_id', 'context', 'user', 'process_class'}

SEEN: dict = {}
CALLBACK_STATE: dict = {}
FAIL = {'on': False}
ORDER: list = []
HOP_SEEN: dict = {}


def record_kwargs(instance, **kwargs):
    if FAIL['on']:
        raise ValueError('injected parity failure')
    SEEN.clear()
    SEEN.update(kwargs)


def record_callback_state(instance, **kwargs):
    instance.refresh_from_db()
    CALLBACK_STATE['status'] = instance.status


def record_order_side_effect(instance, **kwargs):
    instance.refresh_from_db()
    ORDER.append(('side_effect', instance.status))


def record_order_callback(instance, **kwargs):
    instance.refresh_from_db()
    ORDER.append(('callback', instance.status))


def record_order_failure_side_effect(instance, **kwargs):
    instance.refresh_from_db()
    ORDER.append(('failure_side_effect', instance.status))


def record_order_failure_callback(instance, **kwargs):
    instance.refresh_from_db()
    ORDER.append(('failure_callback', instance.status))


def record_hop_kwargs(instance, **kwargs):
    HOP_SEEN.clear()
    HOP_SEEN.update(kwargs)


class ParityProcess(Process):
    process_name = 'parity_process'
    transitions = [
        Transition('sync_transition', sources=['draft'], target='done',
                   failed_state='failed',
                   side_effects=[record_kwargs, record_order_side_effect],
                   callbacks=[record_callback_state, record_order_callback],
                   failure_side_effects=[record_order_failure_side_effect],
                   failure_callbacks=[record_order_failure_callback]),
        Action('sync_action', sources=['draft'],
               side_effects=[record_kwargs, record_order_side_effect],
               callbacks=[record_callback_state, record_order_callback]),
        BackgroundTransition('bg_transition', sources=['draft'], target='done',
                             failed_state='failed',
                             side_effects=[record_kwargs, record_order_side_effect],
                             callbacks=[record_callback_state, record_order_callback],
                             failure_side_effects=[record_order_failure_side_effect],
                             failure_callbacks=[record_order_failure_callback]),
        BackgroundAction('bg_action', sources=['draft'],
                         side_effects=[record_kwargs, record_order_side_effect],
                         callbacks=[record_callback_state, record_order_callback]),
        # next_transition parity: one parent per class, every one chaining
        # into the same background follow-up. The hop accepts both 'draft'
        # (Action parents change no state) and 'chained_src' (Transition
        # parents' target).
        Transition('sync_transition_chain', sources=['draft'],
                   target='chained_src', next_transition='chain_hop'),
        Action('sync_action_chain', sources=['draft'],
               next_transition='chain_hop'),
        BackgroundTransition('bg_transition_chain', sources=['draft'],
                             target='chained_src', next_transition='chain_hop'),
        BackgroundAction('bg_action_chain', sources=['draft'],
                         next_transition='chain_hop'),
        BackgroundTransition('chain_hop', sources=['draft', 'chained_src'],
                             target='chained', side_effects=[record_hop_kwargs]),
    ]


ALL_ACTIONS = ('sync_transition', 'sync_action', 'bg_transition', 'bg_action')
BACKGROUND_ACTIONS = ('bg_transition', 'bg_action')
SYNC_ACTIONS = ('sync_transition', 'sync_action')
CHAIN_PARENTS = ('sync_transition_chain', 'sync_action_chain',
                 'bg_transition_chain', 'bg_action_chain')


def _drive(widget, action, **kwargs):
    getattr(widget.parity_process, action)(**kwargs)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class ParityMatrixTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ProcessManager.bind_model_process(Widget, ParityProcess, state_field='status')

    @classmethod
    def tearDownClass(cls):
        if 'parity_process' in vars(Widget):
            delattr(Widget, 'parity_process')
        super().tearDownClass()

    def setUp(self):
        SEEN.clear()
        CALLBACK_STATE.clear()
        ORDER.clear()
        HOP_SEEN.clear()
        FAIL['on'] = False
        self.user = get_user_model().objects.create(username='parity-actor')

    def _fresh(self):
        return Widget.objects.create(status='draft')

    def _hook_kwargs(self):
        return {k: v for k, v in SEEN.items() if k not in _ENGINE_KEYS}

    def test_hook_kwargs_identical_across_all_four_classes(self):
        results = {}
        for action in ALL_ACTIONS:
            _drive(self._fresh(), action, user=self.user, **dict(TYPED_KWARGS))
            results[action] = self._hook_kwargs()
            self.assertEqual(SEEN['user'].pk, self.user.pk, action)

        for action in ALL_ACTIONS:
            self.assertEqual(results[action], TYPED_KWARGS, action)
            for key, value in TYPED_KWARGS.items():
                self.assertIs(type(results[action][key]), type(value), f'{action}.{key}')

    def test_request_reaches_sync_hooks_and_never_background_hooks(self):
        # The one deliberate asymmetry: a live request cannot cross the
        # durable phase boundary.
        sentinel = object()
        for action in SYNC_ACTIONS:
            _drive(self._fresh(), action, request=sentinel)
            self.assertIs(SEEN.get('request'), sentinel, action)
        for action in BACKGROUND_ACTIONS:
            _drive(self._fresh(), action, request=sentinel)
            self.assertNotIn('request', SEEN, action)

    def test_unserializable_kwarg_fails_at_phase1_for_background_only(self):
        # Serialization is in the loop for every background drive — including
        # inline/sync-mode execution, which is what makes downstream scenario
        # tests able to catch serialization bugs at all (the realism pin).
        # (Sync transitions json-encode kwargs only for logging, so the
        # functional contract is asserted for the background classes.)
        from django.core.exceptions import ImproperlyConfigured

        class Blob:
            pass

        for action in BACKGROUND_ACTIONS:
            with self.assertRaises(ImproperlyConfigured, msg=action):
                _drive(self._fresh(), action, blob=Blob())

    def test_non_finite_float_kwarg_fails_at_phase1_for_background_only(self):
        # NaN/Infinity pass Python's json.dumps (non-standard tokens) but
        # are not valid JSON — without the phase-1 guard the failure
        # surfaces backend-dependently at the row write (issue #118). Same
        # dispatcher contract as an unserializable kwarg above.
        from django.core.exceptions import ImproperlyConfigured
        from django_logic.background.models import TransitionMessage

        for action in BACKGROUND_ACTIONS:
            for bad in (float('nan'), float('inf'), float('-inf')):
                with self.assertRaises(
                        ImproperlyConfigured, msg=f'{action} {bad!r}'):
                    _drive(self._fresh(), action, rate=bad)
        # Phase 1 failed before persisting anything.
        self.assertFalse(TransitionMessage.objects.exists())

    def test_callbacks_observe_the_target_state_in_all_four_classes(self):
        expected = {'sync_transition': 'done', 'sync_action': 'draft',
                    'bg_transition': 'done', 'bg_action': 'draft'}
        for action in ALL_ACTIONS:
            CALLBACK_STATE.clear()
            _drive(self._fresh(), action)
            self.assertEqual(CALLBACK_STATE.get('status'), expected[action], action)

    def test_side_effects_precede_the_target_write_and_callbacks_follow_it(self):
        # The callback-state test above pins WHICH state callbacks observe;
        # this pins the ORDER around the state write: the side-effect runs
        # while the persisted state is still the SOURCE (this process
        # declares no in_progress_state; when one is declared, the sync
        # path and background phase 1 both write it before side-effects
        # run, so the pre-target contract stays symmetric), and the
        # callback runs only after the target write is observable. Actions
        # differ only in the state the callback sees — they never write one.
        expected = {'sync_transition': 'done', 'sync_action': 'draft',
                    'bg_transition': 'done', 'bg_action': 'draft'}
        for action in ALL_ACTIONS:
            ORDER.clear()
            _drive(self._fresh(), action)
            self.assertEqual(
                ORDER,
                [('side_effect', 'draft'), ('callback', expected[action])],
                action,
            )

    def test_terminal_failure_order_is_identical_for_both_failure_capable_classes(self):
        # failed_state is written FIRST, so both failure hooks observe the
        # contained state; failure_side_effects run before failure_callbacks.
        # Every implementation site agrees on this sequence — the sync path
        # (Transition.fail_transition), the background terminal attempt
        # (runner._handle_failure), and the watchdog finalizer that mirrors
        # it — and that cross-class symmetry is the contract pinned here.
        # (Transition's class docstring lists failure_side_effects before
        # the failed_state write; the code does not, in any class.)
        from django_logic.background.models import TransitionMessage
        from django_logic.background.runner import run_background_transition

        FAIL['on'] = True
        results = {}
        for action in ('sync_transition', 'bg_transition'):
            ORDER.clear()
            widget = self._fresh()
            with self.assertRaises(ValueError):
                _drive(widget, action)
            if action == 'bg_transition':
                # sync runs the terminal sequence on its only attempt; the
                # background one runs at MAX_ERRORS — the retry-timing
                # difference already pinned by the failure-routing tests.
                tm = TransitionMessage.objects.get(instance_id=str(widget.pk),
                                                   transition_name=action)
                for _ in range(2):
                    with self.assertRaises(ValueError):
                        run_background_transition(tm.pk)
            widget.refresh_from_db()
            self.assertEqual(widget.status, 'failed', action)
            results[action] = list(ORDER)

        for action, order in results.items():
            self.assertEqual(order, [('failure_side_effect', 'failed'),
                                     ('failure_callback', 'failed')], action)
        FAIL['on'] = False

    def test_next_transition_chains_equivalently_across_all_four_classes(self):
        # The parent's class must not change WHAT the follow-up receives:
        # the same background follow-up runs with the same typed kwargs and
        # a live user, and request never reaches it — stripped by
        # NextTransition for a sync parent (#129), dropped at the parent's
        # own phase 1 for a background parent. The one declared difference:
        # a sync Action never runs next_transition (see the divergence note
        # on Action), while a BackgroundAction's phase 2 does.
        chains = {'sync_transition_chain': True, 'sync_action_chain': False,
                  'bg_transition_chain': True, 'bg_action_chain': True}
        for parent in CHAIN_PARENTS:
            HOP_SEEN.clear()
            widget = self._fresh()
            _drive(widget, parent, user=self.user, request=object(),
                   **dict(TYPED_KWARGS))
            widget.refresh_from_db()
            if not chains[parent]:
                self.assertEqual(HOP_SEEN, {}, parent)
                self.assertEqual(widget.status, 'draft', parent)
                continue
            self.assertEqual(widget.status, 'chained', parent)
            hop_kwargs = {k: v for k, v in HOP_SEEN.items()
                          if k not in _ENGINE_KEYS}
            self.assertEqual(hop_kwargs, TYPED_KWARGS, parent)
            for key, value in TYPED_KWARGS.items():
                self.assertIs(type(hop_kwargs[key]), type(value),
                              f'{parent}.{key}')
            self.assertEqual(HOP_SEEN['user'].pk, self.user.pk, parent)
            self.assertNotIn('request', HOP_SEEN, parent)

    def test_sync_failure_raises_and_routes_failed_state_immediately(self):
        FAIL['on'] = True
        widget = self._fresh()
        with self.assertRaises(ValueError):
            _drive(widget, 'sync_transition')
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'failed')

    def test_background_failure_rolls_back_and_routes_after_retries(self):
        # In sync execution mode phase 2 runs inline and PROPAGATES the
        # exception (celery mode absorbs it at the caller) — but the durable
        # contract is identical: each attempt's writes roll back, the
        # TransitionMessage counts the error, and exhaustion routes
        # failed_state.
        from django_logic.background.models import TransitionMessage
        from django_logic.background.runner import run_background_transition

        FAIL['on'] = True
        widget = self._fresh()
        with self.assertRaises(ValueError):
            _drive(widget, 'bg_transition')
        widget.refresh_from_db()
        self.assertNotEqual(widget.status, 'done')
        tm = TransitionMessage.objects.get(instance_id=str(widget.pk),
                                           transition_name='bg_transition')
        self.assertEqual(tm.errors_count, 1)

        # drive the remaining attempts as the worker would (the periodic
        # starter only picks rows up once RETRY_MINUTES have elapsed)
        for _ in range(2):
            try:
                run_background_transition(tm.pk)
            except ValueError:
                pass
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'failed')
        FAIL['on'] = False
