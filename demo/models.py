from django.db import models

from demo.process import InvoiceProcess

from django_logic.process import ProcessManager


class Invoice(ProcessManager.bind_state_fields(status=InvoiceProcess), models.Model):
    status = models.CharField(choices=InvoiceProcess.states, default='draft', max_length=16, blank=True)
    customer_received = models.BooleanField(default=False)
    is_available = models.BooleanField(default=True)

    def __str__(self):
        return self.status


class Order(models.Model):
    payment_status = models.CharField(max_length=16, blank=True)
