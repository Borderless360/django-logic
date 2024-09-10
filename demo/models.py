from django.db import models
from model_utils import Choices


LOCK_STATES = Choices(
    ('maintenance', 'Under maintenance'),
    ('locked', 'Locked'),
    ('open', 'Open'),
)


class Lock( models.Model):
    status = models.CharField(choices=LOCK_STATES, default=LOCK_STATES.open, max_length=16, blank=True)
    customer_received_notice = models.BooleanField(default=False)
    is_available = models.BooleanField(default=True)

    def __str__(self):
        return self.status
