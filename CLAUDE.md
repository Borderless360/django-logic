# django-logic — guidance for AI assistants

This repo is the **django-logic** library: declarative business logic and state
machines for Django, with durable, queue-routed background transitions
(`django_logic.background`). This file tells an AI how to **use the library
correctly** when generating or reviewing code that depends on it. The rules
below are distilled from a full
production-style validation on Heroku (RabbitMQ + PostgreSQL + multiple
workers + induced worker crashes, deploys, broker loss, and pgbouncer).

## Voice — simplified English, everywhere

This library was largely written by AI tools, and much of the prose became a
private dialect: numbered "phases", "liveness", "retry horizon", ticket ids in
comments. 0.14.0 removed it. **Do not bring it back.**

Write simplified technical English, the way ASD-STE100 defines it:

- One idea per sentence. Aim for 20 words. Never pass 25.
- Active voice, present tense. "The worker writes the row", not "the row is
  written by the worker".
- One word for one meaning. Do not use two words for the same thing.
- Use a verb, not a noun built from a verb. "when it fails", not "on failure of".
- No metaphors and no jokes. Say the mechanism.
- Names are full words: `transition_message`, not `tm`.
- A comment gives a non-obvious *why* in one or two sentences. It never narrates
  the next line, and it never cites a GitHub issue, a PR, or a check id (`#195`,
  `W002`). `CHANGELOG.md` owns that history.
- Do not point at a design-document section number (`§2.7`, `D2 (c)`,
  `contract 7`). State the rule itself in one clause.
- Exception and log text is for the operator on call at 3am. Say what happened
  and what to do.
- If you would not say it out loud, rewrite it.
- When you touch a file, rewrite dialect in the lines you touch.

**Allowed words:** transition, background transition, `TransitionMessage`,
source state, retry window, stranded (nothing is retrying it), enqueue (save
the row and send it to the queue), execute (the worker runs the side-effects),
uncompleted, in progress.

**Retired words, and what to write instead:**

| Do not write | Write |
|---|---|
| `phase 1`, `phase one`, `phase-1` | enqueue |
| `phase 2`, `Phase2`, `phase-2` | execute |
| `liveness` | whether the row is still being retried |
| `retry horizon` | retry window |
| `re-drive`, `redrive` | re-dispatch |
| `in-flight marker` | the uncompleted row |
| `speculative-insert` | describe the insert wait plainly |
| `owning process` | the process that declares the transition |
| `finishing flight` | an attempt that is still running |
| `TM`, `TM-scoped` | `TransitionMessage`, scoped to the row |
| `tm`, `msg`, `inst` | `transition_message`, `message`, `instance` |

`tests/test_voice.py` enforces this table over the library and the current
docs. It skips `CHANGELOG.md` on purpose: the changelog is a historical
record, so it must keep naming the words and APIs that shipped at the time.

## What to generate

