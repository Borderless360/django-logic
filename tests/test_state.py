from django.test import TestCase
from demo.models import Invoice
from django_logic.state import State


class StateTestCase(TestCase):
    def setUp(self) -> None:
        self.state = State(Invoice.objects.create(status='draft'), 'status')

    def test_hash_remains_the_same(self):
        self.assertEqual(self.state._get_hash(), self.state._get_hash())

    def test_get_db_state(self):
        self.assertEqual(self.state.get_db_state(), 'draft')

    def test_lock(self):
        self.assertFalse(self.state.is_locked())
        self.state.lock()
        self.assertTrue(self.state.is_locked())

        # nothing should happen
        self.state.lock()
        self.assertTrue(self.state.is_locked())

        self.state.unlock()
        self.assertFalse(self.state.is_locked())

    def test_set_state(self):
        self.state.set_state('void')
        self.assertEqual(self.state.instance.status, 'void')
        # make sure it was saved to db
        self.state.instance.refresh_from_db()
        self.assertEqual(self.state.instance.status, 'void')
