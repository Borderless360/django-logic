"""Kwargs round-trip from enqueue to execute.

These pin what a background side-effect really receives on the worker.
Sync mode runs enqueue and execute inline, so one call covers both ends.
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
from tests import dl_settings


_SYNC_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=3)

_UUID = UUID('12345678-1234-5678-1234-567812345678')
_WHEN = datetime(2026, 6, 4, 12, 30, 0)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class KwargsRoundTripTests(TestCase):
    def setUp(self):
        bg_models.LAST_KWARGS.clear()
        self.widget = Widget.objects.create()
        self.user = get_user_model().objects.create(username='actor')

    def test_side_effect_receives_restored_user_and_context(self):
        self.widget.process.fulfil(
            user=self.user,
            request=object(),
            when=_WHEN,
            some_uuid=_UUID,
        )
        seen = bg_models.LAST_KWARGS

        # The worker turns user_id back into a live User object.
        self.assertIn('user', seen)
        self.assertIsInstance(seen['user'], get_user_model())
        self.assertEqual(seen['user'].pk, self.user.pk)
        self.assertNotIn('user_id', seen)

        # A request object cannot be serialized, so enqueue drops it.
        self.assertNotIn('request', seen)

        # The worker rebuilds context, so a side-effect declared as
        # fn(instance, context, **kwargs) works in both modes.
        self.assertIn('context', seen)
        self.assertEqual(seen['context'], {})

        # owning_process_class is engine bookkeeping, not caller data. It
        # belongs on the row's column only. A side-effect declared as
        # fn(instance, owning_process_class, **kwargs) must behave the same
        # in both modes.
        self.assertNotIn('owning_process_class', seen)
        transition_message = TransitionMessage.objects.get(
            transition_name='fulfil')
        self.assertEqual(
            transition_message.owning_process_class,
            'tests.background.models.WidgetProcess',
        )

    @override_settings(
        DJANGO_LOGIC={**_SYNC_SETTINGS, 'STRICT_KWARGS_SERIALIZATION': True})
    def test_strict_request_drop_reaches_the_caller_as_its_own_error(self):
        # In strict mode the caller sees KwargsSerializationError, not the
        # dispatcher's generic "not JSON-serializable" error.
        with self.assertRaisesMessage(
                KwargsSerializationError, "'request' dropped"):
            self.widget.process.fulfil(request=object())
        # Enqueue failed before it saved anything.
        self.assertFalse(TransitionMessage.objects.exists())

    def test_owning_process_class_kept_out_of_nested_side_effect_kwargs(self):
        # Same rule for a nested owner. nested_fulfil is declared on
        # NestedBgChildProcess and reached through the bound parent_process.
        # The nested class lands on the column, never in the side-effect
        # kwargs.
        self.widget.parent_process.nested_fulfil()
        seen = bg_models.LAST_KWARGS
        self.assertNotIn('owning_process_class', seen)
        transition_message = TransitionMessage.objects.get(
            transition_name='nested_fulfil')
        self.assertEqual(
            transition_message.owning_process_class,
            'tests.background.models.NestedBgChildProcess',
        )

    def test_typed_kwargs_arrive_with_original_types(self):
        # The worker receives the same Python types the identical synchronous
        # transition would. Without this a regression back to plain strings
        # goes unnoticed.
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

    def test_untagged_row_from_an_older_release_still_runs(self):
        # A row written before the typed encoding carries plain ISO strings.
        # The worker passes them through unchanged instead of crashing.
        self.widget.status = 'fulfilling'
        self.widget.save(update_fields=['status'])
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(self.widget.pk),
            process_name='process',
            transition_name='fulfil',
            queue_name='django_logic.critical',
            kwargs={'when': _WHEN.isoformat(), 'some_uuid': str(_UUID)},
        )
        run_background_transition(transition_message.pk)
        seen = bg_models.LAST_KWARGS
        self.assertEqual(seen['when'], _WHEN.isoformat())
        self.assertEqual(seen['some_uuid'], str(_UUID))

    def test_malformed_tagged_row_passes_through_and_completes(self):
        # A known tag whose payload no longer decodes (someone edited the row
        # by hand) must not stall the worker. The raw tagged value reaches the
        # side-effect and the row completes.
        self.widget.status = 'fulfilling'
        self.widget.save(update_fields=['status'])
        bad = {'__dl_type__': 'datetime', 'value': 'not-a-datetime'}
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(self.widget.pk),
            process_name='process',
            transition_name='fulfil',
            queue_name='django_logic.critical',
            kwargs={'when': bad},
        )
        with self.assertLogs('django-logic.transition', level='WARNING'):
            run_background_transition(transition_message.pk)
        transition_message.refresh_from_db()
        self.assertTrue(transition_message.is_completed)
        self.assertEqual(transition_message.errors_count, 0)
        self.assertEqual(bg_models.LAST_KWARGS['when'], bad)

    def test_undecodable_kwargs_row_counts_errors_and_routes_failed_state(self):
        # A decode that really raises (a user_id that cannot be a primary key)
        # counts as an attempt failure. Otherwise errors_count stays at 0
        # and the row stays claimable forever.
        self.widget.status = 'fulfilling'
        self.widget.save(update_fields=['status'])
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(self.widget.pk),
            process_name='process',
            transition_name='fulfil',
            queue_name='django_logic.critical',
            kwargs={'user_id': ['not', 'a', 'pk']},
        )
        with self.assertRaises(TypeError):
            run_background_transition(transition_message.pk)
        transition_message.refresh_from_db()
        self.assertFalse(transition_message.is_completed)
        self.assertEqual(transition_message.errors_count, 1)

        # Run the remaining attempts as the worker would. A row becomes
        # claimable again once RETRY_MINUTES have passed.
        for _ in range(2):
            with self.assertRaises(TypeError):
                run_background_transition(transition_message.pk)
        transition_message.refresh_from_db()
        self.assertTrue(transition_message.is_completed)
        self.assertEqual(transition_message.errors_count, 3)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'fulfilment_failed')

    def test_deleted_user_degrades_to_none(self):
        # A user deleted before the worker runs restores to None, so the work
        # becomes system-initiated. The widget must sit in the transition's
        # in_progress_state, exactly where enqueue leaves it — otherwise the
        # worker's state guard marks the row superseded.
        self.widget.status = 'fulfilling'
        self.widget.save(update_fields=['status'])
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(self.widget.pk),
            process_name='process',
            transition_name='fulfil',
            queue_name='django_logic.critical',
            kwargs={'user_id': 9_999_999},
        )
        run_background_transition(transition_message.pk)
        self.assertIn('user', bg_models.LAST_KWARGS)
        self.assertIsNone(bg_models.LAST_KWARGS['user'])
