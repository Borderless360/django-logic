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

    # Records exceptions swallowed by ``FailureSideEffects`` so broken
    # cleanup paths don't fail silently. Separate from ``last_error_*``
    # which tracks the side-effect exception that triggered the failure
    # branch in the first place.
    failure_side_effect_error = models.TextField(blank=True)

    # Phase-2 timing. ``started_at`` is (re)written at the top of every
    # phase-2 attempt, so on retry it reflects the *current* attempt —
    # a watchdog can scan ``is_completed=False AND started_at < cutoff``
    # to find hung attempts. ``completed_at`` is set once when the row
    # is marked completed (success or terminal failure). ``duration_ms``
    # measures the last attempt only; null if phase 2 never ran.
    started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    duration_ms = models.PositiveIntegerField(blank=True, null=True)

    app_label = models.CharField(max_length=100)
    model_name = models.CharField(max_length=100)
    # Stored as text (``str(instance.pk)``) rather than an integer so the
    # background path supports every primary-key type the synchronous core
    # already supports: BigAutoField PKs beyond 2**31-1, UUIDField, and
    # CharField primary keys. ``_restore`` looks the instance up with
    # ``model.objects.get(pk=instance_id)``, which coerces the string back
    # to the model's real pk type.
    instance_id = models.CharField(max_length=255)
    process_name = models.CharField(max_length=100)
    transition_name = models.CharField(max_length=100)
    queue_name = models.CharField(max_length=100)

    # Per-attempt timeout configured on ``BackgroundTransition(timeout=N)``.
    # Null = no watchdog for this row. Used by ``watchdog_stale_attempts``
    # to find attempts whose current run has exceeded their declared
    # wall-clock limit.
    timeout_seconds = models.PositiveIntegerField(blank=True, null=True)

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
            models.Index(
                fields=['is_completed', 'started_at'],
                name='dl_bg_started_idx',
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

    def mark_as_started(self) -> None:
        """Record the start of a phase-2 attempt.

        Called on every attempt, including retries — ``started_at`` is
        overwritten so the watchdog sees the current attempt's start,
        not the first one.
        """
        self.started_at = timezone.now()
        self.save(update_fields=['started_at', 'modified'])

    def mark_as_completed(self, measure_duration: bool = True) -> None:
        """Mark the row completed and (optionally) record ``duration_ms``.

        ``measure_duration`` must be ``False`` when the row is finalized by
        a safety-net task (watchdog / detect_stuck) rather than by an actual
        phase-2 attempt. In that case ``started_at`` belongs to an abandoned
        attempt that may be minutes or hours old, so ``now - started_at`` is
        the time-to-finalize, not an execution time — recording it as
        ``duration_ms`` would grossly inflate latency metrics. Leaving
        ``duration_ms`` null signals "no measured execution".
        """
        now = timezone.now()
        self.is_completed = True
        self.completed_at = now
        update_fields = ['is_completed', 'completed_at', 'modified']
        if measure_duration and self.started_at is not None:
            delta = now - self.started_at
            # Clamp to 0 to absorb clock skew; cap into PositiveIntegerField.
            ms = max(int(delta.total_seconds() * 1000), 0)
            self.duration_ms = ms
            update_fields.append('duration_ms')
        self.save(update_fields=update_fields)

    def record_error(self, exception: BaseException) -> None:
        self.last_error_message = str(exception)[:10_000]
        self.last_error_dt = timezone.now()
        # Increment on the DB side (F expression) rather than a
        # read-modify-write on a possibly-stale in-memory errors_count, so
        # two writers racing on the same row — e.g. the watchdog and a
        # reconnected zombie worker that lost its row lock — cannot lose an
        # increment. .update() bypasses auto_now, so set ``modified`` here.
        type(self).objects.filter(pk=self.pk).update(
            errors_count=models.F('errors_count') + 1,
            last_error_message=self.last_error_message,
            last_error_dt=self.last_error_dt,
            modified=self.last_error_dt,
        )
        # Reflect the committed value in memory so the caller's MAX_ERRORS
        # comparison sees the true count, not the stale snapshot.
        self.refresh_from_db(fields=['errors_count', 'modified'])

    def record_failure_side_effect_error(self, exception: BaseException) -> None:
        """Record an exception raised from ``failure_side_effects``.

        Separate from ``record_error`` because the original side-effect
        error (which triggered the failure branch) must stay visible in
        ``last_error_message`` — we just annotate that the cleanup path
        also broke.
        """
        self.failure_side_effect_error = (
            f'{type(exception).__name__}: {exception}'
        )[:10_000]
        self.save(update_fields=['failure_side_effect_error', 'modified'])
