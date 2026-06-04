# Changelog

## [0.3.0]

### Breaking Changes

- **Removed `django_logic.constants` and the `LogType` enum.** All state-change logging now flows through the standard `django-logic` / `django-logic.transition` Python loggers.
- **Removed the legacy logger abstraction.** `AbstractLogger`, `DefaultLogger`, `NullLogger`, `get_logger()`, `DJANGO_LOGIC_DISABLE_LOGGING`, `DJANGO_LOGIC_CUSTOM_LOGGER` are gone. Configure logging through Django `LOGGING` as you would for any other library.
- **Removed `Transition.run_in_background()` / `background_mode` / `background_mode_phase_2` kwargs.** The new `BackgroundTransition` class owns background dispatch end-to-end; there is no per-call opt-in on the base `Transition`.
- **Removed the in-tree `demo/` app** (moved to the separate [django-logic-demo](https://github.com/Borderless360/django-logic-demo) project).
- **DRF moved to an optional dependency** (`pip install django-logic[drf]`). The core library no longer imports Django REST Framework.
- **`in_progress_state` must be unique within a `Process`.** Declaring two transitions on the same process with the same `in_progress_state` now raises `ImproperlyConfigured` at class-creation time.

### New Features — `django_logic.background`

- **`BackgroundTransition` and `BackgroundAction`** — durable, queue-routed background execution with DB persistence (`TransitionMessage`), partial-unique concurrency guard, automatic retry, and a single-task execution model. All side-effects plus the target-state write happen inside one `acks_late=True` Celery task, inside one atomic block.
- **Two execution modes** — `DJANGO_LOGIC['BACKGROUND_EXECUTION']` selects `'celery'` (production) or `'sync'` (tests, management commands, Django shell). Sync mode runs phase 2 inline in the same process, bypasses `transaction.on_commit`, and propagates exceptions to the caller — no Celery broker required for tests.
- **`sync_execution()` context manager** — force Sync mode for a block of code regardless of the global setting.
- **`retry_pending()`** — run the periodic safety-net task once inline, useful for tests that want to simulate "time passed".
- **Explicit queue routing, no default.** Every `BackgroundTransition` must declare `queue='...'`. Missing `queue=` raises `ImproperlyConfigured`. The periodic safety-net tasks run on `DJANGO_LOGIC['STARTER_QUEUE']`.
- **Periodic safety-net tasks** — `retry_stale_transitions`, `cleanup_completed_transitions`, `detect_stuck_transitions`, and `watchdog_stale_attempts`. `retry_stale_transitions` skips rows whose current attempt started within `RETRY_MINUTES` (no per-tick re-dispatch flood while an attempt is in flight).
- **Per-attempt timeouts** — `BackgroundTransition(timeout=<seconds>)` declares a wall-clock budget per phase-2 attempt, persisted as `TransitionMessage.timeout_seconds`. The new `watchdog_stale_attempts` periodic task records a synthetic `TimeoutError` for attempts that exceed it and finalizes the row to `failed_state` once `errors_count` reaches `MAX_ERRORS`. Rows without `timeout` are not watched.
- **Primary-key-agnostic background path** — `TransitionMessage.instance_id` is stored as text (`str(instance.pk)`), so background transitions work with `UUIDField`, `CharField`, and `BigAutoField` primary keys beyond `2**31-1`, matching the synchronous core (migration `0005`).
- **kwargs serialization** — built-in handling of `request`, `user` → `user_id`, `UUID` → `str`, `datetime`/`date` → `.isoformat()`; unserializable values are rejected at phase 1 rather than phase 2.

### Observability

- **`TransitionMessage` timing fields** — `started_at`, `completed_at`, `duration_ms`. `started_at` is (re)written at the top of every phase-2 attempt so a watchdog can scan `is_completed=False AND started_at < cutoff` to find hung attempts. `completed_at` is set once when the row is marked completed (success or terminal failure); `duration_ms` measures the last attempt only. Backed by a new `dl_bg_started_idx` index on `(is_completed, started_at)`.

### Bug Fixes

- **Unrestorable `TransitionMessage` rows now stop retrying.** If phase 2 can't restore the instance, process, or transition (e.g. the model was uninstalled, the transition renamed), the TM is now marked `is_completed=True` in its own statement, outside the failed atomic block. Previously the `mark_as_completed()` call was rolled back along with the atomic block, so the periodic starter would re-dispatch the same unrestorable row every `RETRY_MINUTES` forever.
- **Retry safety-net now respects the execution mode.** `_retry_pending_inline` (`retry_stale_transitions` / `retry_pending()`) ran phase 2 inline only via the no-Celery shim; with Celery installed it always called `apply_async`, so in Sync mode a stale row was published to a broker nobody consumes and never retried. It now runs phase 2 inline in Sync mode and re-dispatches via `apply_async` (to the row's own queue) in Celery mode, mirroring `dispatch_transition`.
- **`failure_callbacks` now fire for safety-net-finalized rows.** Rows finalized by `detect_stuck_transitions` / `watchdog_stale_attempts` previously ran `failed_state` + `failure_side_effects` but never `failure_callbacks`, unlike the in-task terminal path. They now run (best-effort, after the finalizing transaction commits) so terminal-failure semantics are identical regardless of which path finalizes the row.
- **`Action.fail_transition` no longer unlocks a lock it never acquired.** A synchronous `Action` skips locking on success but inherited `Transition.fail_transition`'s unconditional `state.unlock()`; a failing `Action` could therefore release the lock a concurrent `Transition` on the same instance/field held (and discard `RedisState`'s cached state). `Action` now has a symmetric, non-unlocking failure path.
- **Background `context` kwarg restored in phase 2.** Side-effects declared as `fn(instance, context, **kwargs)` (the documented signature) raised in background mode because `context` was dropped at phase 1 and never rebuilt. Phase 2 now rebuilds `context={}` like the synchronous path.
- **Phase-1 `IntegrityError` no longer always reported as `AlreadyInProgress`.** The `TransitionMessage` is created before the `in_progress_state` write, so only its partial-unique violation maps to `AlreadyInProgress`; a constraint error from the user's own model write now surfaces as the real `IntegrityError`.
- **Terminal background failures no longer re-raise out of the Celery task.** The phase-2 re-raise is now Sync-mode only — in Celery mode the outcome is fully recorded on the row, so re-raising only spammed task-failure alerts and risked `acks_late` redelivery.
- **`duration_ms` is no longer inflated for safety-net-finalized rows** (it stays null when no real attempt ran), and `get_transition_by_action_name`'s not-found error now uses `instance.pk` (was `.id`, which raised `AttributeError` on custom-PK models).
- **Celery mode warns when no broker is configured.** `validate_on_ready` now logs a loud warning if the resolved `broker_url` is empty or `memory://` (messages would otherwise vanish into an in-memory transport), and `_reject_sqlite_in_celery_mode` checks only the alias `TransitionMessage` is routed to (a secondary SQLite alias on a Postgres-default deployment is no longer rejected).

### Internal cleanup

- **Removed `Transition.get_task_kwargs()`** — replaced by `django_logic.background.serializers.serialize_kwargs` + the `TransitionMessage.kwargs` JSONField.
- **Removed `django_logic.utils` module** (`restore_user_object`, `restore_action`, `get_process_instance`, `get_process_and_state`) — the durable single-task runner owns restoration end-to-end via its own `_restore()` helper.
- **Removed `ProcessManager.bind_state_fields()`**, `ProcessManager.save()`, and `ProcessManager.non_state_fields` (deprecated since 0.2.0).
- **Removed `Process.queryset_name`** and the `queryset_name` parameter on `State`. `State.get_db_state()` uses `model._default_manager` directly.
- **Removed the `ignore_sources` parameter** from `Process.get_available_transitions()` and `Process.get_transition_by_action_name()`. `Transition.__init__` already appends `in_progress_state` to `sources`, so a mid-flight instance finds its own transition without the escape hatch.
- **Removed `TransitionEventType.BACKGROUND_MODE`** and the `_BACKGROUND_MODE_KEYS` filter in `NextTransition` — both were part of the PR #75 fire-and-forget design, now gone.
- **`State.set_state()`** now calls `refresh_from_db(fields=[self.field_name])` instead of a full refresh, so in-memory mutations from side-effects survive the state write.

### Settings

```python
DJANGO_LOGIC = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'celery',  # or 'sync'
    'STARTER_QUEUE': 'django_logic.starter',  # required in Celery mode
    'TRANSITION_MESSAGE_MAX_ERRORS': 5,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}
```

### Dependencies

- `celery` is now an optional extra (`pip install django-logic[celery]`). The library imports cleanly without it; in Sync mode, Celery is not required at all.

---

## [0.2.0]

### Breaking Changes

- **Dropped Python 3.6–3.10 support.** Minimum required version is now Python 3.11.
- **Removed `setup.py` and `requirements.txt`** — packaging migrated to `pyproject.toml` (setuptools backend).
- **Removed `cached_state` property** from `State`. Use `get_state()` method instead.
- **`State.set_state()` now saves via `instance.save(update_fields=...)`** instead of a raw queryset `update()`, so custom `save()` methods on models are respected.
- **`is_valid()` signature changed** in `Transition` and `BaseTransition`: now accepts `(instance, user)` instead of `(state, user)`.
- **`Conditions.execute()` and `Permissions.execute()`** now accept `instance` instead of `state`.
- **Lock is no longer checked inside `Transition.is_valid()`**; the lock check moved to `Process.get_available_transitions()`.

### New Features

- **`FailureSideEffects`** — new command class and `failure_side_effects` parameter on `Transition`. These run after side-effects fail but _before_ the state is unlocked, allowing cleanup/compensation while the instance is still locked. Execution order on failure: set `failed_state` → failure side-effects → unlock → failure callbacks.
- **Background mode support** — `Transition.change_state()` now supports `background_mode` / `background_mode_phase_2` kwargs, with a `run_in_background()` hook (raises `NotImplementedError` by default, designed for [django-logic-celery](https://github.com/Borderless360/django-logic-celery) integration).
- **Transition context propagation** — each transition receives a unique `tr_id` (UUID). Root/parent IDs (`root_id`, `parent_id`) propagate through nested transitions via thread-safe `ContextVar`, enabling full traceability without explicit kwargs forwarding.
- **New structured logging system** — two standard Python loggers introduced:
  - `django-logic` — general library activity
  - `django-logic.transition` — structured transition event log with `TransitionEventType` enum (`Start`, `Complete`, `Fail`, `SideEffect`, `Callback`, `FailureSideEffect`, `SetState`, `Lock`, `Unlock`, `NextTransition`, `BackgroundMode`)
- **`State.get_state()` method** — reads the current state from the instance attribute (replaces the removed `cached_state` property).
- **`RedisState` rewrite** — now uses a single Redis key for both locking and state storage. The key's existence means locked; its value holds the current state. This makes state changes immediately visible across processes regardless of DB transaction isolation. Automatic TTL-based expiry prevents deadlocks on crashes.
- **Configurable lock timeout** — `DJANGO_LOGIC['LOCK_TIMEOUT']` setting (default: 7200 seconds / 2 hours) replaces the previous hardcoded ~3-year lock duration.
- **`Process.get_transition_by_action_name()`** — new public method to resolve a single transition by action name and user, with clear error handling.
- **`get_available_transitions()` now accepts `ignore_state`** parameter to skip the lock check when needed.
- **`Transition.get_task_kwargs()`** — helper method that serializes transition context (app_label, model_name, instance_id, action_name, etc.) for background task dispatch.
- **`django_logic.utils` module** — new utility functions:
  - `restore_user_object()` — restores user from `user_id` in kwargs
  - `get_process_instance()` — gets process instance from model or `process_class` path
  - `get_process_and_state()` — loads instance + process from serialized kwargs
  - `restore_action()` — restores action from serialized kwargs

### Bug Fixes

- **Root transition exception handling** — exceptions in the root transition are caught and logged instead of propagating to the caller (backward-compatible behavior). Nested transitions still propagate exceptions to parents.
- **`NextTransition` error isolation** — errors in next-transition execution are caught and logged, no longer crashing the main transition.
- **`__getattr__` on `Process`** now strips stale `action_name` from kwargs to prevent "multiple values for argument" errors when kwargs are forwarded from parent transitions.
- Fixed `RedisState.lock()` to store the actual state value (not just `True`) so `get_state()` can return it while locked.

### Deprecations

The legacy logging system is preserved but marked as **DEPRECATED** (will be removed in the next version):
- `LogType` enum in `constants.py`
- `AbstractLogger`, `DefaultLogger`, `NullLogger` classes and `get_logger()` function in `logger.py`
- `DJANGO_LOGIC_DISABLE_LOGGING` and `DJANGO_LOGIC_CUSTOM_LOGGER` settings
- All `self.logger` usage in commands, transitions, and process classes

### Infrastructure & CI

- **Migrated CI from Travis CI to GitHub Actions** (`.github/workflows/ci.yml`), testing on Python 3.11, 3.12, 3.13, 3.14.
- Added `Dockerfile` and `makefile` for local development (build, test, coverage, shell).
- Replaced `setup.py` + `requirements.txt` with `pyproject.toml`.
- Minimum dependencies: Django ≥ 4.0, django-model-utils ≥ 4.5.1, djangorestframework ≥ 3.14.0.

### Tests

- Added `tests/test_logger.py` — comprehensive tests for the new logging system (298 lines).
- Expanded `tests/test_state.py` — added tests for `RedisState`, `get_state()`, lock timeout behavior (+116 lines).
- Expanded `tests/test_transition.py` — added tests for `FailureSideEffects`, background mode, context propagation (+169 lines).
- Added `tests/utils.py` — shared test utilities (125 lines).
- Achieved 100% test coverage.

### Documentation

- Added `docs/logger.md` — documentation for the new structured logging system, including log format, Celery integration, and nested transition examples.
- Updated `README.md` — added CI/coverage badges, documented `failure_side_effects`, updated development setup instructions.
