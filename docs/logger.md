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

## Event types

Every transition-lifecycle line carries a `tr_id` in the message body so all
lines for one logical transition can be grepped together. The event vocabulary
is the `django_logic.logger.TransitionEventType` enum:

`Start`, `Complete`, `Fail`, `SideEffect`, `Callback`, `FailureSideEffect`,
`Set State`, `Lock`, `Unlock`, `Next Transition`.

### Transition log format

```
tr_id <Event> ...args
```

### Synchronous transition — happy path

```
tr_id Start ProcessName action_name instance_key root_id parent_id
tr_id Lock
tr_id Set State in_progress_state          (only if in_progress_state is declared)
tr_id SideEffect reserve_stock
tr_id SideEffect generate_labels           (a new SideEffect line means the previous one finished)
tr_id Set State target
tr_id Unlock
tr_id Callback send_confirmation_email
tr_id Complete
```

On failure the side-effect raises, `Fail` is logged, then `failure_side_effects`
run (before unlock), the state is set to `failed_state` (if declared), the lock
is released, and `failure_callbacks` run.

## Background transitions

A `BackgroundTransition` runs in two phases. Phase 1 (the synchronous call)
logs the `Start [background queue=...]` line, optionally `Set State
in_progress_state`, and the `TransitionMessage#<pk> created` line. Phase 2 (the
worker, or inline in Sync mode) logs `Phase2 Start`, the `SideEffect` lines,
`Set State target`, and `Complete`.

All side-effects **and** the target-state write run inside a single Celery task
(`acks_late=True`) — there is no per-callback Celery fan-out. There are no
`Celery`, `CeleryCallbacks`, or `Done` events; that was a pre-0.3.0 design.

```
tr_id Start ProcessName fulfil instance_key root_id parent_id [background queue=django_logic.critical]
tr_id Set State fulfilling
tr_id TransitionMessage#42 created (queue=django_logic.critical)
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
tr_a Lock
tr_a SideEffect invoke_inner

tr_b Start ProcessName inner instance_key root_id parent_a
tr_b Lock
tr_b Unlock
tr_b Complete

tr_a Unlock
tr_a Complete
```

## Future considerations

OpenTelemetry integration is under consideration.
