from django_logic import Process, Transition


class InvoiceProcess(Process):
    process_name = 'invoice_process'
    states = (
        ('draft', 'Draft'),
        ('paid', 'Paid'),
        ('void', 'Void'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
    )

    transitions = [
        Transition(
            action_name='approve',
            sources=['draft'],
            target='approved'
        ),
        Transition(
            action_name='void',
            sources=['draft', 'paid'],
            target='voided'
        ),
    ]