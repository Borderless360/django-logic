from django.db import models


class Invoice(models.Model):
    STATUS_DRAFT = 'draft'
    STATUS_SUCCESS = 'success'
    STATUS_FAILED = 'failed'
    STATUS_CANCELLED = 'cancelled'

    status = models.CharField(max_length=16, blank=True)
    customer_received = models.BooleanField(default=False)
    is_available = models.BooleanField(default=True)

    def __str__(self):
        return self.status


class InvoiceProxy(Invoice):
    """A proxy over Invoice. One table, one row, one status column, two
    class names — which is what the lock key has to see through."""

    class Meta:
        proxy = True


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE)


class MtiParent(models.Model):
    """Multi-table-inheritance parent. Exists so the binding collision check
    can be tested against a real MTI child, which is the shape that check
    wrongly rejected: a child binding the same process_name as its parent
    installs its OWN accessor, shadowing the parent's."""
    status = models.CharField(max_length=32, default='draft')


class MtiChild(MtiParent):
    review_status = models.CharField(max_length=32, default='draft')
    note = models.TextField(blank=True)


class InvoiceReceipt(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE)
    status = models.CharField(max_length=16, default='draft')
    note = models.CharField(max_length=32, blank=True)
    receipt_text = models.CharField(max_length=32, blank=True)
    computed_note = models.GeneratedField(
        expression=models.functions.Upper('note'), output_field=models.CharField(max_length=32), db_persist=True,
    )

    def save(self, *args, **kwargs):
        if 'note' in self.__dict__:
            self.receipt_text = self.note.upper()
        return super().save(*args, **kwargs)
