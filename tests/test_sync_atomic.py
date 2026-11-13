"""Opt-in synchronous transactions keep required writes and their state together."""
from unittest import skipUnless

from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, connection, transaction
from django.db.models.signals import post_init, post_save
from django.test import SimpleTestCase, TransactionTestCase

from django_logic import DatabaseTransition, Process, ProcessManager, Transition
from django_logic.background import BackgroundTransition
from tests import unbind_model_process
from tests.models import Invoice


def save_required_work(instance, **kwargs):
    instance.customer_received = True
    instance.save(update_fields=['customer_received'])
    Invoice.objects.using(instance._state.db).create(status='required')


def save_then_fail(instance, **kwargs):
    save_required_work(instance)
    raise ValueError('required work failed')


def save_then_database_error(instance, **kwargs):
    save_required_work(instance)
    Invoice.objects.using(instance._state.db).create(status=None)


class AtomicDeclarationTests(SimpleTestCase):
    def test_synchronous_default_and_explicit_false_keep_existing_behavior(self):
        self.assertFalse(Transition('go', sources=['draft']).atomic)
        self.assertFalse(Transition('go', sources=['draft'], atomic=False).atomic)
        self.assertTrue(Transition('go', sources=['draft'], atomic=True).atomic)

    def test_only_boolean_values_are_allowed(self):
        for value in (None, 0, 1, 'false', [], {}):
            with self.subTest(value=value), self.assertRaises(ImproperlyConfigured):
                Transition('go', sources=['draft'], atomic=value)

    def test_background_defaults_and_explicit_options_are_unchanged(self):
        self.assertTrue(BackgroundTransition('go', sources=['draft']).atomic)
        self.assertTrue(BackgroundTransition('go', sources=['draft'], atomic=True).atomic)
        self.assertFalse(BackgroundTransition('go', sources=['draft'], atomic=False).atomic)

    def test_atomic_mode_refuses_custom_completion_instead_of_skipping_it(self):
        class CustomCompletion(Transition):
            def complete_transition(self, state, **kwargs):
                return super().complete_transition(state, **kwargs)

        CustomCompletion('go', sources=['draft'], atomic=False)
        with self.assertRaises(ImproperlyConfigured):
            CustomCompletion('go', sources=['draft'], atomic=True)


