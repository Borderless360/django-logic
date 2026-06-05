# Improvements from the Heroku validation

> Findings from validating django-logic 0.3.0's durable background model on
> real infrastructure — RabbitMQ (CloudAMQP) + PostgreSQL + multiple Celery
> workers + induced worker crashes, mid-flight deploys, broker message loss,
> and pgbouncer transaction pooling. Harness:
> [django-logic-test](https://github.com/Borderless360/django-logic-test)
> (16-row matrix + a nested parent/child case).

## Validated as correct (no change needed)

Every core durability claim held on real infrastructure — **zero library bugs
found**:

| Behaviour | Evidence |
|-----------|----------|
| `on_commit` → `apply_async` → worker → `set_state(target)` | happy path, 69ms |
| Retry → eventual success (periodic starter) | fail×2 → fulfilled, errors=2 |
| Terminal failure at `MAX_ERRORS` → `failed_state` + failure hooks | errors=5 → failed |
| **Worker crash mid-transition** (acks_late redelivery) | `os._exit` on one worker → re-ran on another → completed |
| Deploy mid-flight (SIGTERM → redeliver) | `ps:restart` mid-transition → completed |
| **Broker message loss** → starter recovery | purged the queue → durable TM re-dispatched → completed |
| Concurrent phase-1 (partial unique) | 2 simultaneous → one `AlreadyInProgress` |
| Concurrent phase-2 (`select_for_update nowait`) | 2nd worker "locked … skipping", no dup side-effects |
| Watchdog timeout / stuck finalize / cleanup | all deterministic via the safety-net tasks |
| kwargs serialization, UUID PK, `BackgroundAction`, unrestorable stop-retry | all pass |
| Queue isolation | saturated slow queue did not delay a critical action |
| Nested parent/child (clean pattern) | one child fails → parent `action_required`, siblings fine, no raise |

## Improvement opportunities

Ordered by value. Items marked **[issue]** are filed on the tracker.

### 1. Per-transition task identity for observability — HIGH **[issue]**
Every background transition is dispatched as the single Celery task
`django_logic.run_background_transition`. In Sentry/Flower/CloudAMQP all
transitions collapse under one name, so a failing export-report transition is
indistinguishable from a failing client quick-action. The harness worked
around it with Sentry fingerprints/tags + a per-transition transaction name,
but that's consumer-side and Sentry-specific.
**Proposal:** dispatch with a per-transition task name (e.g.
`django_logic.<process>.<action>`, registered symmetrically web↔worker), or at
minimum set the OpenTelemetry/Sentry transaction name to the transition inside
the task. Distinct names make every monitoring tool group correctly.

### 2. Make `task_reject_on_worker_lost` a documented hard requirement — MEDIUM **[issue]**
Crash redelivery (the headline durability guarantee) needs **both**
`task_acks_late=True` (the library sets this on its task) **and**
`task_reject_on_worker_lost=True` (a *project* Celery setting the consumer
must add). Without the latter, a SIGKILL'd worker's in-flight task may be
acked-and-dropped rather than redelivered, and recovery then relies solely on
the periodic starter (slower). **Proposal:** document this prominently in the
"Production deployment" section and/or emit a startup warning if acks_late is
on but reject_on_worker_lost is off.

### 3. pgbouncer / psycopg3 transaction-pooling compatibility — MEDIUM (GV-critical) **[issue]**
The concurrency guard works under pgbouncer **transaction** pooling, but only
with: `OPTIONS={'prepare_threshold': None}` (psycopg3 server-side prepared
statements break in transaction mode — symptom: phase 2 hangs/errors),
`DISABLE_SERVER_SIDE_CURSORS=True`, and no `sslmode=require` on the
app→pgbouncer hop. **Proposal:** add a "Running behind pgbouncer" docs section;
optionally detect a pooled DSN and warn if prepared statements are enabled.

### 4. Ship a parent/child coordination recipe (and maybe helpers) — HIGH **[issue]**
The original pain (`fundamental problem.md`) is nested `process.xxx()` in
side-effects. The validated clean pattern — parent fans out, child failure
contained in its own `failed_state`, children report via best-effort callbacks
running an idempotent guarded completion check, errors aggregated by reading
child rows, explicit `action_required` parent state — should be **first-class
documentation**, and possibly a small helper (e.g. a "when all children
terminal, fire X" completion utility). Full write-up:
`django-logic-test/docs/design/NESTED_PROCESS_ERROR_HANDLING.md`.

### 5. Log level for handled safety-net conditions — LOW
`detect_stuck` finalization, the watchdog timeout, and the "cannot be restored
→ marking completed" path log at `ERROR`. Some are *handled, expected*
outcomes; at scale they create Sentry-issue noise. Consider `WARNING` for the
handled/expected cases, reserving `ERROR` for genuinely actionable ones — or
document a recommended logging filter.

### 6. Ops affordances — LOW
- A built-in management command to (a) re-dispatch a specific
  `TransitionMessage` immediately (bypassing the `RETRY_MINUTES` recency guard
  — useful for incident response and testing), and (b) show in-progress /
  stuck transitions (the README sketches `transition_status`).
- Document the Postgres **connection budget**: each in-flight task holds a
  connection (two if the app opens a second connection per task); size
  `concurrency × workers` against the DB's limit (pgbouncer or plan cap).
- Document a beat-liveness alert recipe (e.g. Sentry cron monitors via
  `CeleryIntegration(monitor_beat_tasks=True)`).

## Not a library concern (consumer/deployment)

- Web-tier capacity (a 2-worker web dyno can't absorb a large *simultaneous*
  HTTP burst) — size the web tier; generate heavy load server-side.
- The harness's second "evidence" DB connection per task (for crash-proof
  audit rows) strains a pooled connection budget — a harness design detail,
  not django-logic.
