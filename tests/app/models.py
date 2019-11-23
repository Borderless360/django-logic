from django.db import models

from django_logic.process import ProcessManager
from tests.app.process import InvoiceProcess


class Invoice(ProcessManager.bind_state_fields(status=InvoiceProcess), models.Model):
    status = models.CharField(choices=InvoiceProcess.states, max_length=16, blank=True)
    reference = models.CharField(max_length=16)

    def __str__(self):
        return self.reference
