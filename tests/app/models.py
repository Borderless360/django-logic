from django.db import models

from django_logic.process import ProcessManager
from app.process import InvoiceProcess


class Invoice(ProcessManager.bind_state_fields(status=InvoiceProcess), models.Model):
    status = models.CharField(choices=InvoiceProcess.states, max_length=16, blank=True)

    def __str__(self):
        return self.reference


class Order(models.Model):
    payment_status = models.CharField(max_length=16, blank=True)
