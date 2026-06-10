"""End-to-end kwargs serialization round-trip across the phase-1/phase-2
boundary.

These pin the *contract* of what a background side-effect actually receives
in phase 2 — the single biggest previously-untested gap, since the whole
point of django_logic.background.serializers is this boundary. Runs in sync
mode (the default) so phase 1 + phase 2 execute inline.
"""
from datetime import datetime
from uuid import UUID

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from django_logic.background.models import TransitionMessage
from django_logic.background.runner import run_background_transition
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

    def test_datetime_and_uuid_arrive_as_strings_documented_contract(self):
        # The serialization round-trip is lossy by design: types are not
        # preserved. This test pins that contract so a future "fix" that
        # silently changes it is caught.
        self.widget.process.fulfil(when=_WHEN, some_uuid=_UUID)
        seen = bg_models.LAST_KWARGS
        self.assertIsInstance(seen['when'], str)
        self.assertEqual(seen['when'], _WHEN.isoformat())
        self.assertIsInstance(seen['some_uuid'], str)
        self.assertEqual(seen['some_uuid'], str(_UUID))

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
