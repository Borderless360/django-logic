# Django-Logic Logging

State-change logging flows through two standard Python loggers. There is no
custom logger abstraction and no `DJANGO_LOGIC_*` logging settings (those
were removed in 0.3.0) — configure these loggers via Django `LOGGING` as you
would for any library.

## Loggers

- **`django-logic`** — general library activity (safety-net tasks, dispatch
  warnings, etc.). Available in code as `from django_logic.logger import logger`.
- **`django-logic.transition`** — the per-transition lifecycle event log.
  Available as `from django_logic.logger import transition_logger`.

### Configure them

```python
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "django-logic": {"handlers": ["console"], "level": "INFO"},
        "django-logic.transition": {"handlers": ["console"], "level": "INFO"},
    },
}
```

## Logging transition kwargs (and redacting PII)

The `Start` lifecycle line attaches the transition's kwargs to the log
record's `extra` (`extra={'kwargs': ...}`), and the callback-failure log
does too. Those kwargs can include a `user` object, the `request`, and
arbitrary business data (amounts, emails, tokens), so scrub them in your
logging configuration (a `logging.Filter` on the `django-logic.transition`
logger) if the deployment is privacy-sensitive.


## Event types

Every transition-lifecycle line carries a `tr_id` in the message body so all
lines for one logical transition can be grepped together. The event vocabulary
is the `django_logic.logger.TransitionEventType` enum:

`Start`, `Complete`, `Fail`, `SideEffect`, `Callback`,
`Set State`, `Lock`, `Unlock`, `Next Transition`.

### Transition log format

```
tr_id <Event> ...args
```

### Synchronous transition — happy path

```
tr_id Start ProcessName action_name instance_key root_id parent_id
tr_id Lock instance_key
tr_id Set State in_progress_state          (only if in_progress_state is declared)
tr_id SideEffect reserve_stock
tr_id SideEffect generate_labels           (a new SideEffect line means the previous one finished)
tr_id Set State target
tr_id Unlock instance_key
tr_id Callback send_confirmation_email
```

`Lock` and `Unlock` carry the `instance_key` (0.12.0, #188) so a per-instance
log filter shows the whole lock lifecycle — previously only `Start` named the
instance, so the *absence* of a `Lock` line was invisible without a `tr_id`
self-join.

A **failed acquisition is logged, at INFO, before the raise** (0.12.0, #188):

```
tr_id Lock failed instance_key — state is locked
```

Without it, a permanently frozen instance (leaked lock) and a healthy start
were indistinguishable: both emitted one `Start` line and nothing else. It is
INFO, not ERROR, because losing the lock race is an expected concurrency
outcome (#154) — the *pattern* of repeated `Lock failed` lines with no
interleaved `Unlock` for that instance is the leak signal. Under
`DEFER_UNLOCK_UNTIL_COMMIT` the release line reads
`Unlock instance_key deferred until commit`; a revalidation failure releases
with `Unlock instance_key after revalidation failure`.

(`Complete` is a background-only event — the synchronous path ends with the
`Unlock` + `Callback` lines.)

On failure the side-effect raises, `Fail` is logged, the state is set to
`failed_state` (if declared), the lock is
released, and `failure_callbacks` run.

## Background transitions

A `BackgroundTransition` runs in two phases. Phase 1 (the synchronous call)
logs the `Start [background queue=...]` line, then its critical section:
`Lock`, optionally `Set State in_progress_state`, the
`TransitionMessage#<pk> created` line, and `Unlock` (since 0.4 phase 1 holds
the state lock only for this section — the uncompleted row is the in-flight
marker afterwards). Phase 2 (the
worker, or inline in Sync mode) logs `Phase2 Start`, the `SideEffect` lines,
`Set State target`, and `Complete`.

All side-effects **and** the target-state write run inside a single Celery task
(`acks_late=True`) — there is no per-callback Celery fan-out. There are no
`Celery`, `CeleryCallbacks`, or `Done` events; that was a pre-0.3.0 design.

```
tr_id Start ProcessName fulfil instance_key root_id parent_id [background queue=django_logic.critical]
tr_id Lock instance_key
tr_id Set State fulfilling
tr_id TransitionMessage#42 created (queue=django_logic.critical)
tr_id Unlock instance_key
... worker picks up the task ...
tr_id Phase2 Start fulfil instance_key queue=django_logic.critical
tr_id SideEffect reserve_stock
tr_id Set State fulfilled
tr_id Complete
```

## Nested transitions

A transition can be invoked from inside another transition's side-effects or
callbacks. `root_id`/`parent_id` propagate through a thread-safe `ContextVar`
so nested transitions are observable as one logical chain, even when kwargs are
not explicitly forwarded.

```
tr_a Start ProcessName outer instance_key root_id parent_a
tr_a Lock instance_key
tr_a SideEffect invoke_inner

tr_b Start ProcessName inner instance_key root_id parent_a
tr_b Lock instance_key
tr_b Unlock instance_key
tr_b Complete

tr_a Unlock instance_key
tr_a Complete
```

## Future considerations

OpenTelemetry integration is under consideration.
