"""End-to-end kwargs serialization round-trip across the phase-1/phase-2
boundary.

These pin the *contract* of what a background side-effect actually receives
in phase 2 — the single biggest previously-untested gap, since the whole
point of django_logic.background.serializers is this boundary. Runs in sync
mode (the default) so phase 1 + phase 2 execute inline.
"""
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from django_logic.background.models import TransitionMessage
from django_logic.background.runner import run_background_transition
from django_logic.background.serializers import KwargsSerializationError
from tests.background import models as bg_models
from tests.background.models import Widget


_SYNC_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 3,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}

_UUID = UUID('12345678-1234-5678-1234-567812345678')
_WHEN = datetime(2026, 6, 4, 12, 30, 0)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class KwargsRoundTripTests(TestCase):
    def setUp(self):
        bg_models.LAST_KWARGS.clear()
        self.widget = Widget.objects.create()
        self.user = get_user_model().objects.create(username='actor')

    def test_phase2_side_effect_receives_restored_user_and_context(self):
        self.widget.process.fulfil(
            user=self.user,
            request=object(),          # must be dropped
            when=_WHEN,                 # datetime -> ISO str
            some_uuid=_UUID,            # UUID -> str
        )
        seen = bg_models.LAST_KWARGS

        # user_id is rehydrated back to a live User object in phase 2.
        self.assertIn('user', seen)
        self.assertIsInstance(seen['user'], get_user_model())
        self.assertEqual(seen['user'].pk, self.user.pk)
        self.assertNotIn('user_id', seen)

        # request is dropped at phase 1 and never reaches phase 2.
        self.assertNotIn('request', seen)

        # context is rebuilt in phase 2 (mirrors the synchronous path) so a
        # side-effect declared as fn(instance, context, **kwargs) works in
        # both modes.
        self.assertIn('context', seen)
        self.assertEqual(seen['context'], {})

        # owning_process_class is engine bookkeeping persisted on the
        # TransitionMessage column (issue #98), NOT caller data — it is popped
        # by serialize_kwargs and must never reach side-effect kwargs. Pinning
        # this guards sync/background parity: a side-effect declared as
        # fn(instance, owning_process_class, **kwargs) must not start behaving
        # differently in the background path. It IS recorded on the column.
        self.assertNotIn('owning_process_class', seen)
        tm = TransitionMessage.objects.get(transition_name='fulfil')
        self.assertEqual(
            tm.owning_process_class, 'tests.background.models.WidgetProcess'
        )

    @override_settings(
        DJANGO_LOGIC={**_SYNC_SETTINGS, 'STRICT_KWARGS_SERIALIZATION': True})
    def test_strict_request_drop_raises_typeerror_through_real_dispatch(self):
        # The contract consumers actually see: the strict-mode rejection
        # reaches the caller as the documented TypeError (not wrapped into
        # the dispatcher's "not JSON-serializable" ImproperlyConfigured).
        with self.assertRaisesMessage(
                KwargsSerializationError, "'request' dropped"):
            self.widget.process.fulfil(request=object())
        # Phase 1 failed before persisting anything.
        self.assertFalse(TransitionMessage.objects.exists())

    def test_owning_process_class_kept_out_of_nested_side_effect_kwargs(self):
        # Same contract for a NESTED owner: nested_fulfil is declared on
        # NestedBgChildProcess and reached through the bound parent_process.
        # The discriminator (the nested class) lands on the column, never in
        # the side-effect kwargs. nested_fulfil's side-effects include
        # bg_record_kwargs.
        self.widget.parent_process.nested_fulfil()
        seen = bg_models.LAST_KWARGS
        self.assertNotIn('owning_process_class', seen)
        tm = TransitionMessage.objects.get(transition_name='nested_fulfil')
        self.assertEqual(
            tm.owning_process_class,
            'tests.background.models.NestedBgChildProcess',
        )

    def test_typed_kwargs_arrive_with_original_types(self):
        # The round-trip is type-faithful: a phase-2 side-effect receives the
        # same Python types the identical synchronous transition would. This
        # pins the contract so a regression back to lossy strings is caught.
        self.widget.process.fulfil(
            when=_WHEN,
            some_uuid=_UUID,
            amount=Decimal('19.99'),
            pair=(1, 'two'),
            tags={'a', 'b'},
        )
        seen = bg_models.LAST_KWARGS
        self.assertEqual(seen['when'], _WHEN)
        self.assertIs(type(seen['when']), datetime)
        self.assertEqual(seen['some_uuid'], _UUID)
        self.assertIs(type(seen['some_uuid']), UUID)
        self.assertEqual(seen['amount'], Decimal('19.99'))
        self.assertEqual(seen['pair'], (1, 'two'))
        self.assertEqual(seen['tags'], {'a', 'b'})

    def test_legacy_untagged_row_still_runs(self):
        # A TransitionMessage written before the typed encoding carries plain
        # ISO strings — phase 2 must pass them through unchanged, not crash.
        self.widget.status = 'fulfilling'
        self.widget.save(update_fields=['status'])
        tm = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(self.widget.pk),
            process_name='process',
            transition_name='fulfil',
            queue_name='django_logic.critical',
            kwargs={'when': _WHEN.isoformat(), 'some_uuid': str(_UUID)},
        )
        run_background_transition(tm.pk)
        seen = bg_models.LAST_KWARGS
        self.assertEqual(seen['when'], _WHEN.isoformat())
        self.assertEqual(seen['some_uuid'], str(_UUID))

    def test_malformed_tagged_row_passes_through_and_completes(self):
        # A KNOWN tag whose payload no longer decodes (hand-edited row,
        # cross-version writer bug) must not wedge phase 2: the raw tagged
        # form passes through to the side-effect and the row completes.
        self.widget.status = 'fulfilling'
        self.widget.save(update_fields=['status'])
        bad = {'__dl_type__': 'datetime', 'value': 'not-a-datetime'}
        tm = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(self.widget.pk),
            process_name='process',
            transition_name='fulfil',
            queue_name='django_logic.critical',
            kwargs={'when': bad},
        )
        with self.assertLogs('django-logic.transition', level='WARNING'):
            run_background_transition(tm.pk)
        tm.refresh_from_db()
        self.assertTrue(tm.is_completed)
        self.assertEqual(tm.errors_count, 0)
        self.assertEqual(bg_models.LAST_KWARGS['when'], bad)

    def test_undecodable_kwargs_row_counts_errors_and_routes_failed_state(self):
        # kwargs whose decode genuinely raises (a user_id that cannot be a
        # pk) must be accounted like any attempt failure — errors_count
        # increments per attempt instead of escaping at 0 and being
        # re-dispatched by retry_stale_transitions forever, and exhaustion
        # routes failed_state (issue #117).
        self.widget.status = 'fulfilling'
        self.widget.save(update_fields=['status'])
        tm = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(self.widget.pk),
            process_name='process',
            transition_name='fulfil',
            queue_name='django_logic.critical',
            kwargs={'user_id': ['not', 'a', 'pk']},
        )
        with self.assertRaises(TypeError):
            run_background_transition(tm.pk)
        tm.refresh_from_db()
        self.assertFalse(tm.is_completed)
        self.assertEqual(tm.errors_count, 1)

        # Drive the remaining attempts as the worker would (the periodic
        # starter only picks rows up once RETRY_MINUTES have elapsed).
        for _ in range(2):
            with self.assertRaises(TypeError):
                run_background_transition(tm.pk)
        tm.refresh_from_db()
        self.assertTrue(tm.is_completed)
        self.assertEqual(tm.errors_count, 3)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'fulfilment_failed')

    def test_deleted_user_degrades_to_none(self):
        # A user that vanished between phase 1 and phase 2 restores to None
        # (the work becomes "system-initiated"). Drive phase 2 directly with
        # a user_id that does not resolve. The widget must sit in the
        # transition's in_progress_state, exactly as phase 1 leaves it —
        # otherwise the phase-2 state guard marks the row superseded.
        self.widget.status = 'fulfilling'
        self.widget.save(update_fields=['status'])
        tm = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(self.widget.pk),
            process_name='process',
            transition_name='fulfil',
            queue_name='django_logic.critical',
            kwargs={'user_id': 9_999_999},
        )
        run_background_transition(tm.pk)
        self.assertIn('user', bg_models.LAST_KWARGS)
        self.assertIsNone(bg_models.LAST_KWARGS['user'])
