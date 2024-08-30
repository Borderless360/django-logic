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
