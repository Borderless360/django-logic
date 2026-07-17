# Changelog

## [Unreleased]

## [0.6.0] — 2026-07-17

Consumer-facing observability: a first-class bindings registry and a
Django system check that makes warn-mode hook-signature offenders
impossible to miss (#125). Also lands the sync/background parity contract
matrix (#111) — test-only, but it pins the cross-class contracts consumer
migrations depend on.

### Added — bindings registry + system checks (#125)

- **`ProcessManager.bindings`** — a public registry of
  `(model, process_class, state_field)` recorded by every
  `bind_model_process` call, so consumer tooling (coverage audits,
  contract tests) no longer re-derives bindings from model attributes.
- **`django_logic.W001` system check** — re-runs hook-signature
  validation over the registry through Django's checks framework, so
  warn-mode offenders surface in `manage.py check`, every test run and
  deploy checks. Bind-time logger warnings alone are emitted during
  `ready()`, before logging is configured, and can go entirely unseen —
  a consumer's warn-mode suite showed zero warnings on a tree where
  strict mode found three real offenders.

## [0.5.1] — 2026-07-17

### Fixed

- **0.5.0 regression:** bind-time hook validation crashed with
  `TypeError: 'property' object is not iterable` on a Process whose
  class-level `conditions`/`permissions` is a property/descriptor
  (computed per instance). Such definitions cannot be inspected at bind
  time and are now skipped (#121).

## [0.5.0] — 2026-07-17

Type-faithful background kwargs (#107, #108), bind-time hook-signature
validation (#113), and a Django-version CI matrix plus a downstream
consumer-contract job (#110, #112).

### Upgrade notes

The typed kwargs round-trip **changes what background hooks receive in
phase 2** — this is the headline behavioural change of this release:

- **Audit background hooks written against the 0.4.x contract.** Hooks
  that parse ISO strings back into `datetime`/`UUID`, or re-wrap values
  (`UUID(kwargs['some_id'])`), now receive the original types directly.
  While pre-upgrade rows drain, a hook can see *both* forms — tolerate
  both (e.g. `v if isinstance(v, UUID) else UUID(v)`) until the queue is
  clean, then simplify to the typed form.
- **Deploy web and workers together.** A 0.4.x worker passes 0.5.0's
  tagged kwargs dicts through verbatim (it cannot decode them), and
  rolling back to 0.4.x leaves any 0.5.0-written pending rows undecodable.
  Drain or requeue pending `TransitionMessage` rows if you must roll back.
- **Snapshot assertions on persisted kwargs change shape.** The testing
  snapshot helper (`django_logic.testing.snapshot`) exposes the stored
  `TransitionMessage.kwargs`, which now contains `__dl_type__` tag dicts
  for non-JSON-native values.
- **New warnings are on by default.** Passing `request` to a background
  transition, hooks without a named instance-first parameter, and
  non-string dict keys in kwargs each log a warning. Silence them by
  fixing the call sites — or make them hard errors with
  `DJANGO_LOGIC['STRICT_KWARGS_SERIALIZATION']` /
  `DJANGO_LOGIC['STRICT_HOOK_SIGNATURES']`.
- **Trove classifiers now match what CI tests**: Django 4.2 / 5.1 / 5.2 /
  6.0. Dropped 4.0 (never installable under the `requires-python >= 3.11`
  floor this package already had) and 5.0 (end-of-life, untested); added
  4.2, which CI has always tested.

### Added — bind-time hook-signature validation (#113)

- `ProcessManager.bind_model_process` now validates every hook across the
  process tree — transition-level side-effects, callbacks, failure hooks,
  conditions and permissions, plus process-level `conditions`/`permissions`:
  the engine calls hooks as `fn(instance, **kwargs)` (permissions as
  `fn(instance, user, **kwargs)`), so a hook whose first parameter is not a
  named positional (e.g. task-style `def hook(*args, **kwargs)`) is flagged
  at bind time instead of failing at runtime on a worker. Warns by default;
  `DJANGO_LOGIC['STRICT_HOOK_SIGNATURES'] = True` raises
  `ImproperlyConfigured`. Decorated hooks need `functools.wraps` so their
  real signature is visible to the validator.

### Changed — kwargs serialization (#107, #108)

- **Type-faithful kwargs round-trip** (#108). Background-transition kwargs
  are now persisted with a self-describing type tag (`__dl_type__`) and
  restored to their original Python types in phase 2: `datetime`, `date`,
  `time`, `Decimal`, `UUID`, `tuple`, `set`, `frozenset` — recursively
  inside containers. A side-effect now receives the same types whether its
  transition is synchronous or background. `Decimal` and `set`, previously
  rejected at phase 1, are now supported. Rows written by older versions
  (plain ISO strings) still decode; deploy web and workers together when
  upgrading across this boundary (an old worker passes tagged dicts through
  verbatim). Model instances remain rejected — pass a pk and re-fetch.
  Non-string dict keys cannot round-trip (JSON objects have string keys):
  phase 1 flags them with a warning, or a `TypeError` under
  `STRICT_KWARGS_SERIALIZATION`.
- **`request` is dropped loudly** (#107). Phase-1 serialization logs a
  warning (with the tr_id) when it drops `request` from a background
  transition's kwargs, and the new
  `DJANGO_LOGIC['STRICT_KWARGS_SERIALIZATION'] = True` raises `TypeError`
  (specifically `serializers.KwargsSerializationError`) instead. Phase-2
  hooks must never read `request` — the engine rehydrates `user`; pass
  anything else as plain values.
- New `deserialize_kwargs()` is the phase-2 inverse of
  `serialize_kwargs()`; `restore_user()` remains available.
  `make_json_safe()` is kept as a legacy helper but is no longer used by
  the engine.

## [0.4.1] — 2026-07-02

### Added

- **Advertise Django 6.0 support.** Added the `Framework :: Django :: 6.0`
  trove classifier. This is metadata only — the `django>=4.0` requirement
  already permitted Django 6.0, and the full test suite passes against it.

## [0.4.0] — 2026-07-02

Stability hardening plus condition-disambiguated nested background
transitions (#98) and standardised `AppConfig.ready()` process↔model binding
(#100). Every defect from the 0.3.x stability review (R1–R6 reproduced
defects, D1–D5 design races) is fixed with a permanent regression test. See
`docs/STABILITY_REVIEW_AND_V1_PLAN.md` in the planning repo for the full
findings and resolution mapping.

### Added — nested background transition routing (#98)

- **Condition-disambiguated background transitions across nested processes**
  (issue #98). Two nested processes may now declare background transitions
  that **share an `action_name`**, selected by a condition on the instance —
  the polymorphic-routing pattern the synchronous path already supported
  (e.g. per-integration `Gmail` / `Dummy` sub-processes each owning a
  background `send_message_via_integration`). Phase 1 records the owning
  (nested) process class on the `TransitionMessage`
  (`owning_process_class`, migration `0007`); phase 2 restores that **exact**
  transition from it, without re-evaluating the condition. Generic callers
  keep calling `instance.process.send_message_via_integration(...)`.

### Changed

- **Standardised process↔model binding on `AppConfig.ready()` (issue #100).**
  `ProcessManager.bind_model_process(...)` is now documented and practised in
  exactly one place — the app's `AppConfig.ready()` — instead of at module
  import time in `models.py`/`process.py`. Binding at import time forced a
  `model → process → actions → model` circular import (the process and its
  side-effect/condition/permission functions both reference the model), whose
  only workaround was scattering `from .models import X` calls inside every
  action function. Binding in `ready()` (which runs after every app's models are
  loaded) removes the cycle, so action modules import their model at the top
  level normally. No library API change — `bind_model_process` is unchanged;
  the README, `CLAUDE.md`, the Cursor rule, and the bundled test apps
  (`tests/background`, `tests/stability`) now bind in `ready()` only.
- **`_validate_unique_background_action_names` is relaxed to a single
  invariant.** It previously rejected *any* two background transitions sharing
  an `action_name` across a process and its nested tree, and any background
  name that collided with a synchronous one. It now rejects only the genuinely
  ambiguous case — two **background** transitions sharing an `action_name`
  **within a single process class** (where `(owning class, action_name)` no
  longer identifies one transition). Both a shared background name across
  **distinct** nested process classes, and a background name that **coincides
  with a synchronous** transition, are now allowed: phase 2 only ever restores
  background transitions (`_find_transition` filters to `is_background`), so a
  synchronous namesake is invisible to restore, and phase 1 resolves the call
  by conditions/permissions exactly as it already does for duplicate
  synchronous names (an ambiguous call raises `TransitionNotAllowed` at
  runtime). This enables, e.g., a synchronous fast-path and a durable
  background slow-path under one `action_name`, routed by a condition.
- **Phase-2 restore (`runner._find_transition`) prefers the recorded owner**
  and considers only `is_background` transitions. The owner is recorded for
  every background transition started through the Process entrypoint (for a
  transition on the bound process it equals the bound class). Rows with a blank
  `owning_process_class` — created before this release, or enqueued outside the
  Process entrypoint — fall back to matching by `action_name`, but **only when
  that name is unambiguous across the tree**. If an owner-less (or
  renamed-owner) row's name is shared by several nested background transitions,
  restore **refuses to guess** and finalizes the row without running any
  side-effects (it raises internally and stops retrying) rather than risk
  running the wrong condition-disambiguated sibling. Unique-name legacy rows are
  unaffected.

### Upgrade notes

- **Migration `0007` takes a brief `ACCESS EXCLUSIVE` lock** on
  `transitionmessage` (an additive, non-rewriting `ADD COLUMN` on PostgreSQL
  11+). That table is the engine's hottest, so on a busy system run `migrate`
  with a short `lock_timeout` (e.g. `SET lock_timeout = '2s'`) and retry,
  ideally during a low-throughput window. `owning_process_class` is a
  `TextField` (unbounded, never indexed) so deeply-namespaced process paths
  cannot overflow it.
- **Drain before refactoring a background `action_name` into shared nested
  processes.** If you turn a single, uniquely-named background transition into
  the condition-disambiguated nested pattern (same `action_name` on two nested
  processes), do it in a deploy with **no in-flight rows for that action**
  (or split it across two deploys). A row enqueued by the old code carries a
  blank `owning_process_class`; once the name becomes ambiguous, phase 2 cannot
  determine which sibling it meant and will finalize it without side-effects
  (safe, but the work does not run). Rows enqueued after this release always
  record their owner and are immune.

### Breaking Changes

- **Celery and django-redis are core dependencies.** Background transitions
  are Celery tasks — `celery>=5.0` and `django-redis>=5.0.0` install
  automatically. The `[celery]` / `[redis]` extras remain as empty aliases so
  existing pins keep resolving. The no-Celery `@shared_task` shim is removed.
- **`BACKGROUND_EXECUTION` defaults to `'celery'`** (previously: `'celery'`
  only when Celery was importable, else `'sync'`). Test settings must opt in
  with `DJANGO_LOGIC['BACKGROUND_EXECUTION'] = 'sync'`.
- **Celery mode rejects a per-process lock cache at boot.** With
  `DEBUG=False`, a locmem/dummy `default` cache raises `ImproperlyConfigured`
  (the state lock must be shared between web processes and workers); with
  `DEBUG=True` it logs a warning.
- **The in-flight constraint is scoped per process** (migration `0006`,
  constraint renamed `dl_bg_only_one_uncompleted_per_instance` →
  `dl_bg_one_uncompleted_per_process`). Two processes bound to different
  state fields of one model no longer falsely conflict; a duplicate within
  one process still raises `AlreadyInProgress`.
- **Synchronous transitions are gated on in-flight background work.** While
  an uncompleted `TransitionMessage` exists for an instance + process, a
  synchronous `Transition` on it raises `TransitionNotAllowed` (synchronous
  `Action`s are unaffected). Previously sync and background work could
  interleave and overwrite each other's state writes.
- **Phase-2 side-effects run in a savepoint — failed attempts roll back
  their database writes** (all-or-nothing per attempt). The idempotency
  contract shrinks to external calls only. `failure_side_effects` get the
  same isolation; a broken cleanup path rolls back its partial writes.
- **The phase-2 state guard supersedes externally-moved instances.** If the
  instance no longer sits in the state phase 1 left behind (manual ops fix,
  external write), phase 2 completes the row as superseded (`[superseded]`
  in `last_error_message`), skips side-effects, and the external change
  wins. Configure with `DJANGO_LOGIC['PHASE2_STATE_GUARD'] = 'enforce'`
  (default) or `'warn'` (pre-0.4 behaviour). The same guard protects
  `failed_state` writes by the safety-net tasks.

### Fixed (stability review defects)

- **R1 — a `DatabaseError` raised by a side-effect no longer poisons phase 2.**
  Previously the aborted connection made `record_error` itself raise
  `TransactionManagementError`: the error was never recorded, `errors_count`
  never reached `MAX_ERRORS`, the starter re-dispatched the row forever, and
  the constraint blocked every future background transition on the instance.
  Now it is recorded like any failure and the row reaches its terminal state.
- **R2 — partial side-effect writes from a failed attempt no longer commit**
  (rolled back with the attempt's savepoint).
- **R3 — `RedisState` no longer strands instances locked after background
  transitions.** `RedisState.set_state` writes the cache key with `xx=True`:
  writing state never *creates* a lock key — only `lock()` does. RedisState
  is now fully supported with background transitions.
- **R4 — phase 2 no longer overwrites external state changes** (see the
  state guard above).
- **R5 — false cross-process conflicts removed** (see the per-process
  constraint above).
- **R6 — phase 2 restores the process class that enqueued the transition.**
  `_restore` verifies the attribute-resolved class against the recorded
  `process_class` and prefers the recorded one on mismatch (name collision /
  rename between deploys), using the new `TransitionMessage.field_name`
  instead of guessing the state field.
- **D1 — validate-then-lock TOCTOU closed.** Both sync and background
  phase 1 re-read the persisted state under the lock and reject the
  transition if it is no longer a valid source.
- **D2 — sync/background mutual exclusion.** Background phase 1 acquires the
  state lock for its critical section (released in a `finally`, so nothing
  leaks on `AlreadyInProgress` or a caller-transaction rollback); sync
  transitions check the uncompleted-row gate (see above). Phase 1 also
  re-verifies the persisted state **after** the `TransitionMessage` insert:
  on PostgreSQL the insert can block in a speculative-insert wait while a
  concurrent flight's phase 2 finishes, admitting the request against an
  instance that already reached its target — without the recheck the
  transition silently ran twice (observed live on the Heroku harness).
- **D3 — a failing `Action` no longer clobbers an in-flight transition's
  state.** `failed_state` is written only when the state is not locked;
  otherwise the write is skipped with an ERROR log (the exception still
  propagates and failure hooks still run).

### Fixed (GitHub issues #85–#96)

- **#85 — the state lock is released on every failure path after
  acquisition**: a failed `in_progress_state` write, a failed target write
  in `complete_transition`, and a failed `failed_state` write in
  `fail_transition` all unlock before re-raising. Previously any of these
  froze the instance's FSM for the full `LOCK_TIMEOUT`.
- **#87 — positional arguments to transition methods raise `TypeError`.**
  `instance.process.verify(user)` used to silently drop the positional
  user and run with **no permission checks**.
- **#88 — `in_progress_state` uniqueness is validated across a Process AND
  its nested processes** (matching the documented invariant), not just the
  class's own transitions.
- **#90 — the background runner reloads instances via `_base_manager`**
  (and `State.get_persisted_state` does the same), so a filtered default
  manager (archived/soft-deleted rows hidden) can no longer strand an
  in-flight transition as "unrestorable".
- **#91 — crash re-delivery no longer depends on consumer settings**: every
  django-logic task sets `reject_on_worker_lost=True` alongside
  `acks_late=True` at the task level. The old dispatch-time warning (which
  read the *global* `task_acks_late` and could never fire for the per-task
  setting) is removed.
- **#92 — documented loudly** (README + `AlreadyInProgress` docstring) that
  swallowing `AlreadyInProgress` loses updates that arrive while phase 2 is
  mid-flight, with the dirty-flag/re-dispatch pattern consumers need.
- **#94 — a requested `fail_side_effect` that never fires now fails the
  test loudly**: unknown hook names are rejected eagerly by `track()`, and
  a hook that exists but never executes fails the drive — a silent no-op
  used to turn failure tests into happy-path runs.
- **#95 — snapshot fidelity**: `snapshot()` captures JSONField dict/list
  values as real JSON trees (previously a corrupting Python-repr string)
  and fails loudly on unsupported types; `from_snapshot()` refreshes from
  the DB so the returned instance carries real field types, not strings.
- **#96 — scenario tracking instruments the whole process tree**, so hooks
  executed via `next_transition` follow-ups and callback-triggered
  transitions are visible to `assert_side_effects_ran` /
  `assert_side_effects_not_ran`.
- (#86 validate-then-lock TOCTOU, #89 Action `failed_state` guard, and #93
  sync/background interleaving were fixed by the D1/D3/D2 work above.)

### Added

- **`queue=` is optional.** Transitions without it route to
  `DJANGO_LOGIC['DEFAULT_QUEUE']` (default `'django_logic'`), resolved at
  dispatch time. An explicit empty string is still rejected. `STARTER_QUEUE`
  now defaults to `'django_logic.starter'`.
- **`TransitionMessage.field_name`** — phase 1 records the bound state
  field; phase 2 uses it when reconstructing a process from `process_class`
  (legacy rows fall back to the old inference).
- **`TransitionMessage.mark_as_superseded(note)`** — terminal completion for
  rows superseded by external state changes (no `errors_count` increment).
- **`State.get_persisted_state()`** — always reads the database row,
  bypassing any cache layer; used by the revalidation and the state guard.
- **`docs/TESTING_GUIDE.md`** — the full scenario catalog for testing
  processes (happy paths, gating, failures, retries, terminal failures,
  one-in-flight conflicts, superseded rows, snapshot replay) without Celery.
- **`beat_schedule()`** (`django_logic.background`) — ready-made Celery
  beat entries for the four safety-net tasks, routed to
  `DJANGO_LOGIC['STARTER_QUEUE']` with the recommended intervals
  (overridable per task): `app.conf.beat_schedule = beat_schedule()`.
- **`assert_failure_side_effects_ran` / `assert_failure_callbacks_ran`** on
  `ProcessScenario` — the tracker already recorded failure-hook executions;
  now they are assertable. Snapshots also capture/restore the
  `TransitionMessage.field_name` column so restored rows take the same
  phase-2 path as the production row.

### Observability & DX (from Heroku validation; issues #78–#81)

- **Per-transition monitoring identity.** Background dispatch now sets a Celery
  `shadow` (`django_logic.<app>.<transition>`) so Flower / RabbitMQ management /
  Celery events show a distinct name per transition instead of the one shared
  `django_logic.run_background_transition` task. When `sentry-sdk` is installed,
  the runner also names the Sentry transaction and tags it
  (`dl.app`/`dl.model`/`dl.transition`/`dl.instance_id`/`dl.queue`) per
  transition, so each transition is its own Sentry issue. Opt out with
  `DJANGO_LOGIC['SENTRY_TRANSACTION_NAMING'] = False`. No new dependency.
- **Crash re-delivery configured per task.** Every django-logic task sets
  `acks_late=True` + `reject_on_worker_lost=True` on the decorator (see
  issue #91 above), so the pair crash re-delivery depends on no longer
  hinges on consumer Celery settings. A one-time warning on first
  celery-mode dispatch still flags a missing/in-memory broker.
- **pgbouncer (transaction pooling) deployment guide** in the README
  (`prepare_threshold=None`, `DISABLE_SERVER_SIDE_CURSORS`, no app→pgbouncer SSL).
- **`django_logic.conditions`** — `all_related_in` / `any_related_in` guard
  factories for parent/child completion checks, plus
  `docs/recipes/nested-processes.md` (the clean alternative to nested
  `process.xxx()` calls in side-effects).
- **AI usage rules** — `.cursor/rules/django-logic.mdc` + `CLAUDE.md`.

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

### Privacy / logging controls

- **Opt-in kwargs redaction.** Transition kwargs (which can carry `user`, `request`, and arbitrary business data) are attached to log records via `extra={'kwargs': ...}`. Two new `DJANGO_LOGIC` settings let PII/compliance-sensitive deployments control this: `LOG_KWARGS = False` omits kwargs from log records entirely, and `LOG_KWARGS_REDACTOR = <callable | 'dotted.path'>` runs a sanitiser over a copy of the kwargs before logging. Default behaviour is unchanged (kwargs logged as-is).

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
- **Celery mode warns when no broker is configured.** On the first celery-mode dispatch (when the project's Celery app is actually configured), django-logic logs a one-time warning if the resolved `broker_url` is empty or `memory://` (messages would otherwise vanish into an in-memory transport that no worker drains). The check is at dispatch rather than app-ready because app-ready runs before the standard `celery.py` configures the broker. `_reject_sqlite_in_celery_mode` also now checks only the alias `TransitionMessage` is routed to (a secondary SQLite alias on a Postgres-default deployment is no longer rejected).
- **`errors_count` increments atomically.** `TransitionMessage.record_error` now uses a DB-side `F('errors_count') + 1` update instead of a read-modify-write on a possibly-stale in-memory value, so a watchdog and a reconnected zombie worker racing on the same row can't lose an increment.
- **`NextTransition` no longer guesses on ambiguity.** A `next_transition` whose name resolves to more than one available transition is now refused (logged, skipped) instead of silently running whichever was first in iteration order, and the follow-up is invoked through the normal `Process` entrypoint so it gets its own `tr_id` and `_transition_context` (parent chain) rather than inheriting the parent's.
- **Removed a redundant lock check.** `Transition.change_state` relied on `state.is_locked() or not state.lock()`; the atomic `lock()` alone is sufficient, so the `is_locked()` pre-check (a TOCTOU window + extra round-trip) was dropped.
- **User serialization reads `pk`, not `id`,** matching the phase-2 `get(pk=...)` restore and supporting custom user models whose primary key isn't named `id`.

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
