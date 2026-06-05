"""Reusable parent/child completion guard conditions (issue #81)."""
from unittest import mock

from django.test import SimpleTestCase

from django_logic.conditions import all_related_in, any_related_in


def _instance(total, in_states):
    """Fake parent whose ``children`` manager reports `total` rows and
    `in_states` rows matching a filter."""
    manager = mock.Mock()
    manager.count.return_value = total
    manager.filter.return_value.count.return_value = in_states
    manager.filter.return_value.exists.return_value = in_states > 0
    return mock.Mock(children=manager)


class AllRelatedInTests(SimpleTestCase):
    cond = staticmethod(all_related_in('children', 'status', {'done'}))

    def test_true_when_all_match(self):
        self.assertTrue(self.cond(_instance(total=3, in_states=3)))

    def test_false_when_some_unmatched(self):
        self.assertFalse(self.cond(_instance(total=3, in_states=2)))

    def test_false_when_no_children(self):
        self.assertFalse(self.cond(_instance(total=0, in_states=0)))


class AnyRelatedInTests(SimpleTestCase):
    cond = staticmethod(any_related_in('children', 'status', {'failed'}))

    def test_true_when_any_match(self):
        self.assertTrue(self.cond(_instance(total=3, in_states=1)))

    def test_false_when_none_match(self):
        self.assertFalse(self.cond(_instance(total=3, in_states=0)))
