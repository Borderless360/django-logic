"""Removed declaration options fail before a process can use them."""
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from django_logic import DatabaseTransition, Transition
from django_logic.background import BackgroundTransition


class RemovedTransitionApiTests(SimpleTestCase):
    def test_next_transition_is_not_an_accepted_declaration_option(self):
        for transition_class in (Transition, DatabaseTransition, BackgroundTransition):
            for value in ('followup', None):
                with self.subTest(transition=transition_class.__name__, value=value):
                    with self.assertRaisesMessage(
                            ImproperlyConfigured, 'does not take next_transition'):
                        transition_class('start', sources=['draft'], target='done',
                                         next_transition=value)

    def test_next_transition_command_has_no_compatibility_alias(self):
        from django_logic import commands

        self.assertFalse(hasattr(commands, 'NextTransition'))
