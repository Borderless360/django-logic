"""A side effect whose unit of work is not the bound row must not wait on
that row's lock.

``lock=False`` runs the side effect with no state lock, so several may run
at once on one instance and one runs while a background transition on that
instance is uncompleted. It keeps conditions, permissions, callbacks and
failure callbacks. It may not write state — no ``target``, no
``failed_state`` — and ``BackgroundTransition`` refuses it. A lock it never
took it must never release: a State holding no token deletes the key
outright, which would free a lock another transition holds.
"""
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase

from django_logic import Process, ProcessManager, Transition
from django_logic.background import BackgroundTransition
from django_logic.background.models import TransitionMessage
from django_logic.exceptions import (
    TransitionNotAllowed,
    TransitionTemporarilyUnavailable,
)
from django_logic.logger import TransitionEventType
from django_logic.state import State
from tests.models import Invoice


class DeclarationTests(SimpleTestCase):
    def test_a_target_is_refused(self):
        with self.assertRaisesRegex(ImproperlyConfigured, 'lock=False needs target=None'):
            Transition('go', sources=['draft'], target='done', lock=False)

    def test_a_failed_state_is_refused(self):
        with self.assertRaisesRegex(ImproperlyConfigured, 'cannot declare a failed_state'):
            Transition('go', sources=['draft'], failed_state='failed', lock=False)

    def test_a_background_transition_refuses_it(self):
        with self.assertRaisesRegex(ImproperlyConfigured, 'lock=False is not supported'):
            BackgroundTransition('go', sources=['draft'], lock=False)

    def test_an_unknown_keyword_is_refused_and_named(self):
        with self.assertRaisesRegex(ImproperlyConfigured, 'does not take lokc'):
            Transition('go', sources=['draft'], lokc=False)

    def test_the_background_keywords_still_pass(self):
        transition = BackgroundTransition(
            'go', sources=['draft'], target='done', queue='q', timeout=5)
        self.assertEqual(transition.get_queue_name(), 'q')

    def test_the_default_keeps_the_lock(self):
        self.assertTrue(Transition('go', sources=['draft']).lock)

    def test_str_says_so(self):
        self.assertIn('no lock', str(Transition('go', sources=['draft'], lock=False)))


_ran = []


def _record(instance, **kwargs):
    _ran.append(('side_effect', kwargs.get('parcel')))


def _record_callback(instance, **kwargs):
    _ran.append(('callback', kwargs.get('parcel')))


def _record_failure(instance, exception=None, **kwargs):
    _ran.append(('failure', type(exception).__name__))


def _boom(instance, **kwargs):
    raise ValueError('the store said no')


def _never(instance):
    return False


def _staff_only(instance, user):
    return user == 'staff'


class LockFreeProcess(Process):
    process_name = 'lockfree'
    transitions = [
        Transition('post', sources=['draft'], lock=False,
               side_effects=[_record], callbacks=[_record_callback],
               failure_callbacks=[_record_failure]),
        Transition('post_fails', sources=['draft'], lock=False,
               side_effects=[_boom], failure_callbacks=[_record_failure]),
        Transition('post_gated', sources=['draft'], lock=False,
               conditions=[_never], side_effects=[_record]),
        Transition('post_staff', sources=['draft'], lock=False,
               permissions=[_staff_only], side_effects=[_record]),
        Transition('go', sources=['draft'], target='done'),
    ]


