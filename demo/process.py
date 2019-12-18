from django_logic import Process, Transition
from django_logic.tasks import SideEffectTasks, CallbacksTasks


class InProgressTransition(Transition):
    side_effects = SideEffectTasks()


class ProgressTransition(Transition):
    side_effects = SideEffectTasks()
    callbacks = CallbacksTasks()


class InvoiceProcess(Process):
    states = (
        ('draft', 'Draft'),
        ('paid', 'Paid'),
        ('void', 'Void'),
        ('sent', 'Sent'),
    )

    transitions = [
        Transition(action_name='approve',sources=['draft'], target='approved'),
        InProgressTransition(action_name='send_to_customer',
                             sources=['approved'],
                             side_effects=['demo.tasks.send_to_a_customer'],
                             target='sent'),
        Transition(action_name='void', sources=['draft', 'paid'], target='voided'),
        ProgressTransition(action_name='demo', sources=['draft'], target='sent',
                           in_progress_state='in_progress',
                           side_effects=['demo.tasks.demo_task_1',
                                         'demo.tasks.demo_task_2',
                                         'demo.tasks.demo_task_3'],
                           callbacks=['demo.tasks.demo_task_4',
                                      'demo.tasks.demo_task_5'])

    ]