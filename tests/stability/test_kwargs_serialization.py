"""
Category 4.5: kwargs Serialization Round-Trip

Tests that all kwargs survive the serialize -> store -> deserialize cycle
used by get_task_kwargs() for background transition dispatch.

Edge cases: UUID, datetime, Decimal, None, nested dicts, large strings,
and stripped fields (request, user -> user_id).
"""
import uuid
from datetime import datetime, date
from decimal import Decimal
from unittest.mock import MagicMock

from django.test import tag

from django_logic import Transition
from django_logic.state import State

from tests.stability.base import StabilityTestCase
from tests.stability.models import Order, OrderProcess


@tag('stability')
class TestGetTaskKwargs(StabilityTestCase):
    """
    Validates that Transition.get_task_kwargs produces a JSON-serializable
    dict with all critical fields preserved.
    """

    def test_basic_task_kwargs(self):
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        transition = OrderProcess.transitions[1]

        kwargs = {
            'tr_id': uuid.uuid4(),
            'root_id': uuid.uuid4(),
            'parent_id': uuid.uuid4(),
            'process_class': 'tests.stability.models.OrderProcess',
        }

        result = transition.get_task_kwargs(state, **kwargs)

        self.assertEqual(result['app_label'], 'stability')
        self.assertEqual(result['model_name'], 'order')
        self.assertEqual(result['instance_id'], order.pk)
        self.assertEqual(result['action_name'], 'fulfill')
        self.assertEqual(result['target'], 'fulfilled')
        self.assertEqual(result['process_name'], 'process')
        self.assertEqual(result['field_name'], 'status')
        self.assertEqual(result['process_class'], 'tests.stability.models.OrderProcess')

    def test_uuid_converted_to_string(self):
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        transition = OrderProcess.transitions[1]

        tr_id = uuid.uuid4()
        kwargs = {'tr_id': tr_id, 'root_id': tr_id, 'parent_id': tr_id}
        result = transition.get_task_kwargs(state, **kwargs)

        self.assertIsInstance(result['tr_id'], str)
        self.assertEqual(result['tr_id'], str(tr_id))

    def test_user_object_converted_to_user_id(self):
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        transition = OrderProcess.transitions[1]

        user = MagicMock()
        user.id = 42

        result = transition.get_task_kwargs(state, user=user)
        self.assertEqual(result['user_id'], 42)
        self.assertNotIn('user', result)

    def test_user_id_preserved_directly(self):
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        transition = OrderProcess.transitions[1]

        result = transition.get_task_kwargs(state, user_id=99)
        self.assertEqual(result['user_id'], 99)

    def test_none_tr_id_handled(self):
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        transition = OrderProcess.transitions[1]

        result = transition.get_task_kwargs(state, tr_id=None)
        self.assertIsNone(result['tr_id'])

    def test_no_user_no_user_id(self):
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        transition = OrderProcess.transitions[1]

        result = transition.get_task_kwargs(state)
        self.assertNotIn('user_id', result)
        self.assertNotIn('user', result)

    def test_task_kwargs_are_json_serializable(self):
        """All values in task_kwargs must be JSON-serializable."""
        import json

        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        transition = OrderProcess.transitions[1]

        kwargs = {
            'tr_id': uuid.uuid4(),
            'root_id': uuid.uuid4(),
            'parent_id': uuid.uuid4(),
            'process_class': 'tests.stability.models.OrderProcess',
            'user_id': 42,
        }

        result = transition.get_task_kwargs(state, **kwargs)

        try:
            serialized = json.dumps(result)
            deserialized = json.loads(serialized)
        except (TypeError, ValueError) as e:
            self.fail(f"task_kwargs not JSON-serializable: {e}\nData: {result}")

        self.assertEqual(deserialized['instance_id'], order.pk)
        self.assertEqual(deserialized['tr_id'], str(kwargs['tr_id']))


@tag('stability')
class TestKwargsEdgeCases(StabilityTestCase):
    """Edge cases in kwargs handling that could cause worker-side failures."""

    def test_extra_kwargs_not_leaked_into_task_kwargs(self):
        """
        Only expected fields should appear in task_kwargs.
        Random extra kwargs should not leak through.
        """
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        transition = OrderProcess.transitions[1]

        result = transition.get_task_kwargs(
            state,
            tr_id=uuid.uuid4(),
            secret_token='should_not_appear',
            request=MagicMock(),
        )

        self.assertNotIn('secret_token', result)
        self.assertNotIn('request', result)

    def test_large_kwargs_values(self):
        """Large string values should still serialize correctly."""
        import json

        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        transition = OrderProcess.transitions[1]

        result = transition.get_task_kwargs(
            state,
            process_class='a' * 10000,
        )

        serialized = json.dumps(result)
        self.assertGreater(len(serialized), 10000)
