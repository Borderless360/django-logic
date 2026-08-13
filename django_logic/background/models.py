"""TransitionMessage — the durable record of an in-progress transition.

Every ``BackgroundTransition`` / ``BackgroundAction`` creates one row in
phase 1, atomically with the ``in_progress_state`` write on the target
instance. Phase 2 reads the row under ``select_for_update(nowait=True)``
and marks it completed at the end of a successful execution.

The partial unique constraint ``(app_label, model_name, instance_id,
process_name)`` where ``is_completed=False`` is the concurrency guard —
only one uncompleted message can exist per instance *per process* at a
time. Two processes bound to different state fields of the same model
are independent state machines and may both have background work in
flight.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import OperationalError, models, transaction
from django.utils import timezone
from model_utils.models import TimeStampedModel


#: Characters PostgreSQL text/jsonb columns cannot store, however happily
#: Python and SQLite carry them. An exception message that echoes back bytes,
#: scraped HTML or a CSV cell can contain either.
#: Ceiling for the text columns this module writes (``last_error_message``,
#: ``failure_side_effect_error``).
_TEXT_LIMIT = 10_000


def db_safe_text(value: str, limit: int = _TEXT_LIMIT) -> str:
    """Make ``value`` storable in a Postgres text column.

    NUL (U+0000) and lone surrogates are rejected by PostgreSQL, so writing an
    exception message that contains one raises ``DataError`` *from the
    accounting write itself* — which used to escape ``_handle_failure``, roll
    back ``errors_count`` and ``mark_as_completed`` with it, and leave the row
    retrying forever with the instance permanently blocked. The bookkeeping
    must never be the thing that fails, so the characters are escaped rather
    than passed through.
    """
    text = str(value)
    if '\x00' in text:
        text = text.replace('\x00', '\\x00')
    # Lone surrogates survive in Python strings (e.g. from surrogateescape
    # decoding) but cannot be encoded to UTF-8 for the wire.
    try:
        text.encode('utf-8')
    except UnicodeEncodeError:
        text = text.encode('utf-8', 'replace').decode('utf-8')
    return text[:limit]


class TransitionMessage(TimeStampedModel):
    is_completed = models.BooleanField(default=False)
    errors_count = models.PositiveIntegerField(default=0)
    last_error_dt = models.DateTimeField(blank=True, null=True)
    last_error_message = models.TextField(blank=True)

    # Records an exception swallowed while finalizing a terminal failure
    # (a rejected ``failed_state`` write) so it doesn't fail silently.
    # Separate from ``last_error_*`` which tracks the side-effect exception
    # that triggered the failure branch in the first place.
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
    # ``model._base_manager.get(pk=instance_id)`` (immune to filtered
    # default managers — issue #90), which coerces the string back
    # to the model's real pk type.
    instance_id = models.CharField(max_length=255)
    process_name = models.CharField(max_length=100)
    # The model field the process is bound to. Lets phase 2 reconstruct
    # the process from the recorded ``process_class`` without guessing
    # the field name when the model property has been renamed/rebound
    # between phases. Blank on rows created before 0.4.0.
    field_name = models.CharField(max_length=100, blank=True, default='')
    transition_name = models.CharField(max_length=100)
    # Dotted path of the (possibly nested) Process class that DECLARES the
    # transition. Phase 2 uses it to restore the EXACT background transition when
    # an ``action_name`` is shared across condition-disambiguated nested
    # processes (e.g. per-integration Gmail/Dummy sub-processes). It is recorded
    # for EVERY background transition started through the Process entrypoint —
    # for a transition on the bound process itself it equals the bound class
    # path; for a nested one it is the nested class path. Blank only on rows
    # created before this discriminator existed (pre-0.4.x) or, rarely, ones
    # enqueued outside the Process entrypoint; phase 2 then resolves by
    # ``transition_name`` (only when that name is unambiguous across the tree).
    #
    # TextField (not a length-capped CharField) to mirror the unbounded
    # ``process_class`` stored in ``kwargs``: a deeply-namespaced dotted path
    # must never overflow and abort phase 1. Never indexed — only read by pk in
    # phase-2 restore and compared for equality.
    owning_process_class = models.TextField(blank=True, default='')
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
            # One in-flight background transition per instance PER PROCESS.
            # Without process_name in the constraint, two independent state
            # machines on the same model (e.g. ``status`` and
            # ``payment_status``) would falsely conflict.
            models.UniqueConstraint(
                fields=['app_label', 'model_name', 'instance_id', 'process_name'],
                condition=models.Q(is_completed=False),
                name='dl_bg_one_uncompleted_per_process',
            ),
        ]

    def __str__(self) -> str:
        return (
            f'TransitionMessage#{self.pk} '
            f'{self.app_label}.{self.model_name}#{self.instance_id} '
            f'{self.transition_name} on {self.queue_name}'
        )

    @classmethod
    def in_flight_for(cls, instance, process_name: str):
        """The uncompleted rows for ``instance`` + ``process_name`` — the
        durable in-flight marker (#197).

        The ONE place the marker filter is written. The sync gate, the
        Action failure path, and the public ``in_flight()`` probe all read
        through it, so a future change to the marker's keying (the
        #184/#186 identity rework) changes it exactly once.
        """
        return cls.objects.filter(
            app_label=instance._meta.app_label,
            model_name=instance._meta.model_name,
            instance_id=str(instance.pk),
            process_name=process_name,
            is_completed=False,
        )

    LIVENESS_LIVE = 'live'
    LIVENESS_STRANDED = 'stranded'

    #: Grace between an attempt exhausting its declared budget and the
    #: watchdog abandoning it (which writes ``modified`` via record_error,
    #: putting the row back on the horizon clock).
    LIVENESS_SLACK = timedelta(minutes=5)

    @classmethod
    def in_flight_liveness(cls, instance, process_name: str):
        """``None`` (no uncompleted row), ``LIVENESS_LIVE``, or
        ``LIVENESS_STRANDED`` (#195) — one classification shared by the
        sync gate, phase 1's constraint rejection, and ``in_flight()``, so
        they can never disagree about the same row.

        Liveness signals, in order:

        * a running attempt inside its declared per-attempt budget
          (``started_at + timeout_seconds`` plus slack) is LIVE — the
          watchdog's own definition, so the gate cannot call an attempt
          stranded while the watchdog still calls it live;
        * otherwise a row whose newest activity (``modified``, refreshed at
          attempt start / on every recorded error, or ``started_at``) is
          within the retry horizon is LIVE;
        * past the horizon it is STRANDED: nothing has driven it for longer
          than the whole retry pipeline's span. This cannot distinguish a
          truly lost row from a queue backlogged for that long — the
          stranded message names both causes.
        """
        from django_logic.background import settings as bg_settings

        row = (
            cls.in_flight_for(instance, process_name)
            .order_by('-modified')
            .values('modified', 'started_at', 'timeout_seconds')
            .first()
        )
        if row is None:
            return None
        now = timezone.now()
        started, timeout = row['started_at'], row['timeout_seconds']
        if (
            started is not None and timeout is not None
            and now < started + timedelta(seconds=timeout) + cls.LIVENESS_SLACK
        ):
            return cls.LIVENESS_LIVE
        newest = max(t for t in (row['modified'], started) if t is not None)
        # The whole retry pipeline's span plus slack, floored so short
        # test/dev retry configs don't classify a fresh row as stale.
        horizon = max(
            bg_settings.retry_minutes() * (bg_settings.max_errors() + 1), 15,
        )
        if now - newest > timedelta(minutes=horizon):
            return cls.LIVENESS_STRANDED
        return cls.LIVENESS_LIVE

    @classmethod
    def stamp_attempt_started(cls, tm_id: int) -> bool:
        """Mark an attempt as beginning, in its own committed statement.

        Deliberately called from *outside* the attempt's ``atomic`` block
        (#179). ``started_at`` is the only field that must be visible to
        other connections *while* the attempt runs, and must survive the
        attempt rolling back:

        * A hung attempt holds its transaction open, so a ``started_at``
          written inside it is invisible to the watchdog — which filtered
          on ``started_at__isnull=False`` and therefore could never see the
          attempts it exists to abandon.
        * A crashed worker rolls its transaction back, taking the marker
          with it, so the crash was invisible too.

        The consequence was that the watchdog only ever matched rows whose
        attempt had already *committed* a failure — rows that had already
        charged themselves an error — and then charged them again on every
        tick. Written here it survives both cases, which is what makes
        ``timeout=`` mean anything.

        Durability is mode-dependent, as for
        ``runner._mark_unrestorable_completed``:

        * **Celery mode** — phase 2 is the top-level unit of work with no
          surrounding transaction, so this UPDATE autocommits and is visible
          to the watchdog immediately. Verified against real PostgreSQL from
          a second connection. This is the mode the watchdog exists for.
        * **Sync mode inside a caller's ``atomic()``** — it is part of the
          caller's transaction and invisible to other connections until the
          caller commits. Harmless: the phase-1 INSERT that created the row
          is in that same transaction, so on rollback there is no surviving
          row to abandon, and on commit the row and this marker become
          visible together. There is also no worker to abandon — the
          "attempt" is the caller's own thread, which a watchdog could not
          rescue anyway.

        Acquires the row lock with ``nowait`` first and gives up if another
        attempt holds it. That is not an optimisation — a bare ``UPDATE``
        here BLOCKS on PostgreSQL, and because this runs before
        ``_run_atomic``'s ``select_for_update(nowait=True)`` it defeated the
        whole skip-if-locked design: a duplicate dispatch waited out the
        entire live attempt and then ran it again, with no retry backoff.
        (``tests/background/test_concurrency_pg.py`` pins this.) A held row
        means an attempt is already live and has already stamped itself, so
        there is nothing to record and ``_run_atomic`` will skip anyway.

        Because a losing dispatcher never writes, ``started_at`` only ever
        moves forward, and ``duration_ms`` cannot absorb lock wait.

        Returns True if the marker was written.
        """
        now = timezone.now()
        try:
            with transaction.atomic():
                held = (
                    cls.objects
                    .select_for_update(nowait=True)
                    .filter(pk=tm_id, is_completed=False)
                    .exists()
                )
                if not held:
                    return False
                cls.objects.filter(pk=tm_id, is_completed=False).update(
                    started_at=now, modified=now,
                )
        except OperationalError:
            # Locked by a live attempt: it owns the marker.
            return False
        return True

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

    def mark_as_superseded(self, note: str) -> None:
        """Terminal outcome for a row whose instance was moved by something
        else (manual ops fix, external write) while the row was pending.

        The phase-2 state guard calls this instead of running side-effects:
        the row completes (so retries stop), the external state wins, and
        the reason is recorded on ``last_error_message`` for the audit
        trail. ``errors_count`` is NOT incremented — nothing failed.
        """
        self.last_error_message = db_safe_text(note)
        self.last_error_dt = timezone.now()
        self.save(update_fields=['last_error_message', 'last_error_dt', 'modified'])
        self.mark_as_completed(measure_duration=False)

    def record_error(self, exception: BaseException) -> None:
        # db_safe_text, not a bare slice: the accounting write must never be
        # the statement that fails (see its docstring).
        self.last_error_message = db_safe_text(exception)
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

    def record_failure_side_effect_error(
        self, exception: BaseException, *, label: str = '',
    ) -> None:
        """Record an exception raised while finalizing a terminal failure
        (a rejected ``failed_state`` write).

        Separate from ``record_error`` because the original side-effect
        error (which triggered the failure branch) must stay visible in
        ``last_error_message`` — we just annotate that the finalization
        also broke.

        **Appends** rather than replaces, so a second problem on the same
        row cannot silently erase the first. ``label`` names the source.
        """
        note = db_safe_text(f'{type(exception).__name__}: {exception}')
        if label:
            note = f'{label}: {note}'
        existing = self.failure_side_effect_error
        if existing:
            # Budget for the note FIRST: db_safe_text truncates the head, so
            # once the accumulated text approaches the limit, appending and
            # re-truncating silently dropped the note just added — the newest
            # and most relevant diagnostic. Trim the older text instead.
            room = _TEXT_LIMIT - len(note) - 2
            existing = existing[:room] if room > 0 else ''
        self.failure_side_effect_error = db_safe_text(
            f'{existing}; {note}' if existing else note
        )
        self.save(update_fields=['failure_side_effect_error', 'modified'])
