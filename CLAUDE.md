# django-logic — guidance for AI assistants

This repo is the **django-logic** library: declarative business logic & state
machines for Django, with durable, queue-routed background transitions
(`django_logic.background`). This file tells an AI how to **use the library
correctly** when generating or reviewing code that depends on it. (Mirror of
`.cursor/rules/django-logic.mdc`.) The rules below are distilled from a full
production-style validation on Heroku (RabbitMQ + PostgreSQL + multiple
workers + induced worker crashes, deploys, broker loss, and pgbouncer).

## What to generate

Model a workflow as a `Process` subclass: a list of `transitions` (edges).
Each transition has `sources`, `target`, and optional `conditions`,
`permissions`, `side_effects`, `callbacks`, `failure_side_effects`,
`failure_callbacks`. **Bind the model to its process in exactly one place — the
app's `AppConfig.ready()`** — with
`ProcessManager.bind_model_process(Model, MyProcess, state_field='status')`
(import the model and process *inside* `ready()`). Never bind at module import
time in `models.py`/`process.py`: that forces a
`model → process → actions → model` circular import (issue #100), because the
process and its action functions both reference the model. `ready()` runs after
every app's models are loaded, so the cycle never forms and action modules can
import the model at the top level. Then drive it via
`instance.process.<action>(...)` from request/task/method bodies (never at
module top or in another app's `ready()`).

Use `BackgroundTransition` (durable, runs side-effects on a Celery worker,
writes target/`failed_state`) or `BackgroundAction` (same durability, no state
change on success) for anything slow, external, or retriable.

## Non-negotiable rules

1. **Side-effects must be idempotent against external systems.** Background
   side-effects re-run from scratch on every retry. Since 0.4 each attempt's
   *database* writes run in a savepoint and roll back on failure
   (all-or-nothing per attempt), so the idempotency you owe is for external
   calls (APIs, emails, payments). Critical work goes in `side_effects`;
   `callbacks` are best-effort (exceptions swallowed, lost on crash).
2. **Route by SLA with named queues.** `queue=` is optional — transitions
   without it go to `DJANGO_LOGIC['DEFAULT_QUEUE']` (`'django_logic'`).
   Give heavy or SLA-sensitive transitions their own queue (e.g.
   `critical`/`slow`/`fast`) and a dedicated worker per queue.
3. **Never call a nested `x.process.foo()` inside a `side_effect` expecting its
   exception to propagate** — it cascades failures across state machines (the
   "fundamental problem"). For parent→children (e.g. an order with many
   fulfillments): **fan out** to each child's own background transition,
   contain each child's failure in its own `failed_state`, have children
   **report back via best-effort callbacks** that run an **idempotent guarded
   completion check** on the parent, and **aggregate errors by reading child
   rows** (give the parent an explicit `action_required` partial-failure
   state). Never re-raise a child error into the parent.
4. **`in_progress_state` is unique within a Process**; set a `failed_state` so
   failures are contained.
5. **Test in sync mode**: `DJANGO_LOGIC['BACKGROUND_EXECUTION']='sync'` (or the
   `sync_execution()` context manager) runs phase 2 inline with no broker and
   propagates exceptions; `retry_pending()` simulates the periodic starter.
   The global default is `'celery'`, so test settings must opt into sync.
   See `docs/TESTING_GUIDE.md` for the full scenario catalog.
6. **One in-flight background transition per instance per process.** While an
   uncompleted `TransitionMessage` exists, a second background transition
   raises `AlreadyInProgress` and a *synchronous* transition on the same
   instance+process raises `TransitionNotAllowed` — design flows so follow-up
   work chains from terminal hooks, not mid-flight.
7. **Manual state fixes win.** If an instance is moved externally while a
   background row is pending, phase 2 completes the row as *superseded*
   (`'[superseded]'` in `last_error_message`) and skips side-effects
   (`DJANGO_LOGIC['PHASE2_STATE_GUARD']`, default `'enforce'`).

## Deployment the durability contract depends on

- A real broker (Redis/RabbitMQ). Celery and django-redis are core
  dependencies of django-logic (installed automatically);
  `BACKGROUND_EXECUTION` defaults to `'celery'`.
- A cross-process `default` cache (django-redis) for the state lock —
  celery mode refuses to boot with a locmem/dummy cache when `DEBUG=False`.
- Crash re-delivery is built in (every django-logic task sets
  `acks_late=True` + `reject_on_worker_lost=True`); set the global Celery
  pair only for your *own* tasks. You still need a **single beat**
  scheduling the four `django_logic.*` safety-net tasks —
  `app.conf.beat_schedule = beat_schedule()` (from
  `django_logic.background`) routes them to `STARTER_QUEUE` — and a worker
  for every queue you use.
- Behind **pgbouncer transaction pooling**: `OPTIONS={'prepare_threshold':
  None}`, `DISABLE_SERVER_SIDE_CURSORS=True`, and no SSL on the app→pgbouncer
  hop. The concurrency guard (`select_for_update(nowait)` + partial-unique)
  then holds.

## Working IN this repo

- Tests: `make test` / `pytest` (SQLite suite); PostgreSQL concurrency +
  stability suites under `tests/stability`, `tests/background`.
- `django_logic/background/` is the durable engine: `transitions.py`,
  `dispatch.py`, `runner.py` (phase 2), `tasks.py` (Celery + periodic),
  `models.py` (`TransitionMessage`), `settings.py`.
- Read `docs/PLAN.md`, `docs/design/BACKGROUND_TRANSITION_ANALYSIS.md`, and
  `docs/recipes/nested-processes.md` (the fan-out pattern and the
  cascading-failure anti-pattern it replaces) before changing the
  background engine.

See `docs/IMPROVEMENTS_FROM_HEROKU_VALIDATION.md` for validated-behavior notes
and open improvement ideas.
