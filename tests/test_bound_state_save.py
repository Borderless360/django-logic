import django
from django.db.models.signals import post_save
from django.test import TestCase

from django_logic.process import Process, ProcessManager
from django_logic.transition import Transition
from tests import unbind_model_process
from tests.models import Invoice, InvoiceProxy, MtiChild, MtiParent


class Approval(Process):
    process_name = 'invoice_approval'
    transitions = [Transition('approve', sources=['draft'], target='success')]


class Review(Process):
    process_name = 'invoice_review'


class BoundStateSaveTests(TestCase):
    databases = {'default', 'other'}

    def bind(self, model=Invoice, process=Approval, field='status'):
        ProcessManager.bind_model_process(model, process, state_field=field)
        self.addCleanup(unbind_model_process, model, process)

    def test_ordinary_save_preserves_approval_and_writes_customer_change(self):
        self.bind()
        invoice = Invoice.objects.create(status='draft')
        stale = Invoice.objects.get(pk=invoice.pk)
        invoice.invoice_approval.approve()
        stale.customer_received = True
        with self.assertNumQueries(1):
            stale.save()
        stale.refresh_from_db()
        self.assertEqual(stale.status, 'success')
        self.assertTrue(stale.customer_received)

    def test_insert_with_preset_key_and_explicit_state_update(self):
        self.bind()
        invoice = Invoice(pk=75, status='draft')
        invoice.save()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'draft')
        invoice.status = 'corrected'
        invoice.save(update_fields=['status'])
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'corrected')

    def test_preset_key_that_matches_an_existing_row_preserves_its_state(self):
        self.bind()
        approved = Invoice.objects.create(status='success')
        Invoice(pk=approved.pk, status='draft', customer_received=True).save()
        approved.refresh_from_db()
        self.assertTrue(approved.customer_received)
        self.assertEqual(approved.status, 'success')

    def test_copy_to_another_database_inserts_or_preserves_existing_state(self):
        self.bind()
        invoice = Invoice.objects.create(status='draft')
        invoice.save(using='other')
        copy = Invoice.objects.using('other').get(pk=invoice.pk)
        self.assertEqual(copy.status, 'draft')
        copy.status = 'success'
        copy.save(using='other', update_fields=['status'])
        stale = Invoice.objects.get(pk=invoice.pk)
        stale.customer_received = True
        stale.save(using='other')
        copy.refresh_from_db(using='other')
        self.assertTrue(copy.customer_received)
        self.assertEqual(copy.status, 'success')
        self.assertFalse(Invoice.objects.get(pk=invoice.pk).customer_received)

    def test_deferred_fields_stay_unread_and_keep_concurrent_updates(self):
        self.bind()
        invoice = Invoice.objects.create(status='draft')
        for loaded in [('customer_received',), ('customer_received', 'status')]:
            with self.subTest(loaded=loaded):
                Invoice.objects.filter(pk=invoice.pk).update(status='draft', is_available=True)
                stale = Invoice.objects.only(*loaded).get(pk=invoice.pk)
                Invoice.objects.filter(pk=invoice.pk).update(status='success', is_available=False)
                before = stale.get_deferred_fields()
                stale.customer_received = True
                with self.assertNumQueries(1):
                    stale.save(update_fields=None)
                self.assertEqual(stale.get_deferred_fields(), before)
                invoice.refresh_from_db()
                self.assertEqual(invoice.status, 'success')
                self.assertFalse(invoice.is_available)
                self.assertTrue(invoice.customer_received)

    def test_proxy_binding_protects_both_fields_through_both_models(self):
        self.bind(InvoiceProxy)
        self.bind(Invoice, Review, 'is_available')
        for model in (Invoice, InvoiceProxy):
            with self.subTest(model=model):
                self.assertEqual(ProcessManager.state_fields(model), {'status', 'is_available'})
                stale = model.objects.create(status='draft')
                model.objects.filter(pk=stale.pk).update(status='success', is_available=False)
                stale.customer_received = True
                stale.save(force_update=True)
                stale.refresh_from_db()
                self.assertEqual(stale.status, 'success')
                self.assertFalse(stale.is_available)
                self.assertTrue(stale.customer_received)

    def test_child_binding_on_inherited_field_protects_parent_and_child(self):
        self.bind(MtiChild, Review, 'review_status')
        self.bind(MtiChild, Approval, 'status')
        self.assertEqual(ProcessManager.state_fields(MtiParent), {'status'})
        self.assertEqual(ProcessManager.state_fields(MtiChild), {'status', 'review_status'})
        stale = MtiChild.objects.create(status='draft', review_status='draft')
        parent = MtiParent.objects.get(pk=stale.pk)
        MtiChild.objects.filter(pk=stale.pk).update(status='success', review_status='ready')
        parent.save()
        stale.note = 'received'
        stale.save()
        stale.refresh_from_db()
        self.assertEqual((stale.status, stale.review_status, stale.note), ('success', 'ready', 'received'))

    def test_raw_fixture_save_can_restore_state(self):
        self.bind()
        invoice = Invoice.objects.create(status='success')
        invoice.status = 'restored'
        invoice.save_base(raw=True)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'restored')

    def test_bulk_updates_can_write_a_bound_field(self):
        self.bind()
        invoice = Invoice.objects.create(status='draft')
        Invoice.objects.filter(pk=invoice.pk).update(status='ready')
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'ready')
        invoice.status = 'corrected'
        Invoice.objects.bulk_update([invoice], ['status'])
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'corrected')

    def test_signals_describe_fields_written_and_skip_empty_update(self):
        self.bind()
        events = []

        def observe(sender, created, update_fields, **kwargs):
            events.append((created, update_fields))

        post_save.connect(observe, sender=Invoice)
        self.addCleanup(post_save.disconnect, observe, sender=Invoice)
        invoice = Invoice.objects.create(status='draft')
        invoice.save()
        invoice.save(update_fields=['status'])
        Invoice.objects.only('customer_received', 'status').get(pk=invoice.pk).save()
        Invoice.objects.only('id', 'status').get(pk=invoice.pk).save()
        self.assertEqual(events, [
            (True, None),
            (False, None),
            (False, frozenset({'status'})),
            (False, frozenset({'customer_received'})),
        ])

    def test_django_positional_save_arguments(self):
        self.bind()
        invoice = Invoice.objects.create(status='success')
        invoice.status = 'draft'
        if django.VERSION < (6, 0):
            from django.utils.deprecation import RemovedInDjango60Warning

            with self.assertWarns(RemovedInDjango60Warning):
                invoice.save(False, False, None, None)
            invoice.refresh_from_db()
            self.assertEqual(invoice.status, 'success')
            invoice.status = 'corrected'
            with self.assertWarns(RemovedInDjango60Warning):
                invoice.save(False, False, None, ['status'])
            invoice.refresh_from_db()
            self.assertEqual(invoice.status, 'corrected')
        else:
            with self.assertRaises(TypeError):
                invoice.save(False, False, None, None)
