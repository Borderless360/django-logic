from django_logic import Process, Transition


class InvoiceProcess(Process):
    process_name = 'Invoice Process'

    states = (
        ('draft', 'Draft'),
        ('paid', 'Paid'),
        ('voided', 'Voided'),
    )

    transitions = [
        Transition(action_name='approve', sources=['draft'], target='approved'),
        Transition(action_name='pay', sources=['draft'], target='paid'),
        Transition(action_name='void', sources=['draft', 'paid'], target='voided'),
    ]