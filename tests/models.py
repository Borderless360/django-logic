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


class MtiParent(models.Model):
    """Multi-table-inheritance parent. Exists so the binding collision check
    can be tested against a real MTI child, which is the shape that check
    wrongly rejected: a child binding the same process_name as its parent
    installs its OWN accessor, shadowing the parent's."""
    status = models.CharField(max_length=32, default='draft')


class MtiChild(MtiParent):
    extra = models.CharField(max_length=32, blank=True)
