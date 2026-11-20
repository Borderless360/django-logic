"""Fairness group turn order shared by workers serving the same database.

The cursor is locked only while selecting a job. It does not limit how
many jobs a fairness group can run, and is never held while doing business work.
"""
import time

from django.db import connections, models, transaction
from django.db.models import Exists, F, OuterRef
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from django_logic.database import set_local_timeouts


class FairnessGroup(models.Model):
    key = models.CharField(max_length=255, primary_key=True, blank=True)
    last_served_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'django_logic_background'


def claim_fair(rows):
    """Choose work ready earliest for the least recently served fairness group.

    Unkeyed jobs share the empty group. New jobs keep submission order.
    Deferred jobs follow work ready before their new due time.
    Locked or deferred jobs cannot prevent another fairness group from starting.
    A bounded scan leaves the worker free to check running job deadlines.
    """
    alias = rows.db
    groups = FairnessGroup.objects.using(alias)
    with transaction.atomic(using=alias):
        # Only the selection queries have these limits, never business work.
        # Preserve a stricter database setting when one is already configured.
        if connections[alias].vendor == 'postgresql':
            with connections[alias].cursor() as cursor:
                set_local_timeouts(cursor, statement_timeout=1000, lock_timeout=100)
        started = time.monotonic()
        return _claim_in_transaction(rows, groups, started)


def _claim_in_transaction(rows, groups, started):
    # Workers create fairness group turn records when they first select that fairness group's queued work.
    missing = list(rows.exclude(
        fairness_key__in=groups.values('key'),
    ).order_by().values_list('fairness_key', flat=True).distinct()[:128])
    if missing:
        groups.bulk_create(
            [FairnessGroup(key=key) for key in missing],
            ignore_conflicts=True,
        )
    eligible_groups = groups.filter(Exists(
        rows.filter(fairness_key=OuterRef('key')),
    )).order_by(F('last_served_at').asc(nulls_first=True), 'key')
    examined = []
    for index in range(32):
        if index and time.monotonic() - started > 0.25:
            return None
        group = eligible_groups.exclude(key__in=examined).select_for_update(
            skip_locked=True,
        ).first()
        if group is None:
            return None
        examined.append(group.key)
        # An older job waiting for newer work must not keep taking its turn.
        # A missing or earlier due time preserves the job's creation order.
        pk = rows.filter(fairness_key=group.key).order_by(
            Greatest('created', Coalesce('eligible_at', 'created')), 'created', 'pk',
        ).select_for_update(skip_locked=True).values_list('pk', flat=True).first()
        # An unavailable group's turn also advances. Otherwise many
        # long-running groups could occupy every bounded scan forever.
        groups.filter(key=group.key).update(last_served_at=timezone.now())
        if pk is not None:
            return pk
    return None