class AtomicSynchronousTests(TransactionTestCase):
    databases = {'default', 'other'}

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.invoice = Invoice.objects.create(status='draft')
        self.observed = []

    def bind(self, *transitions):
        class AtomicProcess(Process):
            process_name = 'sync_atomic_test'

        AtomicProcess.transitions = list(transitions)
        ProcessManager.bind_model_process(Invoice, AtomicProcess, state_field='status')
        self.addCleanup(unbind_model_process, Invoice, AtomicProcess)

    def connect(self, signal, receiver):
        signal.connect(receiver, sender=Invoice, weak=False)
        self.addCleanup(signal.disconnect, receiver, sender=Invoice)

    def record_failure(self, instance, exception, **kwargs):
        self.observed.append((instance.status, instance.customer_received, exception))

    def assert_rolled_back(self, instance=None, state='draft'):
        instance = instance or self.invoice
        self.assertEqual(instance.status, state)
        self.assertFalse(instance.customer_received)
        stored = Invoice.objects.using(instance._state.db).get(pk=instance.pk)
        self.assertEqual(stored.status, state)
        self.assertFalse(stored.customer_received)
        self.assertFalse(Invoice.objects.using(instance._state.db).filter(status='required').exists())
        self.assertFalse(instance.sync_atomic_test.state.is_locked())

    def reject_target_after_save(self, sender, instance, **kwargs):
        if instance.pk == self.invoice.pk and instance.status == 'done':
            Invoice.objects.using(instance._state.db).create(status='target_audit')
            raise ValueError('target save failed')

    def test_success_saves_required_work_and_target(self):
        self.bind(Transition('go', sources=['draft'], target='done', atomic=True,
                             side_effects=[save_required_work]))
        self.invoice.sync_atomic_test.go()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'done')
        self.assertTrue(self.invoice.customer_received)
        self.assertEqual(Invoice.objects.filter(status='required').count(), 1)
        self.assertFalse(self.invoice.sync_atomic_test.state.is_locked())

    def test_atomic_empty_target_saves_required_work_and_empty_state(self):
        self.bind(Transition('go', sources=['draft'], target='', atomic=True,
                             side_effects=[save_required_work]))

        self.invoice.sync_atomic_test.go()

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, '')
        self.assertTrue(self.invoice.customer_received)
        self.assertEqual(Invoice.objects.filter(status='required').count(), 1)
        self.assertFalse(self.invoice.sync_atomic_test.state.is_locked())

    def test_atomic_empty_target_save_failure_discards_required_changes(self):
        def reject_empty_target(sender, instance, **kwargs):
            if instance.pk == self.invoice.pk and instance.status == '':
                Invoice.objects.create(status='empty_audit')
                raise ValueError('empty target save failed')

        self.bind(Transition('go', sources=['draft'], target='', atomic=True,
                             side_effects=[save_required_work],
                             failure_callbacks=[self.record_failure]))
        self.connect(post_save, reject_empty_target)

        with self.assertRaisesRegex(ValueError, 'empty target save failed') as caught:
            self.invoice.sync_atomic_test.go()

        self.assert_rolled_back()
        self.assertFalse(Invoice.objects.filter(status='empty_audit').exists())
        self.assertEqual(self.observed, [('draft', False, caught.exception)])

    def test_non_database_failure_discards_all_required_writes(self):
        self.bind(Transition('go', sources=['draft'], target='done', atomic=True,
                             side_effects=[save_then_fail], failure_callbacks=[self.record_failure]))
        with self.assertRaisesRegex(ValueError, 'required work failed') as caught:
            self.invoice.sync_atomic_test.go()
        self.assert_rolled_back()
        self.assertEqual(self.observed, [('draft', False, caught.exception)])

    def test_database_failure_rolls_back_and_connection_remains_usable(self):
        self.bind(Transition('go', sources=['draft'], target='done', atomic=True,
                             side_effects=[save_then_database_error],
                             failure_callbacks=[self.record_failure]))
        with self.assertRaises(IntegrityError) as caught:
            self.invoice.sync_atomic_test.go()
        self.assert_rolled_back()
        self.assertEqual(self.observed, [('draft', False, caught.exception)])
        Invoice.objects.create(status='healthy')

    def test_target_post_save_failure_discards_target_and_earlier_writes(self):
        self.bind(Transition('go', sources=['draft'], target='done', atomic=True,
                             side_effects=[save_required_work], failure_callbacks=[self.record_failure]))
        self.connect(post_save, self.reject_target_after_save)
        with self.assertRaisesRegex(ValueError, 'target save failed') as caught:
            self.invoice.sync_atomic_test.go()
        self.assert_rolled_back()
        self.assertFalse(Invoice.objects.filter(status='target_audit').exists())
        self.assertEqual(self.observed, [('draft', False, caught.exception)])

    def test_target_read_failure_restores_source_before_failure_callback(self):
        def refuse_target_read(sender, instance, **kwargs):
            if instance.pk == self.invoice.pk and instance.status == 'done':
                raise ValueError('target read failed')

        self.bind(Transition('go', sources=['draft'], target='done', atomic=True,
                             side_effects=[save_required_work], failure_callbacks=[self.record_failure]))
        self.connect(post_init, refuse_target_read)
        with self.assertRaisesRegex(ValueError, 'target read failed') as caught:
            self.invoice.sync_atomic_test.go()
        self.assert_rolled_back()
        self.assertEqual(self.observed, [('draft', False, caught.exception)])

    def test_failed_state_and_callback_save_use_restored_metadata(self):
        def save_failure_record(instance, exception, **kwargs):
            self.record_failure(instance, exception)
            instance.is_available = False
            instance.save()

        self.bind(Transition('go', sources=['draft'], target='done', failed_state='failed',
                             atomic=True, side_effects=[save_then_fail],
                             failure_callbacks=[save_failure_record]))
        with self.assertRaisesRegex(ValueError, 'required work failed') as caught:
            self.invoice.sync_atomic_test.go()
        self.assert_rolled_back(state='failed')
        self.assertFalse(Invoice.objects.get(pk=self.invoice.pk).is_available)
        self.assertEqual(self.observed, [('failed', False, caught.exception)])

    def test_failure_callback_database_error_keeps_failure_state_and_original_exception(self):
        def broken_failure_callback(instance, **kwargs):
            Invoice.objects.create(status=None)

        self.bind(Transition('go', sources=['draft'], target='done', failed_state='failed',
                             atomic=True, side_effects=[save_then_fail],
                             failure_callbacks=[broken_failure_callback, self.record_failure]))
        with self.assertRaisesRegex(ValueError, 'required work failed') as caught:
            self.invoice.sync_atomic_test.go()
        self.assert_rolled_back(state='failed')
        self.assertEqual(self.observed, [('failed', False, caught.exception)])
        Invoice.objects.create(status='healthy')

    def test_failed_state_write_failure_keeps_original_exception(self):
        def refuse_failed_save(sender, instance, **kwargs):
            if instance.pk == self.invoice.pk and instance.status == 'failed':
                Invoice.objects.create(status='failure_audit')
                raise RuntimeError('failure state save failed')

        self.bind(Transition('go', sources=['draft'], target='done', failed_state='failed',
                             atomic=True, side_effects=[save_then_fail],
                             failure_callbacks=[self.record_failure]))
        self.connect(post_save, refuse_failed_save)
        with self.assertRaisesRegex(ValueError, 'required work failed') as caught:
            self.invoice.sync_atomic_test.go()
        self.assert_rolled_back()
        self.assertFalse(Invoice.objects.filter(status='failure_audit').exists())
        self.assertEqual(self.observed, [('draft', False, caught.exception)])

    def test_rejected_failed_state_save_cannot_restore_discarded_metadata_through_callback(self):
        def dirty_failed_save(sender, instance, **kwargs):
            if instance.pk == self.invoice.pk and instance.status == 'failed':
                instance.customer_received = True
                Invoice.objects.create(status='failure_audit')
                raise RuntimeError('failed-state signal refused the save')

        def save_failure_record(instance, exception, **kwargs):
            self.record_failure(instance, exception)
            instance.is_available = False
            instance.save()

        self.bind(Transition('go', sources=['draft'], target='done', failed_state='failed',
                             atomic=True, side_effects=[save_then_fail],
                             failure_callbacks=[save_failure_record]))
        self.connect(post_save, dirty_failed_save)
        with self.assertRaisesRegex(ValueError, 'required work failed') as caught:
            self.invoice.sync_atomic_test.go()
        self.assert_rolled_back()
        self.assertFalse(Invoice.objects.get(pk=self.invoice.pk).is_available)
        self.assertFalse(Invoice.objects.filter(status='failure_audit').exists())
        self.assertEqual(self.observed, [('draft', False, caught.exception)])

    def test_recovery_read_failure_preserves_original_and_skips_failure_hooks(self):
        recovering = []

        def fail_work(instance, **kwargs):
            save_required_work(instance)
            recovering.append(True)
            raise ValueError('original work failure')

        def refuse_recovery_read(sender, instance, **kwargs):
            if recovering and instance.pk == self.invoice.pk:
                raise RuntimeError('recovery read failed')

        self.bind(Transition('go', sources=['draft'], target='done', failed_state='failed',
                             atomic=True, side_effects=[fail_work],
                             failure_callbacks=[self.record_failure]))
        self.connect(post_init, refuse_recovery_read)
        with self.assertLogs('django-logic.transition', level='ERROR') as logs:
            with self.assertRaisesRegex(ValueError, 'original work failure'):
                self.invoice.sync_atomic_test.go()
        recovering.clear()
        self.assertEqual(self.invoice.status, 'draft')
        self.assertFalse(self.invoice.sync_atomic_test.state.is_locked())
        self.assertEqual(self.observed, [])
        self.assertFalse(Invoice.objects.get(pk=self.invoice.pk).customer_received)
        self.assertFalse(Invoice.objects.filter(status__in=['required', 'failed']).exists())
        self.assertIn('recovery read failed', '\n'.join(logs.output))

    def test_suppressed_database_error_cannot_report_success(self):
        def suppress_database_error(instance, **kwargs):
            save_required_work(instance)
            try:
                Invoice.objects.create(status=None)
            except IntegrityError:
                pass

        self.bind(Transition('go', sources=['draft'], atomic=True,
                             side_effects=[suppress_database_error],
                             failure_callbacks=[self.record_failure]))
        with self.assertRaisesRegex(Exception, 'rolled back'):
            self.invoice.sync_atomic_test.go()
        self.assert_rolled_back()
        self.assertEqual(len(self.observed), 1)

    def test_nested_success_is_undone_when_parent_target_save_fails(self):
        child = Invoice.objects.create(status='draft')
        published = []

        def change_child(instance, **kwargs):
            save_required_work(instance)
            child.sync_atomic_test.child()

        def publish_child_after_commit(instance, **kwargs):
            transaction.on_commit(lambda: published.append(instance.pk), using=instance._state.db)

        self.bind(
            Transition('go', sources=['draft'], target='done', atomic=True,
                       side_effects=[change_child]),
            Transition('child', sources=['draft'], target='child_done', atomic=True,
                       side_effects=[save_required_work], callbacks=[publish_child_after_commit]),
        )
        self.connect(post_save, self.reject_target_after_save)
        with self.assertRaisesRegex(ValueError, 'target save failed'):
            self.invoice.sync_atomic_test.go()
        self.assert_rolled_back()
        child.refresh_from_db()
        self.assert_rolled_back(child)
        self.assertEqual(published, [])
        self.assertFalse(Invoice.objects.filter(status='target_audit').exists())

    def test_nested_failure_discards_child_failed_state_with_parent_work(self):
        child = Invoice.objects.create(status='draft')

        def change_child(instance, **kwargs):
            save_required_work(instance)
            child.sync_atomic_test.child()

        self.bind(
            Transition('go', sources=['draft'], target='done', failed_state='failed', atomic=True,
                       side_effects=[change_child]),
            Transition('child', sources=['draft'], target='child_done', failed_state='child_failed',
                       atomic=True, side_effects=[save_then_fail]),
        )
        with self.assertRaisesRegex(ValueError, 'required work failed'):
            self.invoice.sync_atomic_test.go()
        self.assert_rolled_back(state='failed')
        child.refresh_from_db()
        self.assert_rolled_back(child)

    def test_nonrobust_commit_callback_failure_does_not_report_required_work_as_failed(self):
        publication_error = RuntimeError('publication after commit failed')

        def publish():
            raise publication_error

        def save_and_schedule_publication(instance, **kwargs):
            save_required_work(instance)
            transaction.on_commit(publish, using=instance._state.db, robust=False)

        self.bind(
            Transition('go', sources=['draft'], target='done', atomic=True,
                       side_effects=[save_and_schedule_publication], failure_callbacks=[self.record_failure]),
            Transition('poke', sources=['draft'], lock=False, atomic=True,
                       side_effects=[save_and_schedule_publication], failure_callbacks=[self.record_failure]),
        )
        for action, expected_state in [('go', 'done'), ('poke', 'draft')]:
            with self.subTest(action=action):
                invoice = Invoice.objects.create(status='draft')
                self.assertFalse(transaction.get_connection(invoice._state.db).in_atomic_block)
                with self.assertRaisesRegex(RuntimeError, 'publication after commit failed') as caught:
                    if action == 'go':
                        invoice.sync_atomic_test.go()
                    else:
                        invoice.sync_atomic_test.poke()
                self.assertIs(caught.exception, publication_error)
                invoice.refresh_from_db()
                self.assertEqual(invoice.status, expected_state)
                self.assertTrue(invoice.customer_received)
                self.assertFalse(invoice.sync_atomic_test.state.is_locked())
        self.assertEqual(Invoice.objects.filter(status='required').count(), 2)
        self.assertEqual(self.observed, [])

    def test_default_retains_non_database_failure_writes(self):
        self.bind(Transition('go', sources=['draft'], target='done', side_effects=[save_then_fail]))
        with self.assertRaisesRegex(ValueError, 'required work failed'):
            self.invoice.sync_atomic_test.go()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'draft')
        self.assertTrue(self.invoice.customer_received)
        self.assertEqual(Invoice.objects.filter(status='required').count(), 1)
        self.assertFalse(self.invoice.sync_atomic_test.state.is_locked())

    def test_explicit_false_retains_non_database_failure_writes(self):
        self.bind(Transition('go', sources=['draft'], target='done', atomic=False,
                             side_effects=[save_then_fail]))
        with self.assertRaisesRegex(ValueError, 'required work failed'):
            self.invoice.sync_atomic_test.go()
        self.invoice.refresh_from_db()
        self.assertTrue(self.invoice.customer_received)
        self.assertEqual(Invoice.objects.filter(status='required').count(), 1)

    def test_required_transaction_and_recovery_follow_instance_database_alias(self):
        invoice = Invoice.objects.using('other').create(pk=self.invoice.pk, status='draft')
        self.bind(Transition('go', sources=['draft'], target='done', failed_state='failed', atomic=True,
                             side_effects=[save_then_fail], failure_callbacks=[self.record_failure]))
        with self.assertRaisesRegex(ValueError, 'required work failed'):
            invoice.sync_atomic_test.go()
        self.assert_rolled_back(invoice, state='failed')
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).status, 'draft')
        self.assertEqual(Invoice.objects.count(), 1)

    def test_optional_callback_and_next_action_failures_keep_successful_changes(self):
        def broken_callback(instance, **kwargs):
            Invoice.objects.create(status=None)

        def later_callback(instance, **kwargs):
            self.observed.append(instance.status)

        def follow_after_success(instance, **kwargs):
            instance.sync_atomic_test.follow(**kwargs)

        self.bind(
            Transition('go', sources=['draft'], target='done', atomic=True,
                       side_effects=[save_required_work],
                       callbacks=[broken_callback, later_callback, follow_after_success]),
            Transition('follow', sources=['done'], target='followed', atomic=True,
                       side_effects=[save_then_fail]),
        )
        self.invoice.sync_atomic_test.go()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'done')
        self.assertTrue(self.invoice.customer_received)
        self.assertEqual(Invoice.objects.filter(status='required').count(), 1)
        self.assertEqual(self.observed, ['done'])
        self.assertFalse(self.invoice.sync_atomic_test.state.is_locked())

    def test_no_side_effects_target_failure_writes_failed_state_and_can_retry(self):
        self.bind(Transition('go', sources=['draft', 'failed'], target='done', failed_state='failed',
                             atomic=True, failure_callbacks=[self.record_failure]))
        self.connect(post_save, self.reject_target_after_save)
        with self.assertRaisesRegex(ValueError, 'target save failed'):
            self.invoice.sync_atomic_test.go()
        self.assert_rolled_back(state='failed')
        self.assertFalse(Invoice.objects.filter(status='target_audit').exists())
        post_save.disconnect(self.reject_target_after_save, sender=Invoice)
        self.invoice.sync_atomic_test.go()
        self.assertEqual(self.invoice.status, 'done')

    def test_lock_free_atomic_failure_does_not_release_another_callers_lock(self):
        self.bind(Transition('go', sources=['draft'], atomic=True, lock=False,
                             side_effects=[save_then_fail]))
        other_state = self.invoice.sync_atomic_test.state
        self.assertTrue(other_state.lock())
        self.addCleanup(other_state.unlock)
        with self.assertRaisesRegex(ValueError, 'required work failed'):
            self.invoice.sync_atomic_test.go()
        self.assertTrue(other_state.is_locked())
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'draft')
        self.assertFalse(self.invoice.customer_received)
        self.assertFalse(Invoice.objects.filter(status='required').exists())

    @skipUnless(connection.vendor == 'postgresql', 'DatabaseTransition requires PostgreSQL')
    def test_database_transition_saves_required_work_and_target(self):
        self.bind(DatabaseTransition('go', sources=['draft'], target='done', atomic=True,
                                     side_effects=[save_required_work]))
        self.invoice.sync_atomic_test.go()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'done')
        self.assertTrue(self.invoice.customer_received)
        self.assertEqual(Invoice.objects.filter(status='required').count(), 1)
        self.assertFalse(self.invoice.sync_atomic_test.state.is_locked())

    @skipUnless(connection.vendor == 'postgresql', 'DatabaseTransition requires PostgreSQL')
    def test_database_transition_target_failure_rolls_back_work_and_records_failed_state(self):
        self.bind(DatabaseTransition('go', sources=['draft'], target='done', failed_state='failed',
                                     atomic=True, side_effects=[save_required_work],
                                     failure_callbacks=[self.record_failure]))
        self.connect(post_save, self.reject_target_after_save)
        with self.assertRaisesRegex(ValueError, 'target save failed') as caught:
            self.invoice.sync_atomic_test.go()
        self.assert_rolled_back(state='failed')
        self.assertFalse(Invoice.objects.filter(status='target_audit').exists())
        self.assertEqual(self.observed, [('failed', False, caught.exception)])
