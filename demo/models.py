from django.db import models
from demo.process import LockerProcess
from django_logic.process import ProcessManager


class Lock(ProcessManager.bind_state_fields(status=LockerProcess), models.Model):
    status = models.CharField(choices=LockerProcess.states, default=LockerProcess.states.open, max_length=16, blank=True)
    customer_received_notice = models.BooleanField(default=False)
    is_available = models.BooleanField(default=True)

    def __str__(self):
        return self.status
