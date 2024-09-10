from django.db import models

from demo.choices import LOCK_STATES


class Lock( models.Model):
    status = models.CharField(choices=LOCK_STATES, default=LOCK_STATES.open, max_length=16, blank=True)
    customer_received_notice = models.BooleanField(default=False)
    is_available = models.BooleanField(default=True)

    def __str__(self):
        return self.status
