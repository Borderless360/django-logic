from django.db import models
from django.test import TestCase

from django_logic.process import ProcessManager, Process
from django_logic.transition import Transition


class FirstProcess(Process):
    process_name = 'first_process'
    queryset = models.Manager()  # fake it to pass init
    transitions = [
        Transition(action_name='transition1', sources=['state1'], target='state3')
    ]


class SecondProcess(Process):
    process_name = 'second_process'
    queryset = models.Manager()  # fake it to pass init
    transitions = [
        Transition(action_name='transition2', sources=['state2'], target='state3')
    ]


class ProcessManagerTestCase(TestCase):
    def test_processes_bound_correctly(self):
        bind_class = ProcessManager.bind_state_fields(first_state=FirstProcess, second_state=SecondProcess)
        bind_class_obj = bind_class()
        self.assertTrue(hasattr(bind_class_obj, 'first_process'))
        self.assertTrue(isinstance(bind_class_obj.first_process, FirstProcess))
        self.assertTrue(hasattr(bind_class_obj, 'second_process'))
        self.assertTrue(isinstance(bind_class_obj.second_process, SecondProcess))