Model a workflow as a `Process` subclass: a list of `transitions` (edges).
Each transition has `sources`, `target`, and optional `conditions`,
`permissions`, `side_effects`, `callbacks`, `failure_callbacks`. **Bind the model to its process in exactly one place — the
app's `AppConfig.ready()`** — with
`ProcessManager.bind_model_process(Model, MyProcess, state_field='status')`
(import the model and process *inside* `ready()`). Never bind at module import
time in `models.py`/`process.py`: that forces a
`model → process → actions → model` circular import, because the
process and its action functions both reference the model. `ready()` runs after
every app's models are loaded, so the cycle never forms and action modules can
import the model at the top level. Then drive it via
`instance.process.<action>(...)` from request/task/method bodies (never at
module top or in another app's `ready()`).

Use `BackgroundTransition` (durable, runs side-effects on a Celery worker,
writes target/`failed_state`) or `BackgroundAction` (same durability, no state
change on success) for anything slow, external, or retriable.

**Declarations are specifications.** Write every process and its transitions
out in full — explicit `sources`, `target`, `conditions`, `side_effects`,
`callbacks` per declaration, even when sibling processes repeat one shape.
Duplication in declarations is acceptable and usually preferable: it tells
how the process behaves; a builder that assembles transitions tells how the
code works and hides the line a reviewer came to check. The same rule holds
at usage sites: drive `instance.process.action(...)` literally, never through
name-string dispatch.

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
4. **Set a `failed_state`** so failures are contained. `in_progress_state`
   is **background-only** (0.12.0): on a `BackgroundTransition` it is written
   in the same transaction as the `TransitionMessage` row; declaring it on a
   synchronous `Transition`/`Action` raises at class creation. It may be shared
   freely — every marked instance carries its exact transition on the row, so
   recovery never guesses which transition it belongs to (the old
   `django_logic.E001` check is retired). A synchronous "busy" step is a real
   state: a fast transition into it chained via `next_transition` to a
   `BackgroundTransition` that does the work, plus a small periodic retry
   for the crash window (see the README migration note).
5. **Test in sync mode**: `DJANGO_LOGIC['BACKGROUND_EXECUTION']='sync'` (or the
   `sync_execution()` context manager) runs the worker path inline with no
   broker and propagates exceptions; `retry_pending()` simulates the periodic
   starter. The global default is `'celery'`, so test settings must opt into
   sync. See `docs/TESTING_GUIDE.md` for the full scenario catalog.
6. **One uncompleted background transition per instance per process.** While an
   uncompleted `TransitionMessage` exists, a second background transition
   raises `AlreadyInProgress` and a *synchronous* transition on the same
   instance+process raises `TransitionTemporarilyUnavailable` (both subclass
   `TransitionNotAllowed`; catch the transient type first to answer
   "retry shortly") — design flows so follow-up work chains from terminal
   hooks, not while another transition is still running. A failing `Action`'s
   `failed_state` write is skipped while the row is uncompleted: the worker
   owns the state field.
7. **Manual state fixes win.** If an instance is moved externally while a
   background row is pending, the worker completes the row as *superseded*
   (`'[superseded]'` in `last_error_message`) and skips side-effects. This is
   unconditional since 0.10.0.

## The architecture rule (locked — do not drift back)

One rule decides every reliability design here: **the cache may lie briefly
(a lock key expires); the database row never lies (it is written atomically
with the state change); everything recoverable recovers from a row.**
Its corollaries, each pinned by a shipped mistake:

- **No durable busy marker without a durable owner.** A busy state written
  to the database with no row that owns its recovery parks the instance
  forever when a worker is hard-killed. That design shipped once, grew a
  sweeper whose defects dominated four review passes, and was cut in
  0.12.0. Do not rebuild it.
- **The mutex stays in the cache, and refusal is instant.** A conditional
  UPDATE is a durable busy marker; a row lock held inside a transaction
  keeps a connection idle-in-transaction across external side effects
  (fatal under pgbouncer) and turns instant refusal into blocking.
- **The row names its transition.** Recovery re-runs work from
  `TransitionMessage` rows, never from broker messages, so the row records
  `transition_name` and `owning_process_class`. Never switch to one Celery
  task per transition: a task name inside a broker message turns a rename
  deploy into silent message loss; the one shared task fails loudly
  (`[unrestorable]`).

## Release policy (anti-spiral)

Measured on 0.10.0 → 0.13.1: +1,263 net engine lines in 13 days, ~90% of it
guarding the machinery's own failure modes; 68.9% of inserted lines fixed the
same release's own fixes. Therefore:

1. **No hardening without a consumer-reproduced failure.** A guard, knob,
   validator or doc lands only for a defect a consumer hit or reproduced —
   never because a review imagined one. Corollary: do not validate,
   document or optimize a knob until a consumer sets it.
2. **Adversarial self-review is capped at one pass per release.** Findings
   from later passes become issues for the next consumer-driven release;
   they are not fixed in place.
3. **A third fix on the same defect class triggers a design cut, not a
   fourth fix.** Precedent: 0.12.0 removed the stranded sweep (−582) after
   four passes kept finding defects there — the cut ended the incident
   class where hardening had not.

## Deployment the durability contract depends on

- A real broker (Redis/RabbitMQ). Celery is a core dependency of
  django-logic (installed automatically); `BACKGROUND_EXECUTION` defaults
  to `'celery'`.
- A cross-process `default` cache for the state lock — celery mode refuses
  to boot with a locmem/dummy cache when `DEBUG=False`. The engine locks
  through Django's cache API and imports no backend, so Django's built-in
  `django.core.cache.backends.redis.RedisCache` is enough; django-redis is
  the `[redis]` extra, not a core dependency (0.11.0).
- Crash re-delivery is built in (every django-logic task sets
  `acks_late=True` + `reject_on_worker_lost=True`); set the global Celery
  pair only for your *own* tasks. You still need a **single beat**
  scheduling the four `django_logic.*` periodic tasks — and a worker for
  every queue you use. Install them by writing the `CELERY_`-namespaced key,
  because a plain `app.conf.beat_schedule = …` assignment is silently ignored
  when the project also defines `CELERY_BEAT_SCHEDULE` in Django settings:

  ```python
  from django_logic.background import beat_schedule
  app.conf['CELERY_BEAT_SCHEDULE'] = {
      **(app.conf.beat_schedule or {}), **beat_schedule(),
  }
  ```

  `manage.py check` reports missing entries as `django_logic.W002` — a
  *warning*, so it does not fail the command unless you run
  `check --fail-level WARNING`.
- Behind **pgbouncer transaction pooling**: `OPTIONS={'prepare_threshold':
  None}`, `DISABLE_SERVER_SIDE_CURSORS=True`, and no SSL on the app→pgbouncer
  hop. The concurrency guard (`select_for_update(nowait)` + partial-unique)
  then holds.

## Working IN this repo

- Tests: `python tests/manage.py test` (SQLite suite, also `make test`);
  PostgreSQL concurrency + stability suites under `tests/stability`,
  `tests/background`. There is no pytest configuration — do not add one
  without wiring `DJANGO_SETTINGS_MODULE`.
- `django_logic/background/` is the durable engine: `transitions.py`
  (enqueue), `dispatch.py`, `runner.py` (execute on the worker), `tasks.py`
  (Celery + periodic), `models.py` (`TransitionMessage`), `settings.py`.
- Read `docs/design/BACKGROUND_TRANSITION_ANALYSIS.md` and
  `docs/recipes/nested-processes.md` (the fan-out pattern and the
  cascading-failure anti-pattern it replaces) before changing the
  background engine.
- `CHANGELOG.md` is the record of what shipped and why; `TODO.md` holds what
  has not. Neither is a design document — do not add planning docs that
  duplicate them.
