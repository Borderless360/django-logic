# PR #75 Review: Release 0.2.0

> **Source:** https://github.com/Borderless360/django-logic/pull/75
> **Author:** Andrey Omelyanuk
> **Status:** Superseded by [PR #76](https://github.com/Borderless360/django-logic/pull/76) (release/0.2.0 with bug fixes)
> **Size:** +1,617 / −195 across 27 files (2 commits)
> **Note:** This is a historical review. All 9 bugs identified here have been fixed in PR #76.
>
> **⚠ Historical document.** Many APIs described below (`background_mode` /
> `run_in_background` kwargs, `Transition.get_task_kwargs`,
> `django_logic.utils.restore_action` / `restore_user_object` /
> `get_process_and_state`, `ignore_sources` parameter, `LogType`,
> `TransitionEventType.BACKGROUND_MODE`) were **removed** in 0.3.0
> as part of the Stage 2 rewrite around `django_logic.background`.
> See `docs/PLAN.md` §4 for the current design and `CHANGELOG.md` for
> the 0.3.0 removal list.

---

## Summary

This is a major release that refactors core APIs, incorporates production
lessons from the GV project, and adds infrastructure for background task
execution. It addresses many issues we identified in the GV codebase
(race conditions, `set_state` using `update()` vs `save()`, lock timeouts,
structured logging) and provides hooks for the `BackgroundTransition` pattern.

---

## Breaking Changes

| Change | Impact | GV Compatibility |
|--------|--------|------------------|
| Drop Python 3.6–3.10 (min 3.11) | Modernizes codebase | GV runs 3.12 — compatible |
| Remove `setup.py` → `pyproject.toml` | Modern packaging | Good |
| Remove `cached_state` property → `get_state()` | All `state.cached_state` refs must change | GV already uses `get_state()` pattern |
| `State.set_state()` uses `instance.save(update_fields=...)` instead of queryset `update()` | Triggers model `save()` overrides | **Directly addresses GV's `FixedRedisState.set_state()` fix** — that workaround becomes unnecessary |
| `is_valid()` accepts `(instance, user)` not `(state, user)` | All custom conditions/permissions must update signatures | GV conditions already use `instance` |
| `Conditions.execute()` / `Permissions.execute()` accept `instance` not `state` | Same as above | Compatible |
| Lock check moved from `Transition.is_valid()` to `Process.get_available_transitions()` | Changes when locks are evaluated | GV's `QueueTransition.is_valid()` override already skips lock — now it's default behavior at the right level |

## New Features

### 1. FailureSideEffects

New command class that runs after side-effects fail but **before** the state
is unlocked. Execution order on failure:

```
set failed_state → failure_side_effects (locked) → unlock → failure_callbacks (unlocked)
```

**Assessment:** Good addition. GV doesn't have this, but it's a common need
for compensation logic that must run while the instance is still locked.

### 2. Background Mode (Two-Phase Protocol)

Built-in support for background transitions via `background_mode` kwarg:

- **Phase 1** (web process): lock state, set `in_progress_state`, call
  `run_in_background()` (raises `NotImplementedError` by default)
- **Phase 2** (worker): call `change_state()` with
  `background_mode_phase_2=True`, which skips the lock and runs side effects

```python
class BackgroundTransition(Transition):
    def run_in_background(self, state, **kwargs):
        task_kwargs = self.get_task_kwargs(state, **kwargs)
        run_transition_task.delay(**task_kwargs)
```

**Assessment:** This is the hook we need. It standardizes the pattern GV
implemented in `django_logic_ext.transitions.BackgroundTransition` but at the
framework level. However, it requires the caller to pass `background_mode=True`
— we should consider making it automatic (the transition class itself should
determine whether to run in background, not the caller).

### 3. Transition Context Propagation

UUID-based `tr_id` / `root_id` / `parent_id` via thread-safe `ContextVar`:

```
root transition (tr_id=A, root_id=A, parent_id=A)
  └─ nested transition (tr_id=B, root_id=A, parent_id=A)
       └─ nested transition (tr_id=C, root_id=A, parent_id=B)
```

**Assessment:** Excellent. GV's `gv/django_logic.py` and
`django_logic_ext/transitions.py` already manually propagate `tr_id` /
`root_id` via kwargs. This makes it automatic and thread-safe.

### 4. Structured Logging

Two standard Python loggers:
- `django-logic` — general library activity
- `django-logic.transition` — structured transition event log with
  `TransitionEventType` enum (Start, Complete, Fail, SideEffect, Callback,
  FailureSideEffect, SetState, Lock, Unlock, NextTransition, BackgroundMode)

**Assessment:** Matches what GV built with `transition_logger`. The format
(`tr_id EventType ...args`) is identical to GV's production logs.

### 5. RedisState Rewrite

Single Redis key for both locking and state storage:
- Key existence = locked; value = current state
- `lock()` stores current state with TTL (atomic `nx=True`)
- `set_state()` overwrites key value + persists to DB (resets TTL)
- `get_state()` reads from key (falls back to instance attr when unlocked)
- `unlock()` deletes key; DB becomes source of truth again
- Configurable `LOCK_TIMEOUT` via `DJANGO_LOGIC['LOCK_TIMEOUT']` (default 7200s)

**Assessment:** This is the fix for the race condition documented in
`docs/race-condition-issue`. The specific Git commit GV pins to
(`be9d1018...`) is incorporated here.

### 6. Utility Functions (`django_logic.utils`)

- `restore_user_object()` — restores user from `user_id` in kwargs
- `get_process_instance()` — gets process from model or `process_class` path
- `get_process_and_state()` — loads instance + process from serialized kwargs
- `restore_action()` — restores action from serialized kwargs

**Assessment:** These are exactly the utilities GV had to build in
`django_logic_ext/tasks.py`. Good to have them in core.

### 7. Other Changes

- `Transition.get_task_kwargs()` — serializes transition context for
  background task dispatch
- `Process.get_transition_by_action_name()` — new public method
- `get_available_transitions(ignore_state=True, ignore_sources=True)` —
  skip lock check and source-state check when needed
- `Process.__getattr__` strips stale `action_name` from kwargs
- `NextTransition` errors isolated — no longer crash the main transition
- Root transition exceptions caught and logged (not re-raised) — returns `tr_id`

---

## Bugbot Issues (9 found, rated by severity)

### High Severity (3)

1. **Lock not released when `run_in_background()` raises** — If the
   background dispatch fails, the state stays locked until TTL expiry.
   The code does call `fail_transition` (which unlocks), but then re-raises,
   which may bypass cleanup in the caller.

2. **Background mode phase 2 can't find transition** — After phase 1 sets
   `in_progress_state`, phase 2 calls `get_available_transitions()` which
   checks `state in transition.sources`. Since the state is now
   `in_progress_state` (not in `sources`), the transition isn't found.
   The `ignore_sources` parameter exists to solve this, but `restore_action()`
   already passes `ignore_sources=True` — need to verify the full path.

3. **Swallowed exceptions in `change_state`** — The `raise` after
   `fail_transition` in `SideEffects.execute()` is commented out. If
   `complete_transition` raises, the exception is caught but not re-raised,
   leaving the caller unaware of failure.

### Medium Severity (3)

4. **NextTransition skips lock via inherited background kwargs** — Parent's
   `background_mode_phase_2=True` leaks into next-transition kwargs, causing
   `skip_lock=True`. Fixed by stripping background keys in
   `NextTransition.execute()`.

5. **`self.instance` vs `self.state.instance`** — Error path in
   `get_transition_by_action_name` uses `self.instance` which may be `None`.

6. **Background mode string vs UUID comparison** — `get_task_kwargs`
   serializes UUIDs to strings, but `skip_lock` compares them. Works in the
   Celery path but could break with direct UUID kwargs.

### Low Severity (3)

7. **`dict.update()` returns None** — `log_data = state.get_log_data().update(...)`
   assigns `None` to `log_data`.

8. **Duplicate deprecated logging** — `get_transition_by_action_name` and
   `_get_transition_method` both log the same "executes transition" message.

9. **RedisState lock invisible when state value is None** — If the state
   field value is `None`, `lock()` stores `None` but `is_locked()` returns
   `False` (can't distinguish "key with None value" from "key doesn't exist").

---

## TODO.md (included in PR)

The PR includes a `TODO.md` with planned 0.3.0 work:
- Remove deprecated legacy logging
- Move DRF to optional dependency
- Move logging from Transition to State methods
- Consider `next_transition` via callbacks
- Add UUID generation for `Action.change_state`
- Revisit root transition exception swallowing
- Set up automated PyPI publishing
- Move demo to separate repo

---

## Assessment & Decisions Needed

### What this PR gets right
- Incorporates almost all production fixes from GV (`set_state` via `save()`,
  RedisState rewrite, context propagation, structured logging, utility functions)
- Background mode hook is the right abstraction level
- `FailureSideEffects` is a good addition
- Infrastructure modernization (pyproject.toml, GitHub Actions, Docker) is solid
- 100% test coverage

### Concerns to resolve before merging

1. **Root exception swallowing** — The PR catches root transition exceptions
   and returns `tr_id` instead of raising. This breaks `try/except
   TransitionNotAllowed` patterns shown in the README. The TODO.md
   acknowledges this. We need to decide: raise or return?

2. **`SideEffects.execute()` commented-out raise** — The `# raise` after
   `fail_transition` means side-effect failures are silently swallowed at
   the root level. This is a significant behavioral change.

3. **Bugbot high-severity issues** — Items 1–3 above need fixes before merge.

4. **`background_mode` as caller kwarg vs class attribute** — Currently the
   caller must pass `background_mode=True`. Our plan calls for
   `BackgroundTransition` where it's automatic. The hook supports both
   patterns but the API should be cleaner.

5. **No `BackgroundTransition` / `BackgroundAction` in core** — The PR
   provides the hook (`run_in_background`) but doesn't ship the actual
   classes. Our plan requires these as first-class primitives.

6. **No MQTransition / DB-backed queue** — The PR doesn't incorporate the
   `django_logic_ext` MQ pattern. This is the most production-proven
   approach in GV. We need to decide if this goes into 0.2.0 or 0.3.0.

### Recommendation

**Do not merge as-is.** The PR is a strong foundation but needs:

1. Fix the 3 high-severity bugbot issues
2. Decide on exception handling strategy (raise vs swallow)
3. Consider whether to ship `BackgroundTransition` / `BackgroundAction`
   classes in this release or the next
4. Evaluate whether MQ pattern from `django_logic_ext` should be included

The PR aligns well with Phase 1 of our PLAN.md — it handles most of the
"Project Modernization" and "New Primitives" groundwork. The remaining gap
is the actual `BackgroundTransition` / `BackgroundAction` classes and the
Celery backend integration.
