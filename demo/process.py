from django_logic import Process, Transition
from django_logic.tasks import SideEffectTasks


class CeleryTransition(Transition):
    side_effects = SideEffectTasks()


class InvoiceProcess(Process):
    states = (
        ('draft', 'Draft'),
        ('paid', 'Paid'),
        ('void', 'Void'),
    )

    transitions = [
        Transition(action_name='approve',sources=['draft'], target='approved'),
        CeleryTransition(action_name='send_to_customer',
                         sources=['approved'],
                         side_effects=['app.tasks.send_to_a_customer'],
                         target='sent'),
        Transition(action_name='void', sources=['draft', 'paid'], target='voided'),
    ]