"""One set of assertions, run against all four transition shapes.

The recurring consumer bug is divergence: the same hook behaves
differently when its transition changes shape — synchronous or
background, with a target or without one. This module drives all four
shapes on the same model and states the intended differences, so a new
difference fails a test here.

Pinned for every shape:

* hook kwargs — the same values and the same Python types, plus a live ``user``;
* ``request`` is refused at the call, identically in all four shapes;
* every background drive serializes kwargs, so an unserializable kwarg fails at
  enqueue — inline sync-mode execution included;
* failure routing — a synchronous transition raises to the caller and writes
  failed_state at once; a background one absorbs at the caller and writes
  failed_state once the retries run out;
* callbacks observe the target state in all four classes;
* hook order — side-effects run while the persisted state is still the source,
  and callbacks run only after the target write is readable. On terminal
  failure, failed_state is written first and the failure callbacks second;
* ``next_transition`` — the same background follow-up runs with the same typed
  kwargs and a live ``user`` whether the parent is synchronous or background,
  with a target or without one, and never sees ``request``.
"""
from datetime import datetime, timezone as tz
from decimal import Decimal
from uuid import UUID

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from django_logic import Process, Transition
from django_logic.background import BackgroundTransition
from django_logic.exceptions import TransitionNotAllowed
from django_logic.process import ProcessManager
from tests.background.models import Widget
from tests import dl_settings

_SYNC_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=3)

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
                   failure_callbacks=[record_order_failure_callback]),
        Transition('sync_action', sources=['draft'],
               side_effects=[record_kwargs, record_order_side_effect],
               callbacks=[record_callback_state, record_order_callback]),
        BackgroundTransition('bg_transition', sources=['draft'], target='done',
                             failed_state='failed',
                             side_effects=[record_kwargs, record_order_side_effect],
                             callbacks=[record_callback_state, record_order_callback],
                             failure_callbacks=[record_order_failure_callback]),
        BackgroundTransition('bg_action', sources=['draft'],
                         side_effects=[record_kwargs, record_order_side_effect],
                         callbacks=[record_callback_state, record_order_callback]),
        # One chaining parent per shape, all into the same background
        # follow-up. The follow-up accepts 'draft' because no-target
        # parents change no state, and 'chained_src' because the
        # state-writing parents write it.
        Transition('sync_transition_chain', sources=['draft'],
                   target='chained_src', next_transition='chain_hop'),
        Transition('sync_action_chain', sources=['draft'],
               next_transition='chain_hop'),
        BackgroundTransition('bg_transition_chain', sources=['draft'],
                             target='chained_src', next_transition='chain_hop'),
        BackgroundTransition('bg_action_chain', sources=['draft'],
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

    def test_request_is_refused_at_the_call_in_all_four_shapes(self):
        # A transition never takes the request: hooks run on a worker for
        # a background transition, where no request exists, and the sync
        # shapes follow the same rule so a declaration can change shape
        # without changing its callers.
        sentinel = object()
        for action in SYNC_ACTIONS + BACKGROUND_ACTIONS:
            with self.assertRaisesMessage(TypeError, 'never takes the request'):
                _drive(self._fresh(), action, request=sentinel)
            self.assertNotIn('request', SEEN)

    def test_unserializable_kwarg_fails_at_enqueue_for_background_only(self):
        # Every background drive serializes kwargs, inline sync-mode execution
        # included. That is what lets a scenario test catch a serialization bug
        # at all. Synchronous transitions encode kwargs only for logging.
        from django.core.exceptions import ImproperlyConfigured

        class Blob:
            pass

        for action in BACKGROUND_ACTIONS:
            with self.assertRaises(ImproperlyConfigured, msg=action):
                _drive(self._fresh(), action, blob=Blob())

    def test_non_finite_float_kwarg_fails_at_enqueue_for_background_only(self):
        # Python's json.dumps writes NaN and Infinity, but neither is valid
        # JSON. Without the guard at enqueue the failure appears later at the
        # row write, and differs per database.
        from django.core.exceptions import ImproperlyConfigured
        from django_logic.background.models import TransitionMessage

        for action in BACKGROUND_ACTIONS:
            for bad in (float('nan'), float('inf'), float('-inf')):
                with self.assertRaises(
                        ImproperlyConfigured, msg=f'{action} {bad!r}'):
                    _drive(self._fresh(), action, rate=bad)
        # Enqueue failed before it wrote anything.
        self.assertFalse(TransitionMessage.objects.exists())

    def test_callbacks_observe_the_target_state_in_all_four_classes(self):
        expected = {'sync_transition': 'done', 'sync_action': 'draft',
                    'bg_transition': 'done', 'bg_action': 'draft'}
        for action in ALL_ACTIONS:
            CALLBACK_STATE.clear()
            _drive(self._fresh(), action)
            self.assertEqual(CALLBACK_STATE.get('status'), expected[action], action)

    def test_side_effects_precede_the_target_write_and_callbacks_follow_it(self):
        # The test above pins which state callbacks observe; this one pins the
        # order around the state write. The side-effect runs while the
        # persisted state is still the source, and the callback runs only after
        # the target write is readable. Actions differ only in the state the
        # callback sees, because they never write one.
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
        # failed_state is written first, so the failure callbacks observe the
        # contained state. The synchronous path, the worker's terminal attempt
        # and the watchdog finalizer must all agree on that order.
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
                # The synchronous path runs the terminal sequence on its only
                # attempt; the background one runs it at MAX_ERRORS.
                row = TransitionMessage.objects.get(instance_id=str(widget.pk),
                                                    transition_name=action)
                for _ in range(2):
                    with self.assertRaises(ValueError):
                        run_background_transition(row.pk)
            widget.refresh_from_db()
            self.assertEqual(widget.status, 'failed', action)
            results[action] = list(ORDER)

        for action, order in results.items():
            self.assertEqual(order, [('failure_callback', 'failed')], action)
        FAIL['on'] = False

    def test_next_transition_chains_equivalently_across_all_four_shapes(self):
        # The parent's shape must not change what the follow-up receives:
        # the same typed kwargs and a live user. Every shape chains — a
        # transition with no target included.
        for parent in CHAIN_PARENTS:
            HOP_SEEN.clear()
            widget = self._fresh()
            kwargs = dict(TYPED_KWARGS)
            _drive(widget, parent, user=self.user, **kwargs)
            widget.refresh_from_db()
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
        # In sync mode the worker step runs inline and propagates the
        # exception; celery mode absorbs it at the caller. The durable contract
        # is the same either way: each attempt's writes roll back, the row
        # counts the error, and running out of retries writes failed_state.
        from django_logic.background.models import TransitionMessage
        from django_logic.background.runner import run_background_transition

        FAIL['on'] = True
        widget = self._fresh()
        with self.assertRaises(ValueError):
            _drive(widget, 'bg_transition')
        widget.refresh_from_db()
        self.assertNotEqual(widget.status, 'done')
        row = TransitionMessage.objects.get(instance_id=str(widget.pk),
                                            transition_name='bg_transition')
        self.assertEqual(row.errors_count, 1)

        # Run the remaining attempts as the worker would. A row becomes
        # claimable again once RETRY_MINUTES have passed.
        for _ in range(2):
            try:
                run_background_transition(row.pk)
            except ValueError:
                pass
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'failed')
        FAIL['on'] = False
