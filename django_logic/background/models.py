"""TransitionMessage — the durable record of an in-progress transition.

Every ``BackgroundTransition`` / ``BackgroundAction`` creates one row
when it enqueues, atomically with the ``in_progress_state`` write on the
target instance. The worker reads the row under
``select_for_update(nowait=True)`` and marks it completed at the end of
a successful execution.

The partial unique constraint ``(app_label, model_name, instance_id,
process_name)`` where ``is_completed=False`` is the concurrency guard —
only one uncompleted message can exist per instance *per process* at a
time. Two processes bound to different state fields of the same model
are independent state machines and may both have background work in
progress.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import OperationalError, models, transaction
from django.utils import timezone
from model_utils.models import TimeStampedModel


#: Ceiling for the text columns this module writes (``last_error_message``,
#: ``failure_side_effect_error``).
_TEXT_LIMIT = 10_000


def db_safe_text(value: str) -> str:
    """Make ``value`` storable in a Postgres text column.

    NUL (U+0000) and lone surrogates are rejected by PostgreSQL, so an
    exception message that contains one raises ``DataError`` from the
    accounting write itself. The bookkeeping must never be the thing that
    fails, so the characters are escaped rather than passed through.
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
    return text[:_TEXT_LIMIT]


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

    # True when the row completed as a failure — retries exhausted, a
    # permanent failure, a restore that could not run, an unrestorable row.
    # False for a success and for a superseded row (the external state
    # change won; the instance is not parked). The cleanup sweep keeps the
    # newest failure row per instance and process, and this flag is what
    # tells it apart: ``errors_count`` cannot, because a permanent failure
    # completes at one error and a retried success can carry several.
    ended_in_failure = models.BooleanField(default=False)

    # Worker timing. ``started_at`` is (re)written at the top of every
    # attempt, so on retry it reflects the *current* attempt; the stuck
    # report and the retry classification read it. ``completed_at`` is
    # set once when the row is marked completed (success or terminal
    # failure). ``duration_ms`` measures the last attempt only; null if
    # the worker never ran.
    started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    duration_ms = models.PositiveIntegerField(blank=True, null=True)

    app_label = models.CharField(max_length=100)
    model_name = models.CharField(max_length=100)
    # Stored as text (``str(instance.pk)``) so the background path
    # supports every primary-key type the synchronous core already
    # supports: BigAutoField PKs beyond 2**31-1, UUIDField, and
    # CharField primary keys. ``_restore`` looks the instance up with
    # ``model._base_manager.get(pk=instance_id)`` (immune to filtered
    # default managers), which coerces the string back to the model's
    # real pk type.
    instance_id = models.CharField(max_length=255)
    process_name = models.CharField(max_length=100)
    # The model field the process is bound to. Lets the worker reconstruct
    # the process from the recorded ``process_class`` without guessing
    # the field name when the model property has been renamed or rebound
    # between enqueue and execute. Blank on rows created before 0.4.0.
    field_name = models.CharField(max_length=100, blank=True, default='')
    transition_name = models.CharField(max_length=100)
    # Dotted path of the (possibly nested) Process class that declared the
    # transition. Restore selects the exact background transition by this
    # class plus ``transition_name``. Blank on rows from before the column
    # existed (or enqueued outside the Process entrypoint); restore then
    # resolves by name alone, only when the name is unambiguous.
    # TextField: a deeply-namespaced dotted path must never overflow and
    # abort enqueue. Never indexed — read by pk, compared for equality.
    owning_process_class = models.TextField(blank=True, default='')
    queue_name = models.CharField(max_length=100)

    # Per-attempt budget configured on ``BackgroundTransition(timeout=N)``.
    # Null = unbounded. The worker reads it and kills an attempt process
    # that runs past it.
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
            # One uncompleted background transition per instance PER PROCESS.
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
    def instance_key(cls, instance, process_name: str) -> dict:
        """The instance + process keying, written in one place so a future
        change to it changes once."""
        return {
            'app_label': instance._meta.app_label,
            'model_name': instance._meta.model_name,
            'instance_id': str(instance.pk),
            'process_name': process_name,
        }

    @classmethod
    def for_instance(cls, instance, process_name: str):
        """Every row for ``instance`` + ``process_name``."""
        return cls.objects.filter(**cls.instance_key(instance, process_name))

    @classmethod
    def in_flight_for(cls, instance, process_name: str):
        """The uncompleted rows for ``instance`` + ``process_name``. The
        sync gate, the Action failure path, and the public ``in_flight()``
        probe all read through it."""
        return cls.for_instance(instance, process_name).filter(is_completed=False)

    RETRYING = 'retrying'
    STRANDED = 'stranded'

    @classmethod
    def worker_holds_row(cls, transition_message_id: int) -> bool:
        """Whether a worker attempt holds this row's lock right now.

        Asks with ``select_for_update(nowait=True)`` inside its own
        savepoint and gives up at once, so the probe never blocks and never
        keeps a lock. On SQLite the clause is dropped, so the answer is
        always False there — pull mode rejects SQLite at boot, and in
        sync mode the attempt runs in the caller's own thread.
        """
        try:
            with transaction.atomic():
                list(
                    cls.objects
                    .select_for_update(nowait=True)
                    .filter(pk=transition_message_id, is_completed=False)
                    .values_list('pk', flat=True)
                )
        except OperationalError:
            return True
        return False

    @classmethod
    def retry_status(cls, instance, process_name: str):
        """``None`` (no uncompleted row), ``RETRYING``, or ``STRANDED``.

        One classification shared by the sync gate, enqueue's constraint
        rejection, and ``in_flight()``, so they can never disagree about
        the same row.

        In order:

        * a row whose newest activity (``modified``, refreshed at
          attempt start / on every recorded error, or ``started_at``)
          is within the retry window is still being retried;
        * past the window, a row a worker still holds is still being
          retried: an attempt that runs quietly for longer than the
          window is slow, not lost. The probe is a savepointed
          ``select_for_update(nowait=True)`` that locks nothing; when it
          cannot answer (a poisoned connection, the database down), the
          time-based answer below stands;
        * past the window with no worker on the row it is stranded:
          nothing has retried it for longer than the whole retry
          pipeline's span. This cannot distinguish a truly lost row from
          a queue backlogged for that long — the stranded message names
          both causes.
        """
        from django_logic import conf

        row = (
            cls.in_flight_for(instance, process_name)
            .order_by('-modified')
            .values('pk', 'modified', 'started_at')
            .first()
        )
        if row is None:
            return None
        now = timezone.now()
        started = row['started_at']
        newest = max(t for t in (row['modified'], started) if t is not None)
        retry_window = conf.retry_window_minutes()
        if now - newest > timedelta(minutes=retry_window):
            try:
                if cls.worker_holds_row(row['pk']):
                    return cls.RETRYING
            except Exception:
                # The probe must never break the gate that asked. Unknown
                # means the time-based classification stands.
                pass
            return cls.STRANDED
        return cls.RETRYING

    @classmethod
    def stamp_attempt_started(cls, transition_message_id: int) -> bool:
        """Mark an attempt as beginning, in its own committed statement.

        Deliberately called from *outside* the attempt's ``atomic`` block.
        ``started_at`` is the only field that must be visible to other
        connections *while* the attempt runs, and must survive the attempt
        rolling back: a hung attempt holds its transaction open (a write
        inside it is invisible to other connections), and a crashed worker
        rolls the stamp back with it. Written here it survives both
        cases.

        Durability is mode-dependent:

        * **Pull mode** — the worker is the top-level unit of work with
          no surrounding transaction, so this UPDATE autocommits and is
          visible to other connections immediately.
        * **Sync mode inside a caller's ``atomic()``** — part of the
          caller's transaction, invisible until the caller commits.
          Harmless: the INSERT that created the row is in that same
          transaction, so the row and this stamp become visible (or roll
          back) together — and the "attempt" is the caller's own thread,
          which nothing else could rescue anyway.

        Acquires the row lock with ``nowait`` first and gives up if another
        attempt holds it. Not an optimisation: a bare ``UPDATE`` BLOCKS on
        the live attempt's row lock, defeating the skip-if-locked design.
        A held row means a live attempt has already stamped itself, so
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
                    .filter(pk=transition_message_id, is_completed=False)
                    .exists()
                )
                if not held:
                    return False
                cls.objects.filter(
                    pk=transition_message_id, is_completed=False
                ).update(
                    started_at=now, modified=now,
                )
        except OperationalError:
            # Locked by a live attempt: it owns the marker.
            return False
        return True

    def mark_as_completed(
        self, measure_duration: bool = True, *, ended_in_failure: bool = False,
    ) -> None:
        """Mark the row completed and (optionally) record ``duration_ms``.

        ``measure_duration`` must be ``False`` when the row is finalized by
        the stuck finalizer rather than by an actual worker attempt. In that case ``started_at`` belongs to an abandoned
        attempt that may be minutes or hours old, so ``now - started_at`` is
        the time-to-finalize, not an execution time — recording it as
        ``duration_ms`` would grossly inflate latency metrics. Leaving
        ``duration_ms`` null signals "no measured execution".

        ``ended_in_failure`` is True on every terminal-failure path, so the
        cleanup sweep can keep the newest failure row per instance.
        """
        now = timezone.now()
        self.is_completed = True
        self.completed_at = now
        update_fields = ['is_completed', 'completed_at', 'modified']
        if ended_in_failure:
            self.ended_in_failure = True
            update_fields.append('ended_in_failure')
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

        The worker's state guard calls this instead of running side-effects:
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
        # two writers racing on the same row — e.g. the stuck finalizer
        # and a reconnected zombie worker that lost its row lock — cannot
        # lose an increment. .update() bypasses auto_now, so set ``modified`` here.
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
            # Budget for the new note FIRST: appending then re-truncating
            # would drop the newest (most relevant) note near the limit —
            # trim the older text instead.
            room = _TEXT_LIMIT - len(note) - 2
            existing = existing[:room] if room > 0 else ''
        self.failure_side_effect_error = db_safe_text(
            f'{existing}; {note}' if existing else note
        )
        self.save(update_fields=['failure_side_effect_error', 'modified'])


def in_flight(instance, process_name: str = 'process') -> bool:
    """Whether a background transition is still being retried for
    ``instance`` + ``process_name``.

    For shaping answers at API seams ("busy, try again shortly"), NOT as
    a pre-flight gate: the read is racy — a transition can start or
    complete between this call and whatever the caller does next. The
    engine's own guards stay authoritative. A stranded row (nothing is
    retrying it) answers ``False``, so a consumer answering 409 on this
    probe and 400 on plain ``TransitionNotAllowed`` stays consistent.
    """
    return (
        TransitionMessage.retry_status(instance, process_name)
        == TransitionMessage.RETRYING
    )
