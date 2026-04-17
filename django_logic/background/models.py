"""TransitionMessage — the durable record of an in-progress transition.

Every ``BackgroundTransition`` / ``BackgroundAction`` creates one row in
phase 1, atomically with the ``in_progress_state`` write on the target
instance. Phase 2 reads the row under ``select_for_update(nowait=True)``
and marks it completed at the end of a successful execution.

The partial unique constraint ``(app_label, model_name, instance_id)``
where ``is_completed=False`` is the concurrency guard — only one
uncompleted message can exist per instance at a time.
"""
from __future__ import annotations

from django.db import models
from django.utils import timezone
from model_utils.models import TimeStampedModel


class TransitionMessage(TimeStampedModel):
    is_completed = models.BooleanField(default=False)
    errors_count = models.PositiveIntegerField(default=0)
    last_error_dt = models.DateTimeField(blank=True, null=True)
    last_error_message = models.TextField(blank=True)

    app_label = models.CharField(max_length=100)
    model_name = models.CharField(max_length=100)
    instance_id = models.PositiveIntegerField()
    process_name = models.CharField(max_length=100)
    transition_name = models.CharField(max_length=100)
    queue_name = models.CharField(max_length=100)
    kwargs = models.JSONField(blank=True, default=dict)

    class Meta:
        app_label = 'django_logic_background'
        indexes = [
            models.Index(
                fields=['is_completed', 'created'],
                name='dl_bg_incomplete_idx',
            ),
            models.Index(
                fields=['app_label', 'model_name', 'instance_id'],
                name='dl_bg_instance_idx',
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['app_label', 'model_name', 'instance_id'],
                condition=models.Q(is_completed=False),
                name='dl_bg_only_one_uncompleted_per_instance',
            ),
        ]

    def __str__(self) -> str:
        return (
            f'TransitionMessage#{self.pk} '
            f'{self.app_label}.{self.model_name}#{self.instance_id} '
            f'{self.transition_name} on {self.queue_name}'
        )

    def mark_as_completed(self) -> None:
        self.is_completed = True
        self.save(update_fields=['is_completed', 'modified'])

    def record_error(self, exception: BaseException) -> None:
        self.errors_count = (self.errors_count or 0) + 1
        self.last_error_message = str(exception)[:10_000]
        self.last_error_dt = timezone.now()
        self.save(
            update_fields=[
                'errors_count',
                'last_error_message',
                'last_error_dt',
                'modified',
            ]
        )
