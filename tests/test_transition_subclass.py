from django.test import TestCase, override_settings

from django_logic import Process, Transition
from django_logic.background import BackgroundTransition
from django_logic.background.models import TransitionMessage
from django_logic.exceptions import TransitionNotAllowed
from tests import dl_settings
from tests.models import Invoice


class RecordAcceptedAction:
    def change_state(self, state, *, record_action, **kwargs):
        result = super().change_state(state, **kwargs)
        record_action(self.action_name)
        return result


class ReviewedTransition(RecordAcceptedAction, Transition):
    pass


class ReviewedBackgroundTransition(RecordAcceptedAction, BackgroundTransition):
    pass


class InvoiceReview(Process):
    transitions = [
        ReviewedTransition('review', sources=['draft'], target='reviewed'),
        ReviewedBackgroundTransition('send', sources=['reviewed'], target='sent'),
    ]


@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class TransitionSubclassTests(TestCase):
    def test_subclass_can_record_an_accepted_action_and_keep_its_own_argument(self):
        invoice = Invoice.objects.create(status='draft')
        process = InvoiceReview(instance=invoice, field_name='status')
        actions = []

        with self.assertRaises(TransitionNotAllowed):
            process.send(record_action=actions.append)
        self.assertEqual(actions, [])

        self.assertIsNotNone(process.review(record_action=actions.append))
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'reviewed')
        self.assertIsNotNone(process.send(record_action=actions.append))
        self.assertEqual(actions, ['review', 'send'])
        job = TransitionMessage.objects.get(instance_id=invoice.pk, transition_name='send')
        self.assertNotIn('record_action', job.kwargs)
