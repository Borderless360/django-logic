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

    def test_background_effects_are_not_an_accepted_declaration_option(self):
        for transition_class in (Transition, DatabaseTransition, BackgroundTransition):
            with self.subTest(transition=transition_class.__name__):
                with self.assertRaisesMessage(ImproperlyConfigured, 'does not take background_effects'):
                    transition_class('start', sources=['draft'], background_effects=[])

    def test_background_effect_has_no_public_alias_or_runtime_module(self):
        from importlib import import_module
        from django_logic import background

        self.assertFalse(hasattr(background, 'BackgroundEffect'))
        with self.assertRaises(ModuleNotFoundError):
            import_module('django_logic.background.effects')
