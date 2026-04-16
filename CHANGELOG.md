# Changelog

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
