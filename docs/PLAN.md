# Django Logic — Road to 1,000 Stars

> **HISTORICAL** — this is a snapshot of the v3 execution plan, superseded by
> the shipped 0.4–0.8 releases; see [CHANGELOG.md](../CHANGELOG.md) for what
> actually shipped and [INDEX.md](INDEX.md) for current documentation.
> Kept for context, not normative.

> Document-driven development plan.
> Status: **v3 — executing. Stage 1 complete.**

---

## Table of Contents

1. [Vision](#1-vision)
2. [Current State](#2-current-state)
3. [Stage 1 — Land PR #75 (v0.2.0)](#3-stage-1--land-pr-75-v020)
4. [Stage 2 — Durable BackgroundTransition (v0.3.0)](#4-stage-2--durable-backgroundtransition-v030)
5. [Stage 3 — Observability, DX & Testing (v1.0.0)](#5-stage-3--observability-dx--testing-v100)
6. [Stage 4 — Communication & Launch](#6-stage-4--communication--launch)
7. [Stage 5 — Community & Ecosystem](#7-stage-5--community--ecosystem)
8. [Resolved Decisions](#8-resolved-decisions)
9. [Success Metrics](#9-success-metrics)

---

## 1. Vision

Make Django Logic the go-to workflow/FSM library for Django by providing
clean, declarative state management that works synchronously out of the box
and asynchronously with a single import swap. Designed for AI/vibe-coding
from the ground up — the declarative Process definition is the spec, and
scenario-based tests verify it.

**Tagline:** *"Business logic belongs in processes, not in views."*

**Three pillars:**

1. **Declarative workflows** — Process classes that AI and humans can read,
   generate, and review without understanding framework internals.
2. **Production-grade background execution** — Durable BackgroundTransition
   with DB persistence, automatic retry, queue routing.
3. **Document-driven testing** — Scenario-based tests that read like business
   stories, with state snapshots for bug reproduction.

---

## 2. Current State

| Dimension | Status |
|-----------|--------|
| **Core package** | `django-logic` v0.2.0 (Stage 1 complete, PR #76) |
| **Legacy celery package** | `django-logic-celery` — removed from this workspace; superseded by Stage 2 |
| **Production consumer** | GV project — will receive the Stage 2 code vendored and migrate transition-by-transition |
| **Fundamental problem** | Documented in [`docs/recipes/nested-processes.md`](recipes/nested-processes.md) (the original external research note, `fundamental problem.md`, is not part of this repo); addressed by the Stage 2 single-task execution model |
| **GitHub stars** | 66 |

### What PR #75 already delivers

- pyproject.toml, GitHub Actions CI, Docker, 100% test coverage
- Python 3.11+ / Django 4.0+
- `State.set_state()` via `instance.save()` (fixes GV's `FixedRedisState`)
- `RedisState` rewrite (fixes race condition from `docs/race-condition-issue`)
- `FailureSideEffects` command class
- Background mode two-phase hook (`run_in_background()` / `get_task_kwargs()`)
- Context propagation (`tr_id` / `root_id` / `parent_id` via `ContextVar`)
- Structured logging (`django-logic.transition` logger + `TransitionEventType`)
- Utility functions (`restore_user_object`, `restore_action`, etc.)
- `Process.get_transition_by_action_name()`, `ignore_state`, `ignore_sources`

---

## 3. Stage 1 — Land PR #75 (v0.2.0) ✅ COMPLETE

> **Goal:** Get the foundation merged with bugs fixed and a clean release.
> **Depends on:** Nothing.
> **Delivers:** Stable 0.2.0 on PyPI.
> **Status:** All bug fixes applied. PR #76 open at https://github.com/Borderless360/django-logic/pull/76

### 1.1 Fix Bugbot high-severity issues

| # | Issue | Fix |
|---|-------|-----|
| 1 | Lock not released when `run_in_background()` raises | Already calls `fail_transition()` (which unlocks) before re-raise — verify the full path, add test |
| 2 | Phase 2 can't find transition after `in_progress_state` | `restore_action()` already passes `ignore_sources=True` — verify end-to-end, add test. Also adopt MQTransition's approach: `self.sources.append(self.in_progress_state)` in `__init__` |
| 3 | Swallowed exceptions (`# raise` in SideEffects) | Uncomment the `raise` — side-effect failures must propagate. Root exception swallowing in `Process` is a separate concern |

### 1.2 Fix medium/low severity issues

| # | Issue | Fix |
|---|-------|-----|
| 4 | NextTransition inherits `background_mode_phase_2` | Already stripped via `_BACKGROUND_MODE_KEYS` — verify test coverage |
| 5 | `self.instance` vs `self.state.instance` | Replace with `self.state.instance` in error path |
| 6 | String vs UUID comparison | Normalize to string in `skip_lock` check |
| 7 | `dict.update()` returns None | Split into two statements (matches rest of file) |
| 8 | Duplicate deprecated logging | Remove duplicate in `get_transition_by_action_name` |
| 9 | RedisState lock invisible for None state | Use sentinel value (e.g. `"__locked__"`) when state is None |

### 1.3 Resolve exception handling strategy

**Decision:** Root transitions **raise exceptions** (preserve backward
compatibility with `try/except TransitionNotAllowed`). The `tr_id` is
returned only on success. Remove the try/except wrapper in
`_get_transition_method` that swallows root exceptions.

### 1.4 Clean up deprecated code markers

Leave deprecated code in place for 0.2.0 (removal planned for 0.3.0),
but ensure deprecation warnings are emitted via `warnings.warn()`.

### 1.5 Release

- Fix all items above in the PR branch
- Run full test suite
- Merge PR #75
- Tag v0.2.0
- Publish to PyPI (manual for now; automated publishing in Stage 3)

---

## 4. Stage 2 — Durable BackgroundTransition (v0.3.0)

> **Goal:** Ship `BackgroundTransition` and `BackgroundAction` as the only
> way to run background work in Django Logic. DB-backed durability from
> day one. **Explicit queue per transition, no default.** Every transition
> survives worker crashes, broker failures, and deploys.
> **Depends on:** Stage 1 (v0.2.0 merged).
> **Delivers:** Production-grade background transitions, single-task
> execution model, internal testing helpers. This version will be copied
> into the GV project and integrated there before the old versions are
> removed.

### Why this design

Two forces shaped this stage:

1. **The fundamental problem** (see
   [`docs/recipes/nested-processes.md`](recipes/nested-processes.md); the
   original external research note is not part of this repo):
   nested `process.xxx()` calls inside side-effects created cascading
   `fail_transition` chains the moment `django-logic 0.2.0` started
   re-raising. The single-task execution model below sidesteps that class
   of problem entirely for background work — side-effects for one
   transition run inside one task, retried as a unit, with the state
   change owned by the task.
2. **Operational control.** Production experience: when *everything*
   defaults to a shared queue, a slow export eventually stalls fulfilment.
   You then scramble to re-route one transition at a time. The fix is
   upstream: **make every `BackgroundTransition` declare its queue. Fail
   loudly if it doesn't.**

The old fire-and-forget approach (PR #75's `run_in_background`, GV's
`BackgroundTransition`) is gone. The legacy `django-logic-celery`
chain-of-tasks approach is gone (the repo has been removed from this
workspace). The single-task model below replaces both.

### 2.1 Design principles

- **One task per transition.** All side-effects, the target-state write,
  and the TransitionMessage-completed write happen inside a single Celery
  task with `acks_late=True`. If the worker dies mid-execution, Celery
  re-delivers and the task runs from scratch. No chain-of-tasks, no
  dangling state.
- **Explicit queue, no default.** `BackgroundTransition(queue='...')` is
  required at declaration time. No `DJANGO_LOGIC['CELERY_QUEUE']`
  fallback. A transition without a queue is a configuration error.
- **`in_progress_state` is unique within a process.** Two transitions
  on the same `Process` cannot share an `in_progress_state`. Validated
  at class-creation time; duplicates raise `ImproperlyConfigured`.
  This removes the `ignore_sources` hack and makes state-to-transition
  lookup unambiguous.
- **Two execution modes — Celery (prod) and Sync (tests, shell, cron).**
  Phase 1 is identical either way. Phase 2 either runs in a worker via
  `apply_async` or inline in the same process. Tests never need a
  Celery broker. See §2.5.
- **DB is the source of truth.** The `TransitionMessage` row is the
  authoritative "this work needs to happen" record. Celery is the fast
  path; the periodic starter is the safety net that re-dispatches stale
  messages onto the **same queue they were declared on**.
- **Phase 1 is atomic.** `set_state(in_progress_state)` and
  `TransitionMessage.objects.create(...)` run in the same
  `transaction.atomic()`. In Celery mode, `transaction.on_commit`
  dispatches the task; in Sync mode, phase 2 runs inline after the
  block exits. If the request crashes before commit, everything rolls
  back.
- **Plain Python functions as side-effects.** Users never write Celery
  tasks for side-effects. They write `def f(instance, **kwargs): ...`.
- **Side-effects must be idempotent.** They will re-run from scratch on
  retry. This is the single most important user contract.
- **The transition class decides.** Callers do not pass
  `background_mode=True`. Using `BackgroundTransition` means it always
  runs in background.
- **Celery is an optional dependency** (`extras_require`). If it is
  not installed, Sync mode is the only available execution mode.

### 2.2 API

```python
# process.py
from django_logic import Process, Transition
from django_logic.background import BackgroundTransition, BackgroundAction

class OrderProcess(Process):
    transitions = [
        Transition(
            action_name='approve',
            sources=['draft'],
            target='approved',
            conditions=[has_stock],
            permissions=[is_staff],
            side_effects=[validate_order],
        ),
        BackgroundTransition(
            action_name='fulfill',
            sources=['approved'],
            target='fulfilled',
            in_progress_state='fulfilling',
            failed_state='fulfillment_failed',
            queue='django_logic.critical',
            side_effects=[reserve_stock, generate_labels, call_courier],
            callbacks=[send_confirmation_email],
        ),
        BackgroundTransition(
            action_name='generate_export',
            sources=['approved'],
            target='exported',
            in_progress_state='exporting',
            failed_state='export_failed',
            queue='django_logic.slow',
            side_effects=[build_csv, upload_to_s3],
        ),
        BackgroundAction(
            action_name='sync_inventory',
            sources=['fulfilled'],
            failed_state='sync_failed',
            queue='django_logic.fast',
            side_effects=[push_to_erp],
        ),
        Transition(
            action_name='cancel',
            sources=['draft', 'approved'],
            target='cancelled',
        ),
    ]

# apps.py — bind in AppConfig.ready() (the single supported binding site;
# binding at module import time creates a model→process→actions→model cycle,
# issue #100).
from django.apps import AppConfig
from django_logic import ProcessManager

class OrdersConfig(AppConfig):
    name = 'orders'

    def ready(self):
        from .models import Order
        from .process import OrderProcess
        ProcessManager.bind_model_process(Order, OrderProcess, state_field='status')
```

Omitting `queue=` raises `ImproperlyConfigured` at import time.

### 2.3 Execution flow

```
Phase 1 — web process (synchronous, fast):
  1. Validate conditions + permissions
  2. atomic {
       a. set_state(in_progress_state)               — DB + RedisState
       b. TransitionMessage.objects.create(
            app_label, model_name, instance_id,
            process_name, transition_name,
            queue_name=<transition.queue>,           — stored for retries
            kwargs=<serialized>,
          )
          (IntegrityError from the partial unique constraint →
           another transition already in progress → TransitionNotAllowed)
     }
  3. transaction.on_commit(
       lambda: run_background_transition.apply_async(
         args=[transition_message_id], queue=transition.queue
       )
     )
  4. Return tr_id to caller

Phase 2 — inside the single Celery task (acks_late=True):
  1. atomic {
       a. select_for_update(nowait=True) the TransitionMessage
          (OperationalError → another worker has it → exit silently)
       b. Restore instance, process, transition
       c. Run side-effects sequentially (plain Python)
       d. On success:
            • set_state(target)
            • message.mark_as_completed()
       e. On side-effect exception:
            • message.errors_count += 1; save last_error_*
            • If errors_count < MAX_ERRORS:
                leave message uncompleted → retried by periodic starter
                (state stays in in_progress_state)
            • If errors_count >= MAX_ERRORS:
                set_state(failed_state)
                message.mark_as_completed()
     }
  2. After commit (best-effort, outside atomic):
     • Callbacks (on success) or failure_callbacks (on failure)
     • next_transition (on success)

Safety net — periodic Celery task (retry_stale_transitions):
  - Scans uncompleted TransitionMessages older than RETRY_MINUTES
    with errors_count < MAX_ERRORS
  - Re-dispatches each one to its stored queue_name
  - This is how worker crashes, broker losses, and dropped
    on_commit callbacks all recover
```

### 2.4 Why "everything in one task" matters

The old `django-logic-celery` pattern dispatched a Celery chain: each
side-effect was its own task, with `complete_transition` as the final
task and `fail_transition` as an error handler. A worker crash between
side-effect N and side-effect N+1 left the model stuck in
`in_progress_state` with no recovery. Worse, it interacted badly with
the nested-transition problem documented in
[`docs/recipes/nested-processes.md`](recipes/nested-processes.md) (originally
analysed in an external research note that is not part of this repo).

In the new model:

- A worker crash at any point before the `atomic { ... }` block
  completes → DB rolls back → periodic starter re-dispatches → task
  re-runs from scratch → side-effects re-run (idempotent) → final state
  is written.
- A worker crash after `atomic { ... }` commits (i.e. during the
  best-effort callbacks phase) → state is correct, message is
  completed, only callbacks are lost. This is the documented
  best-effort boundary.

### 2.5 Execution modes

The same `BackgroundTransition` definition runs under one of two
execution modes, selected by setting or context.

```python
DJANGO_LOGIC = {
    'BACKGROUND_EXECUTION': 'celery',   # or 'sync'
    ...
}
```

**Celery mode** (production default when Celery is installed):

```
Phase 1 (web):
  atomic { set_state(in_progress_state); create TransitionMessage }
  transaction.on_commit(lambda:
      run_background_transition.apply_async(args=[tm.id], queue=tm.queue_name)
  )
  return tr_id

Phase 2:
  runs in a Celery worker on tm.queue_name
```

**Sync mode** (tests, management commands, shell, CI, local dev without
a broker):

```
Phase 1 (same process):
  atomic { set_state(in_progress_state); create TransitionMessage }
  # no on_commit dance — dispatch is immediate

Phase 2 (same process, same thread, right after):
  atomic { ... side-effects, state write, mark completed ... }
  callbacks / next_transition
  → exceptions raised by side-effects propagate to the caller
```

#### How to select the mode

| Situation | How |
|---|---|
| Production | `DJANGO_LOGIC['BACKGROUND_EXECUTION'] = 'celery'` (or omit, it's the default when Celery is installed) |
| Test suite (global) | `DJANGO_LOGIC['BACKGROUND_EXECUTION'] = 'sync'` in test settings |
| One test / block | `with django_logic.background.sync_execution(): ...` |
| Management command | `call_command(...)` inside `sync_execution()`, or set the global setting to `'sync'` for scripts |
| Django shell / REPL | Set `BACKGROUND_EXECUTION='sync'` in settings, or enter the context manager |
| Celery not installed | Sync mode is automatic; `'celery'` with no Celery raises `ImproperlyConfigured` at startup |

#### Why Sync mode is not just "`CELERY_TASK_ALWAYS_EAGER`"

- `task_always_eager` still requires importing Celery. Sync mode works
  with Celery uninstalled.
- `task_always_eager` runs the task through Celery's own machinery,
  including its serialization layer, which masks bugs where kwargs
  are not actually JSON-serializable. Sync mode uses the same
  dispatch path as Celery mode (kwargs are serialized into the
  TransitionMessage before phase 2 reads them), so serialization bugs
  are caught in tests.
- Sync mode bypasses `transaction.on_commit`, which never fires under
  Django's `TestCase` (wrapping transaction never commits). This is
  the single most common "why isn't my Celery task running in tests"
  gotcha — sync mode removes it entirely.

#### Differences between modes (by design)

| | Celery mode | Sync mode |
|---|---|---|
| Phase 1 return | Immediate, caller sees `tr_id` | Only returns after phase 2 completes |
| Side-effect exceptions | Logged + recorded on TM, caller already got 200 | Propagated to the caller |
| Retry on failure | Periodic starter re-dispatches | Not automatic (test outer transaction rolls back; management command is one-shot) |
| Worker isolation | Yes | No — same process |
| Concurrency guard | Partial unique constraint + select_for_update | Same (still enforced) |

### 2.6 BackgroundAction

`BackgroundAction` is the durable, queue-routed counterpart of the
plain `Action` class — runs side-effects in the background without
changing the model's state on success.

- **Uses the same TransitionMessage + retry machinery** as
  `BackgroundTransition`. Worker crashes recover automatically;
  concurrent requests are rejected by the partial unique constraint.
- **No `target`, no `in_progress_state`.** The model's state field
  doesn't change during the action. Concurrency is guarded purely by
  the TransitionMessage.
- **`queue=` is required**, same as `BackgroundTransition`.
- **Success path in phase 2:** run side-effects, `mark_as_completed()`.
  No `set_state`.
- **Failure at `MAX_ERRORS`:** if `failed_state=` was declared, write
  it; otherwise just mark completed. Failure callbacks run best-effort
  after the atomic block.

### 2.7 Reliability contract

```
GUARANTEED (survives any crash):
  - State reaches target OR failed_state
  - Side-effects are retried from scratch until success or max errors
  - No two workers run the same transition simultaneously
    (select_for_update + partial unique constraint)
  - queue_name is preserved across retries
  - Error count and last error are recorded in DB

BEST EFFORT (may be lost on worker crash):
  - Callbacks (run after state reaches target, outside the atomic block)
  - Failure callbacks (run after failed_state is set)
  - next_transition (triggered after callbacks)

REQUIREMENTS for users:
  - Side-effects MUST be idempotent
  - Critical work belongs in side-effects, not callbacks
  - If a callback MUST run, chain it as another BackgroundTransition
  - Every BackgroundTransition MUST declare queue=
  - in_progress_state MUST be unique within a Process
```

#### Known edge case — watchdog vs slow worker

When a ``BackgroundTransition`` declares ``timeout=N`` and the watchdog
task (``watchdog_stale_attempts``) runs:

- **Normal case.** The original worker holds the row's
  ``select_for_update`` lock. The watchdog's own
  ``select_for_update(nowait=True)`` fails with ``OperationalError``;
  it defers, and the running attempt finishes (success or terminal) on
  its own.
- **Race case.** The original worker has lost its DB connection (broker
  blip, long Python-side work, GC pause > idle timeout, etc.) but is
  still executing side-effects in-process. The row lock is released.
  The watchdog acquires the row, records a synthetic
  ``TimeoutError`` (``errors_count += 1``), re-dispatches, and either
  terminates the row at ``MAX_ERRORS`` or leaves it for the periodic
  starter.

  Meanwhile the original worker may complete its side-effects
  successfully. When it tries to ``set_state(target)`` /
  ``mark_as_completed()``, its atomic block either rolls back (no
  valid row lock) or hits a completed row and is a no-op. **The
  side-effects will have run twice.** This is safe per the "side-effects
  MUST be idempotent" requirement above; it is the price of catching
  hung workers without a second heartbeat channel.

The periodic starter (``retry_stale_transitions``) is subject to the
same race, minus the timeout trigger — a row held only in Python by a
worker with a dead DB connection looks identical to an abandoned row.
``RETRY_MINUTES`` sets the minimum window before re-dispatch; pick it
larger than your realistic worst-case side-effect duration plus one
broker round-trip.

### 2.8 TransitionMessage model

```python
class TransitionMessage(TimeStampedModel):
    is_completed = BooleanField(default=False)
    errors_count = PositiveIntegerField(default=0)
    last_error_dt = DateTimeField(blank=True, null=True)
    last_error_message = TextField(blank=True)

    # Phase-2 timing. started_at is overwritten on every attempt so a
    # watchdog can find hung attempts via (is_completed=False AND
    # started_at < cutoff). completed_at + duration_ms are written once,
    # when the row is marked completed (success or terminal failure).
    started_at = DateTimeField(blank=True, null=True)
    completed_at = DateTimeField(blank=True, null=True)
    duration_ms = PositiveIntegerField(blank=True, null=True)

    app_label = CharField(max_length=100)
    model_name = CharField(max_length=100)
    instance_id = PositiveIntegerField()
    process_name = CharField(max_length=100)
    transition_name = CharField(max_length=100)
    queue_name = CharField(max_length=100)      # required, no blank
    kwargs = JSONField(blank=True, default=dict)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=['app_label', 'model_name', 'instance_id'],
                condition=Q(is_completed=False),
                name='only_one_uncompleted_transition_per_instance',
            )
        ]
        indexes = [
            Index(fields=['is_completed', 'created']),
            Index(fields=['app_label', 'model_name', 'instance_id']),
            Index(fields=['is_completed', 'started_at']),  # watchdog
        ]
```

#### Unrestorable rows

If phase 2 can't resolve the `(model, instance, process, transition)`
tuple (model uninstalled, transition renamed, etc.) the runner marks
the row `is_completed=True` in its own UPDATE statement, **outside**
the atomic block whose rollback would otherwise discard the mark.
Without this hoist the periodic starter would re-dispatch the same
unrestorable row every `RETRY_MINUTES` forever.

### 2.9 Periodic safety-net tasks

| Task | Frequency | Queue | Purpose |
|------|-----------|-------|---------|
| `retry_stale_transitions` | Every 2 min | `DJANGO_LOGIC['STARTER_QUEUE']` (required) | Re-dispatch uncompleted messages older than `RETRY_MINUTES` to their own `queue_name` |
| `cleanup_completed_transitions` | Daily | `DJANGO_LOGIC['STARTER_QUEUE']` | Delete completed messages older than `CLEANUP_DAYS` |
| `detect_stuck_transitions` | Every 5 min | `DJANGO_LOGIC['STARTER_QUEUE']` | Log/alert on messages at `MAX_ERRORS` |

The periodic tasks themselves run on `STARTER_QUEUE`, but each
re-dispatched transition goes back to its own `queue_name`. A slow
export that failed is retried on the slow queue, never on the
critical queue.

(Periodic tasks are not meaningful in Sync mode — no worker pool to
re-dispatch to. Sync users schedule retries themselves or let test
transactions roll back.)

### 2.10 Settings

```python
DJANGO_LOGIC = {
    'LOCK_TIMEOUT': 7200,

    # 'celery' (default when Celery is installed) or 'sync'.
    # 'sync' runs phase 2 inline in the same process — used in tests,
    # management commands, and the Django shell.
    'BACKGROUND_EXECUTION': 'celery',

    # Required when BACKGROUND_EXECUTION='celery'. No default.
    # A transition cannot start without a queue, and the framework's
    # periodic tasks need their own explicit queue.
    'STARTER_QUEUE': 'django_logic.starter',

    'TRANSITION_MESSAGE_MAX_ERRORS': 5,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}
```

There is no `CELERY_QUEUE` default. Every `BackgroundTransition`
declares `queue=`; every framework task has an explicit queue in
`DJANGO_LOGIC`.

### 2.11 Suggested queue layout (guidance, not framework default)

```
django_logic.fast       — quick tasks (<1s): notifications, cache updates
django_logic.critical   — user-facing with SLA: fulfilment, payments
django_logic.slow       — long tasks (>30s): exports, reports
django_logic.starter    — periodic safety-net tasks
```

Users configure Celery workers per queue, matching worker resources to
task profile:

```bash
celery -A myapp worker -Q django_logic.fast -c 8 --prefetch-multiplier 4
celery -A myapp worker -Q django_logic.slow -c 1 --prefetch-multiplier 1
celery -A myapp worker -Q django_logic.critical -c 4 --prefetch-multiplier 1
celery -A myapp worker -Q django_logic.starter -c 1
```

### 2.12 kwargs serialization

Both execution modes use the same serialization path (kwargs are
written to the TransitionMessage, then read back in phase 2). This
means tests catch serialization bugs that `task_always_eager` would
miss.

> **Historical — the design as shipped in 0.3.0, superseded by the
> typed round-trip in 0.5.0 (#107, #108).** Non-serializable values
> were coerced lossily at TransitionMessage creation, so a phase-2
> hook received strings where the synchronous path passed real
> `UUID`/`datetime` objects:
>
> - `request` → stripped (framework pulls `user` first)
> - `user` → `user_id`
> - `UUID` → `str`
> - `datetime` / `date` → `.isoformat()`

Since 0.5.0 the round-trip is type-faithful. Values that JSON cannot
represent natively are persisted with a self-describing `__dl_type__`
tag and restored to their original Python types in phase 2, so a
side-effect receives the same types whether its transition is
synchronous or background:

- `request` → dropped loudly: a warning is logged, and
  `DJANGO_LOGIC['STRICT_KWARGS_SERIALIZATION'] = True` raises instead.
  A live request cannot cross the phase boundary; extract `user`
  (which is rehydrated) or pass plain values.
- `user` → `user_id`, restored to a live `user` in phase 2.
- `datetime` / `date` / `time` / `Decimal` / `UUID` / `tuple` / `set`
  / `frozenset` → tag-encoded, restored in phase 2 with the original
  type (recursively, inside containers). `Decimal` and `set`,
  previously rejected at phase 1, are supported.
- Model instances and arbitrary objects → still rejected at phase 1
  (`TypeError`). Pass a pk and re-fetch in the hook: phase 2 may run
  much later and must see fresh rows, not a stale snapshot.
- Non-string dict keys → JSON objects only have string keys, so these
  cannot round-trip; flagged loudly at phase 1 (warning, or
  `TypeError` under the strict setting).

Rows written before the typed encoding (plain ISO strings) still
decode; deploy web and workers together when upgrading across this
boundary — an older worker passes the tagged dicts through verbatim.
The authoritative contract lives in the
`django_logic/background/serializers.py` module docstring.

### 2.13 Django app setup

`django_logic` becomes a proper Django app with migrations:
- Add to `INSTALLED_APPS`
- `TransitionMessage` model + migrations
- Dispatch hook installed at app-ready time; Celery mode uses
  `transaction.on_commit`, Sync mode dispatches inline

### 2.14 Remove deprecated legacy logging

- Remove `LogType`, `AbstractLogger`, `DefaultLogger`, `NullLogger`, `get_logger()`
- Remove `DJANGO_LOGIC_DISABLE_LOGGING` and `DJANGO_LOGIC_CUSTOM_LOGGER` settings
- Remove all `self.logger` references

### 2.15 Move DRF to optional dependency

Core library does not import DRF. Move to `extras_require`.

### 2.16 Testing surface shipped with Stage 2

The sync execution mode (§2.5) is the primary testing tool — it
replaces the "internal test runner" we originally sketched. What ships
alongside it:

- **`django_logic.background.sync_execution()`** — context manager /
  decorator that flips to sync mode for a block, independent of the
  global setting.
- **`django_logic.background.retry_pending()`** — helper that runs
  the periodic starter once, inline. Use it in tests to simulate
  "time passed, the starter re-dispatched the stale message".
- **`django_logic.testing.trackers`** *(internal module used by our
  own tests; promoted to public in Stage 3)* — records which
  side-effects / callbacks ran for a given `tr_id`, so tests can
  assert the execution timeline without brittle mocks.
- **Import-time validation errors** — `BackgroundTransition(queue=...)`
  missing, duplicate `in_progress_state` on a `Process`, and other
  configuration bugs raise `ImproperlyConfigured` before the first
  transition runs. No need for a separate linting step.

The full public `django_logic.testing` API (scenario framework,
snapshots, AI-readable output) lands in Stage 3 as planned.

### 2.17 Operational extras that shipped with Stage 2

These are the small, self-contained pieces that were pulled forward from
Stage 3 because they're either free or prerequisites for the GV
migration's operational visibility:

- **Timing fields on `TransitionMessage`** (§2.8). Enables a
  "hung-attempt" watchdog with no extra moving parts.
- **Unrestorable-row hoist.** Phase 2 marks permanently-unrestorable
  TMs completed in a single `UPDATE` *outside* the failed atomic
  block, so the periodic starter stops picking them up. Pattern:

  ```
  try:
      outcome = _run_atomic(tm_id)
  except _RestoreFailed as exc:
      # Atomic block rolled back; run the mark in a fresh statement
      # so is_completed=True persists.
      _mark_unrestorable_completed(exc.tm_id)
      return
  ```

  The in-atomic `mark_as_completed()` call that previously "looked
  right" was a bug — the `_NothingToDo` raise made the savepoint roll
  back and took the mark with it.

### 2.18 Tests (for Stage 2 itself)

- Phase 1: atomic rollback on IntegrityError, kwargs serialization,
  on_commit dispatch (Celery mode) vs inline dispatch (Sync mode)
- Phase 2: happy path, side-effect failure with retry, side-effect
  failure at max errors, select_for_update contention, idempotent re-run
- Periodic starter: picks up stale messages, respects queue_name,
  stops at max errors
- `BackgroundTransition(queue=None)` raises `ImproperlyConfigured`
- Duplicate `in_progress_state` within a `Process` raises `ImproperlyConfigured`
- Concurrent phase-1 requests: one succeeds, one gets `TransitionNotAllowed`
- Sync mode: exceptions propagate to caller; works under `TestCase`
  (savepoint semantics) and `TransactionTestCase`
- Sync mode + Celery not installed: works; setting `BACKGROUND_EXECUTION='celery'`
  without Celery raises `ImproperlyConfigured` at app-ready time

### 2.19 Integration into GV

This version will be vendored into the GV project under its own import
path, then migrated transition-by-transition. Only after GV is fully
migrated will the old `django_logic_ext`, `django-logic-celery`, and
`FixedRedisState` be removed from GV. No backward-compatibility shims
are shipped from this repo.

---

## 5. Stage 3 — Observability, DX & Testing (v1.0.0)

> **Goal:** Add observability, developer experience features, and the
> scenario-based testing framework. Ship 1.0 — the "Show HN" release.
> **Depends on:** Stage 2 (v0.3.0).
> **Delivers:** Docs site, testing framework, admin/DRF integration,
> observability, type hints. Signals stability.

### 3.1 Execution time logging

Instrument `SideEffects.execute()` and `Callbacks.execute()` to log
per-function timing:

```
tr_id SideEffect function_name started
tr_id SideEffect function_name completed duration_ms=1234
```

### 3.2 Transition timing on TransitionMessage — ✅ SHIPPED IN 0.3.0

The three fields (`started_at`, `completed_at`, `duration_ms`) plus the
`(is_completed, started_at)` watchdog index already landed with Stage 2
— see §2.8 above. Stage 3 only owns the *surfaces* that consume them:
the `transition_status` management command (§3.4) and the docs page on
observability (§3.6).

### 3.3 Configurable timeout per transition

```python
BackgroundTransition(
    action_name='fulfill',
    sources=['approved'],
    target='fulfilled',
    timeout=600,
    fallback='set_action_required',
    ...
)
```

### 3.4 Management command

```bash
python manage.py transition_status
```

Shows currently in-progress transitions, duration, error counts, queues.

### 3.5 Scenario-based testing framework

Ship the public `django_logic.testing` module — designed for document-driven
development and AI/vibe-coding workflows.

**`ProcessScenario` base class** with:
- `create_instance()` — create model instances with state
- `from_snapshot()` — restore instance from JSON snapshot for bug reproduction
- `transition()` — execute synchronous transitions
- `background_transition()` — run background transitions inline (no Celery)
- `retry_transition()` — simulate the periodic starter
- `assert_state()`, `assert_available()`, `assert_not_available()`
- `assert_side_effects_ran()`, `assert_callbacks_ran()`
- `assert_error_recorded()`, `assert_error_count()`

**`snapshot(instance)`** — capture full state of a model instance as JSON
(fields, related objects, TransitionMessage, process status). Copyable from
production logs/Sentry/admin for bug reproduction.

**`snapshot_on_failure`** — opt-in flag that automatically dumps instance
state as JSON when any test assertion fails.

**AI-readable failure output** — structured timeline showing exactly where
the process diverged from expectations, including snapshot JSON.

See `docs/design/TESTING_SCENARIOS.md` for the full design.

### 3.6 Documentation site (MkDocs Material)

| Page | Content |
|------|---------|
| Getting Started | 5-minute tutorial: model → process → transition → done |
| Core Concepts | Transition, Action, Process, State, conditions, permissions |
| Background Tasks | BackgroundTransition, BackgroundAction, queue routing |
| Reliability | TransitionMessage, retry, stuck detection, reliability contract |
| Observability | Execution timing, timeout configuration, fallback hooks |
| Testing Your Processes | Document-driven triangle, ProcessScenario, snapshots |
| Advanced | Nested processes, custom state classes, context passing |
| API Reference | Auto-generated from docstrings |
| Migration from django-fsm | Step-by-step guide |
| Cookbook | Approval flows, payment processing, order management |

### 3.7 Django Admin integration

`TransitionAdminMixin`:
- Show available transitions as action buttons
- Display current state with color coding
- Show `TransitionMessage` history inline

### 3.8 DRF integration

`TransitionSerializerMixin`:
- Expose `available_actions` field
- `TransitionViewSet` — generic viewset for triggering transitions via API

### 3.9 Examples

Ship `examples/` directory:
- `order_management/` — sync transitions + background fulfillment
- `payment_processing/` — background transitions with retry
- `approval_workflow/` — nested processes + permissions
- Docker Compose for each

### 3.10 Type hints

Full type annotations across the codebase. Run `mypy --strict`.

### 3.11 Better error messages

```
TransitionNotAllowed: Transition 'pay' not available from state 'draft'.
  Current state: draft
  Available transitions: approve, cancel
  Reason: state 'draft' not in sources ['approved']
```

### 3.12 Automated PyPI publishing

GitHub Actions workflow: on tag push → build → publish to PyPI.

### 3.13 Release 1.0.0

- Bump version to 1.0.0
- Development Status classifier: `5 - Production/Stable`
- Full CHANGELOG

---

## 6. Stage 4 — Communication & Launch

> **Goal:** Build awareness and drive adoption. Target: 200 → 500 stars.
> **Depends on:** Stage 3 (v1.0.0 released).
> **Delivers:** Blog posts, conference talks, community presence.

### 4.1 Launch content

| Content | Channel | Timing |
|---------|---------|--------|
| "Introducing Django Logic 1.0" blog post | Dev.to, Medium | At 1.0 release |
| "Django Logic vs Django FSM" comparison | Dev.to, Reddit r/django | Week 1 |
| "Background State Machines with Django & Celery" tutorial | Dev.to, Hashnode | Week 2 |
| "AI-Driven Business Logic with Django Logic" post | Dev.to | Week 3 |
| Video walkthrough (15–20 min) | YouTube | At 1.0 release |
| Twitter/X thread — "Why we built Django Logic" | Twitter/X | At 1.0 release |

### 4.2 Community outreach

- **Reddit** r/django, r/python — announcement + answer workflow questions
- **Hacker News** — "Show HN" post
- **Django Forum** (forum.djangoproject.com) — participate in discussions
- **Django Newsletter** + **Python Weekly** — submit for inclusion
- **Stack Overflow** — answer django-fsm and workflow questions
- **Awesome Django** list — submit PR
- **Django Packages** — update listing

### 4.3 Conference talks

Submit proposals to DjangoCon US/EU, PyCon (3–6 months ahead of each).

### 4.4 GitHub optimization

| Action | Detail |
|--------|--------|
| Repo description | "Declarative business logic & state machines for Django — sync and background, with Celery support" |
| Topics | `django`, `fsm`, `state-machine`, `workflow`, `celery`, `python`, `business-logic`, `ai` |
| Social preview | Professional card with logo + tagline |
| Releases | GitHub Releases with changelogs for every version |
| Issue templates | Bug report, feature request, question |
| CONTRIBUTING.md | Clear contribution guide |
| Sponsor button | GitHub Sponsors |

---

## 7. Stage 5 — Community & Ecosystem

> **Goal:** Sustain growth, build contributor community. Target: 500 → 1,000 stars.
> **Depends on:** Stage 4 (post-launch momentum).
> **Delivers:** Ecosystem packages, active community.

### 5.1 Ecosystem packages

- **django-logic-viz** — Generate Mermaid/Graphviz diagrams from process
  definitions (`python manage.py show_processes --format mermaid`)
- **django-logic-history** — First-class transition history/audit log
  (generalized from GV's HistoryMixin, eliminating the 13-class problem)
- **django-logic-graphql** — GraphQL mutations for transitions (Strawberry)

### 5.2 Community building

- Enable **GitHub Discussions** as the primary Q&A channel
- Label issues with **"good first issue"** to onboard contributors
- **Monthly release cadence** — keep momentum visible
- **"Built with Django Logic"** showcase section in docs
- Target **10+ contributors** and **5+ external PRs**

---

## 8. Resolved Decisions

| # | Question | Decision |
|---|----------|----------|
| 1 | Mono-repo vs separate packages | **Mono-repo.** `django_logic.background` ships in the core package; Celery as `extras_require`. |
| 2 | Fate of django-logic-celery | **Deleted.** The legacy chain-of-tasks pattern is superseded by Stage 2. No deprecation window; the repo has been removed from this workspace. |
| 3 | Auto-wrapping callables | **No auto-wrapping.** Side-effects are plain Python functions. The framework runs them inside a single Celery task — users don't write Celery tasks for side-effects. |
| 4 | Pluggable backends | **Celery-only.** |
| 5 | Naming | **`BackgroundTransition` / `BackgroundAction`.** |
| 6 | Version strategy | **Accelerated:** 0.2.0 → 0.3.0 → 1.0.0. |
| 7 | Queue routing | **Explicit `queue=` required per `BackgroundTransition`, no default.** Framework tasks use `DJANGO_LOGIC['STARTER_QUEUE']`. Missing `queue=` is `ImproperlyConfigured`. |
| 8 | Single-task vs chain-of-tasks | **Single task.** All side-effects and the target-state write run inside one `acks_late=True` Celery task, inside an atomic block. A worker crash re-delivers the whole task, preventing stuck `in_progress_state` models. |
| 9 | Phase 1 state change | **Atomic `set_state(in_progress_state)` + `TransitionMessage.create` in phase 1.** Concurrent requests are blocked by the partial unique constraint. |
| 10 | DRF integration | **Optional module in core** (`django_logic.contrib.drf`), shipped in v1.0.0. |
| 11 | Fire-and-forget vs durable background | **Durable only.** Fire-and-forget has no production value. |
| 12 | Callback reliability | **Best-effort.** Callbacks run after the atomic block and are not retried on crash. Critical work belongs in side-effects. |
| 13 | Retry semantics | **All side-effects re-run from scratch.** Require idempotency. No checkpointing. |
| 14 | Integration path into GV | **Vendor + migrate.** Copy this Stage 2 code into GV under its own import path, migrate transitions one at a time, then remove legacy extensions. No backward-compat shims from this repo. |
| 15 | Running background work without Celery | **Sync execution mode is first-class.** `DJANGO_LOGIC['BACKGROUND_EXECUTION'] = 'sync'` runs phase 2 inline in the same process. Used in tests, management commands, shell, CI. Bypasses `transaction.on_commit` (so `TestCase` works). Exceptions from phase 2 propagate to the caller — tests can `assertRaises`. `sync_execution()` context manager for per-block override. Celery uninstalled → sync is the only mode. |
| 16 | `BackgroundAction` durability | **Same durable path as `BackgroundTransition`** (TransitionMessage + retry), just without a state write on success. No `target`, no `in_progress_state`. `queue=` still required. |
| 17 | `in_progress_state` uniqueness | **Unique within a `Process`.** Duplicates raise `ImproperlyConfigured` at class creation. This removes the `ignore_sources` hack for phase 2 lookup and makes state-to-transition mapping unambiguous. |

---

## 9. Success Metrics

| Metric | Target | When |
|--------|--------|------|
| GitHub stars | 200 | After v1.0.0 launch |
| GitHub stars | 500 | 6 months post-launch |
| GitHub stars | 1,000 | 12 months post-launch |
| PyPI monthly downloads | 5,000+ | 6 months post-launch |
| Test coverage | >= 95% | Every release |
| Open issues resolved | < 10 open at any time | Ongoing |
| Contributors | 10+ | 12 months post-launch |
| Docs site monthly visitors | 1,000+ | 6 months post-launch |

---

## Reference Documents

See `docs/INDEX.md` for full documentation map.

| Document | Location |
|----------|----------|
| Fundamental Problem (nested transitions) | [`docs/recipes/nested-processes.md`](recipes/nested-processes.md) (the original external research note is not part of this repo) |
| Background Transition Analysis | `docs/design/BACKGROUND_TRANSITION_ANALYSIS.md` |
| Scenario-Based Testing Design | `docs/design/TESTING_SCENARIOS.md` |
| PR #75 Review (historical) | `docs/research/PR-75-REVIEW.md` |
| Race Condition Investigation | `docs/research/race-condition-issue` |
| Monitoring & Fallback Ideas | `docs/research/idea1.txt` |
