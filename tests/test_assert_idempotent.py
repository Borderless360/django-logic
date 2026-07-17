"""Tests for ``django_logic.testing.assert_idempotent`` (issue #106).

Background side-effects re-run from scratch on every retry, so they must be
idempotent. The helper applies a side-effect twice and asserts the second
application changes nothing observable — via instance fields and/or a
``capture(instance)`` callable for off-instance effects.
"""
from django.test import TestCase

from django_logic.testing import assert_idempotent
from tests.background.models import Widget


def set_status_done(instance, **kwargs):
    instance.status = 'done'
    instance.save(update_fields=['status'])


def set_status_to(instance, value=None, **kwargs):
    instance.status = value
    instance.save(update_fields=['status'])


def append_to_log(instance, **kwargs):
    instance.se_log = (instance.se_log or '') + 'ran,'
    instance.save(update_fields=['se_log'])


def create_sibling(instance, **kwargs):
    Widget.objects.create(status='sibling')


def get_or_create_sibling(instance, **kwargs):
    Widget.objects.get_or_create(status='sibling')


def append_in_memory(instance, **kwargs):
    instance.kwargs_seen.append('x')


class AssertIdempotentTests(TestCase):

    def test_idempotent_side_effect_passes(self):
        widget = Widget.objects.create()
        assert_idempotent(set_status_done, widget, fields=['status'])
        self.assertEqual(widget.status, 'done')

    def test_non_idempotent_side_effect_fails_with_diff(self):
        widget = Widget.objects.create()
        with self.assertRaises(AssertionError) as ctx:
            assert_idempotent(append_to_log, widget, fields=['se_log'])
        message = str(ctx.exception)
        self.assertIn('append_to_log is not idempotent', message)
        self.assertIn("se_log: 'ran,' -> 'ran,ran,'", message)
        self.assertIn('retry', message)

    def test_capture_catches_off_instance_effect(self):
        widget = Widget.objects.create()
        siblings = Widget.objects.filter(status='sibling')
        with self.assertRaises(AssertionError) as ctx:
            assert_idempotent(create_sibling, widget,
                              capture=lambda i: siblings.count())
        self.assertIn('capture(): 1 -> 2', str(ctx.exception))

    def test_capture_passes_for_guarded_off_instance_effect(self):
        widget = Widget.objects.create()
        siblings = Widget.objects.filter(status='sibling')
        assert_idempotent(get_or_create_sibling, widget,
                          capture=lambda i: siblings.count())
        self.assertEqual(siblings.count(), 1)

    def test_requires_fields_or_capture(self):
        widget = Widget.objects.create()
        with self.assertRaises(TypeError) as ctx:
            assert_idempotent(set_status_done, widget)
        self.assertIn('fields', str(ctx.exception))
        self.assertIn('capture', str(ctx.exception))
        # An empty fields list is the same vacuous no-op observation.
        with self.assertRaises(TypeError):
            assert_idempotent(set_status_done, widget, fields=[])

    def test_kwargs_are_forwarded_to_fn(self):
        widget = Widget.objects.create()
        assert_idempotent(set_status_to, widget, fields=['status'],
                          value='shipped')
        self.assertEqual(widget.status, 'shipped')

    def test_in_place_mutation_caught_without_refresh(self):
        # refresh_from_db=False observes in-memory state; the deep copy at
        # observation time keeps an in-place list mutation from aliasing the
        # first observation into a vacuous pass.
        widget = Widget.objects.create()
        with self.assertRaises(AssertionError) as ctx:
            assert_idempotent(append_in_memory, widget,
                              fields=['kwargs_seen'], refresh_from_db=False)
        self.assertIn("kwargs_seen: ['x'] -> ['x', 'x']", str(ctx.exception))