class RunTests(TestCase):
    def setUp(self):
        ProcessManager.bind_model_process(Invoice, LockFreeProcess, state_field='status')
        self.addCleanup(ProcessManager.unbind_model_process, Invoice, LockFreeProcess)
        cache.clear()
        self.addCleanup(cache.clear)
        _ran.clear()
        self.invoice = Invoice.objects.create(status='draft')

    def _someone_else_holds_the_lock(self):
        other = State(self.invoice, 'status', 'lockfree')
        self.assertTrue(other.lock())
        return other

    def test_it_runs_while_another_transition_holds_the_lock(self):
        self._someone_else_holds_the_lock()
        self.invoice.lockfree.post(parcel=1)
        self.assertEqual(_ran, [('side_effect', 1), ('callback', 1)])

    def test_a_lock_taking_sibling_is_still_refused(self):
        self._someone_else_holds_the_lock()
        with self.assertRaisesRegex(TransitionNotAllowed, 'State is locked'):
            self.invoice.lockfree.go()

    def test_it_stays_listed_while_the_row_is_locked(self):
        self._someone_else_holds_the_lock()
        actions = list(self.invoice.lockfree.get_available_actions())
        self.assertIn('post', actions)
        self.assertNotIn('go', actions)

    def test_it_never_releases_a_lock_it_did_not_take(self):
        other = self._someone_else_holds_the_lock()
        self.invoice.lockfree.post(parcel=1)
        self.assertTrue(other.is_locked(), 'the lock-free transition freed a foreign lock')
        other.unlock()
        self.assertFalse(other.is_locked())

    def test_a_failure_runs_the_failure_callbacks_and_re_raises(self):
        other = self._someone_else_holds_the_lock()
        with self.assertRaisesRegex(ValueError, 'the store said no'):
            self.invoice.lockfree.post_fails()
        self.assertEqual(_ran, [('failure', 'ValueError')])
        self.assertTrue(other.is_locked())

    def test_it_writes_no_state_and_leaves_no_lock(self):
        self.invoice.lockfree.post(parcel=1)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'draft')
        self.assertFalse(State(self.invoice, 'status', 'lockfree').is_locked())

    def test_conditions_and_permissions_still_gate_it(self):
        with self.assertRaises(TransitionNotAllowed):
            self.invoice.lockfree.post_gated()
        with self.assertRaises(TransitionNotAllowed):
            self.invoice.lockfree.post_staff(user='guest')
        self.invoice.lockfree.post_staff(user='staff')
        self.assertEqual(_ran, [('side_effect', None)])

    def test_two_runs_on_one_instance_both_complete(self):
        self.invoice.lockfree.post(parcel=1)
        self.invoice.lockfree.post(parcel=2)
        self.assertEqual([p for kind, p in _ran if kind == 'side_effect'], [1, 2])

    def test_the_lifecycle_log_says_the_lock_was_skipped_and_never_released(self):
        key = self.invoice.lockfree.state.instance_key
        with self.assertLogs('django-logic.transition', level='INFO') as logs:
            self.invoice.lockfree.post(parcel=1)
        joined = '\n'.join(logs.output)
        self.assertIn(f'{TransitionEventType.LOCK.value} skipped {key}', joined)
        self.assertNotIn(TransitionEventType.UNLOCK.value, joined)


class BackgroundInFlightTests(TestCase):
    """The background gate protects the state field. A lock-free transition
    never touches it, so a store mid-fetch still posts its parcels."""

    def setUp(self):
        ProcessManager.bind_model_process(Invoice, LockFreeProcess, state_field='status')
        self.addCleanup(ProcessManager.unbind_model_process, Invoice, LockFreeProcess)
        cache.clear()
        self.addCleanup(cache.clear)
        _ran.clear()
        self.invoice = Invoice.objects.create(status='draft')
        TransitionMessage.objects.create(
            app_label=Invoice._meta.app_label,
            model_name=Invoice._meta.model_name,
            instance_id=str(self.invoice.pk),
            process_name='lockfree',
            transition_name='fetch',
            queue_name='django_logic',
            is_completed=False,
        )

    def test_it_runs_while_a_background_transition_is_uncompleted(self):
        self.invoice.lockfree.post(parcel=1)
        self.assertEqual(_ran, [('side_effect', 1), ('callback', 1)])

    def test_a_lock_taking_sibling_waits_for_the_background_transition(self):
        with self.assertRaises(TransitionTemporarilyUnavailable):
            self.invoice.lockfree.go()
