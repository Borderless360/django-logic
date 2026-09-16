"""Ten parcels of one store post at once and none waits for another.

``lock=False`` is the declaration for a side effect whose unit of work is
not the bound row. This is the proof under real threads and a real cache:
the lock-taking sibling serialises on the row and the losers are refused,
the lock-free transition runs on every thread, and a lock another
transition holds is left exactly as it was.
"""
import threading
import time

from django.test import tag

from django_logic import Process, ProcessManager, Transition
from django_logic.exceptions import TransitionNotAllowed
from django_logic.state import State

from tests.stability.base import StabilityTestCase, run_concurrent
from tests.stability.models import Order

_posted = []
_posted_guard = threading.Lock()


def post_parcel(instance, **kwargs):
    # Long enough that ten threads overlap inside the side effect.
    time.sleep(0.2)
    with _posted_guard:
        _posted.append(kwargs.get('parcel'))


class StoreProcess(Process):
    process_name = 'store_process'
    transitions = [
        Transition('post_parcel', sources=['draft'], lock=False, side_effects=[post_parcel]),
        Transition('post_parcel_locked', sources=['draft'], side_effects=[post_parcel]),
    ]


@tag('stability')
class LockFreeTransitionUnderThreadsTests(StabilityTestCase):
    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(Order, StoreProcess, state_field='status')
        self.addCleanup(ProcessManager.unbind_model_process, Order, StoreProcess)
        _posted.clear()
        self.order = Order.objects.create(status='draft')

    def _post(self, action, parcel):
        # Each thread reads its own row on its own connection.
        order = Order.objects.get(pk=self.order.pk)
        getattr(order.store_process, action)(parcel=parcel)

    def _ten(self, action):
        return run_concurrent(
            self._post, n_threads=10,
            args_per_thread=[((action, parcel), {}) for parcel in range(10)],
        )

    def test_ten_parcels_post_at_once_and_none_is_refused(self):
        outcomes = self._ten('post_parcel')
        self.assertEqual([error for _, error in outcomes if error], [])
        self.assertEqual(sorted(_posted), list(range(10)))

    def test_the_lock_taking_sibling_refuses_the_overlapping_callers(self):
        outcomes = self._ten('post_parcel_locked')
        refused = [error for _, error in outcomes if isinstance(error, TransitionNotAllowed)]
        self.assertTrue(refused, 'ten overlapping callers on one lock, none refused')
        self.assertEqual(len(_posted) + len(refused), 10)

    def test_it_runs_under_a_foreign_lock_and_leaves_that_lock_held(self):
        holder = State(self.order, 'status', 'store_process')
        self.assertTrue(holder.lock())
        self.track_lock(holder)
        outcomes = self._ten('post_parcel')
        self.assertEqual([error for _, error in outcomes if error], [])
        self.assertEqual(len(_posted), 10)
        self.assertTrue(holder.is_locked(), 'a lock-free transition freed a foreign lock')
        holder.unlock()
