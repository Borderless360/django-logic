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
`failure_callbacks`. Bind it with
`ProcessManager.bind_model_process(Model, MyProcess, state_field='status')`,
then drive it via `instance.process.<action>(...)`.

Use `BackgroundTransition` (durable, runs side-effects on a Celery worker,
writes target/`failed_state`) or `BackgroundAction` (same durability, no state
change on success) for anything slow, external, or retriable.

## Non-negotiable rules

1. **Side-effects must be idempotent.** Background side-effects re-run from
   scratch on every retry. Critical work goes in `side_effects`; `callbacks`
   are best-effort (exceptions swallowed, lost on crash).
2. **Every background transition declares `queue=`** (no default). Route by
   SLA and give each queue its own worker (e.g. `critical`/`slow`/`fast`).
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

## Deployment the durability contract depends on

- A real broker (Redis/RabbitMQ).
- Celery `task_acks_late=True` **and** `task_reject_on_worker_lost=True` (re-
  deliver a killed worker's task), plus a **single beat** scheduling the four
  `django_logic.*` safety-net tasks on `STARTER_QUEUE`. A worker for every
  queue you use.
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
- Read `docs/PLAN.md`, `docs/design/BACKGROUND_TRANSITION_ANALYSIS.md`, and the
  root `fundamental problem.md` before changing the background engine.

See `docs/IMPROVEMENTS_FROM_HEROKU_VALIDATION.md` for validated-behavior notes
and open improvement ideas.
