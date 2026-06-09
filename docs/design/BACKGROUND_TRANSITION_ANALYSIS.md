# Background Transition — Design & Failure Analysis

> Design document for `BackgroundTransition` / `BackgroundAction` in
> Django Logic v0.3.0. Read `PLAN.md` section 4 first; this file is the
> rationale and the crash-by-crash analysis behind the chosen design.

---

## 1. What background work has to survive

A background transition splits work across a process boundary:

```
Phase 1 (web process):  validate → record "this work needs to happen"
Phase 2 (Celery worker): run side-effects → write final state
```

Between those two phases, a production system will experience, in order
of likelihood: worker OOM/SIGKILL, deploys mid-task, broker restarts,
DB blips, network partitions, Celery pre-fetch losses, and the
occasional cosmic ray. The design question is: **what persists the
intent to run this work through all of those?**

Our answer: a DB row (`TransitionMessage`), written in the same atomic
block as the `in_progress_state` change, dispatched via
`transaction.on_commit` to a **user-declared queue**, and consumed by a
**single Celery task with `acks_late=True`** that owns the entire state
machine transition — side-effects and final state change both.

The same design supports a **second execution mode for tests and
scripts** that runs phase 2 inline in the same process, with no Celery
broker required — see §6.

---

## 2. The chosen design in one picture

```
┌─────────────────────────── PHASE 1 (web process) ───────────────────────┐
│                                                                          │
│  instance.orders.fulfil(user=u)                                          │
│      │                                                                   │
│      ▼                                                                   │
│  validate conditions + permissions                                       │
│      │                                                                   │
│      ▼                                                                   │
│  atomic {                                                                │
│      set_state(in_progress_state)              ── DB + RedisState        │
│      TransitionMessage.objects.create(                                   │
│          app_label, model_name, instance_id,                             │
│          process_name, transition_name,                                  │
│          queue_name=<transition.queue>,        ── REQUIRED, no default   │
│          kwargs=<serialized>,                                            │
│      )                                                                   │
│  }                                                                       │
│      │                                                                   │
│      ▼                                                                   │
│  transaction.on_commit(                                                  │
│      lambda: run_background_transition                                   │
│          .apply_async(args=[tm.id], queue=tm.queue_name)                 │
│  )                                                                       │
│      │                                                                   │
│      ▼                                                                   │
│  return tr_id to caller                                                  │
└──────────────────────────────────────────────────────────────────────────┘

┌────────────────── PHASE 2 (Celery task, acks_late=True) ────────────────┐
│                                                                          │
│  @shared_task(acks_late=True)                                            │
│  def run_background_transition(transition_message_id):                   │
│      with atomic():                                                      │
│          tm = TransitionMessage.objects                                  │
│              .select_for_update(nowait=True)                             │
│              .get(id=transition_message_id, is_completed=False)          │
│          # OperationalError → another worker has it, exit silently       │
│                                                                          │
│          instance, process, transition = restore(tm)                     │
│                                                                          │
│          try:                                                            │
│              for side_effect in transition.side_effects.commands:        │
│                  side_effect(instance, **tm.kwargs)                      │
│          except Exception as e:                                          │
│              tm.errors_count += 1                                        │
│              tm.last_error_message = str(e)                              │
│              tm.last_error_dt = timezone.now()                           │
│              if tm.errors_count >= MAX_ERRORS:                           │
│                  process.state.set_state(transition.failed_state)        │
│                  tm.mark_as_completed()                                  │
│              else:                                                       │
│                  tm.save()   # leave uncompleted → starter will retry    │
│              raise                                                       │
│          else:                                                           │
│              process.state.set_state(transition.target)                  │
│              tm.mark_as_completed()                                      │
│                                                                          │
│      # outside atomic, best-effort:                                      │
│      if tm.is_completed and state == target:                             │
│          callbacks.execute(...)                                          │
│          next_transition.execute(...)                                    │
│      elif tm.is_completed and state == failed_state:                     │
│          failure_callbacks.execute(...)                                  │
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────── SAFETY NET (periodic task on STARTER_QUEUE) ────────────┐
│                                                                          │
│  @shared_task  # retry_stale_transitions, every 2 min                    │
│  def retry_stale_transitions():                                          │
│      for tm in TransitionMessage.objects.filter(                         │
│          is_completed=False,                                             │
│          errors_count__lt=MAX_ERRORS,                                    │
│          created__lt=now - RETRY_MINUTES,                                │
│      ):                                                                  │
│          run_background_transition.apply_async(                          │
│              args=[tm.id], queue=tm.queue_name,                          │
│          )                                                               │
└──────────────────────────────────────────────────────────────────────────┘
```

Three properties this gives you, effectively for free:

1. **The intent to do the work is a DB row.** If Celery loses the
   message, if the worker dies, if `on_commit` never fires — the row
   exists, and the periodic starter re-dispatches it. The broker is
   demoted from source-of-truth to fast path.
2. **The entire state machine transition is one atomic unit per attempt.**
   Either side-effects + target-state write all commit, or none of
   them do and the task retries. There is no "side effects succeeded,
   state never updated" stuck state.
3. **Every transition runs on exactly the queue its author chose.**
   No `DJANGO_LOGIC['CELERY_QUEUE']` default to quietly re-route work
   when a new transition is added. Missing `queue=` is a boot-time
   error, not a production surprise.

Two smaller rules keep the design honest:

- **`in_progress_state` is unique within a Process.** Validated at
  class-creation time. Phase 2 can then find its transition by the
  in-progress state alone, without the `ignore_sources=True` hack.
  Operational bonus: a glance at `state='fulfilling'` tells you
  exactly which transition is mid-flight.
- **`BackgroundAction` uses the same durable path.** No state change
  on success, but same `TransitionMessage` row, same retry, same
  concurrency guard. A background action that bypassed the DB record
  would be fire-and-forget in disguise, which we rejected.

---

## 3. Crash-point analysis

The full execution has 13 numbered steps. A crash can happen between
any two of them. This table is the exhaustive list.

```
PHASE 1 (web):
  ① validate
  ② atomic { set_state(in_progress_state); create TransitionMessage }
  ③ transaction.on_commit → dispatch Celery task

PHASE 2 (task, inside atomic block):
  ④ fetch TransitionMessage with select_for_update(nowait=True)
  ⑤ restore instance + transition
  ⑥ run side_effect_1
  ⑦ run side_effect_2
  ⑧ run side_effect_N
  ⑨ set_state(target)                    (success path)
     or set_state(failed_state)          (failure path, at max errors)
  ⑩ mark TransitionMessage as completed

PHASE 3 (task, outside atomic block — BEST EFFORT):
  ⑪ callbacks / failure_callbacks
  ⑫ next_transition
```

### Crash table

| Crash point | What survives | Recovery |
|---|---|---|
| Between ① and ② | Nothing changed | HTTP caller gets error. Clean. |
| During ② (inside atomic) | Nothing | DB rollback. Clean. |
| Between ② and ③ (commit OK, `on_commit` dropped) | TM row, `in_progress_state` | **Automatic.** Periodic starter re-dispatches in ~`RETRY_MINUTES`. |
| During ④ with another worker holding the lock | TM row | **Automatic.** Losing worker exits silently; holding worker finishes. |
| During ⑥–⑧ (worker dies mid-side-effects) | TM row, `in_progress_state`, partial side-effects | **Automatic.** Starter re-dispatches. Side-effects re-run from scratch → **must be idempotent**. |
| During ⑨–⑩ (target written, mark not committed) | Nothing — whole atomic block rolls back | **Automatic.** Starter re-dispatches. Side-effects re-run, state re-written. |
| Between atomic commit and ⑪ (callbacks) | State is target/failed, TM completed | **Lost.** Callbacks do not run. Documented best-effort. |
| During ⑪–⑫ (callbacks, next_transition) | State correct, TM completed | **Lost.** Same as above. |

### What this means for users

- **Side-effects must be idempotent.** Any call like "reserve stock"
  must tolerate "already reserved". Any external API call must be
  keyed so replays are safe. This is non-negotiable.
- **Put critical work in side-effects, never in callbacks.** Anything
  that must run for correctness belongs before ⑩.
- **If a follow-up step is critical, chain another `BackgroundTransition`.**
  The second transition has its own TM row and its own retry. The
  "best-effort" boundary is only at the callback layer.
- **`failure_side_effects` run inside the atomic block**, before the
  TM is marked completed. They are part of the retried flow.
  `failure_callbacks` run after and are best-effort.

---

## 4. Why not the rejected alternatives

Briefly, for the record:

| Approach | Why rejected |
|---|---|
| **Fire-and-forget** (PR #75's `run_in_background`, GV's `BackgroundTransition`) | Worker crash, broker loss, or dropped `on_commit` all leave the model stuck in `in_progress_state` with zero recovery. Every interesting failure case needs manual intervention. |
| **Celery chain-of-tasks** (legacy `django-logic-celery`) | A crash between side-effect N and N+1 leaves the model stuck mid-flight. Interacts badly with the nested-transition re-raise problem documented in `fundamental problem.md`. Removed from this workspace. |
| **DB-backed MQ with separate handler task, side-effects outside atomic** (earlier iteration of the design) | Side-effects succeeding but the state write being lost to a worker crash is the worst failure mode — invisible inconsistency. Moving the state write into the same atomic block as side-effects fixes it. |

---

## 5. Reliability contract (the user-facing version)

```
┌─────────────────────────────────────────────────────┐
│  GUARANTEED (survives any crash):                    │
│                                                      │
│  ✓ State reaches target OR failed_state              │
│  ✓ Side-effects retried from scratch until success   │
│    or max errors                                     │
│  ✓ No two workers run the same transition at once    │
│  ✓ queue_name is preserved across retries            │
│  ✓ errors_count and last_error_message recorded      │
│                                                      │
├─────────────────────────────────────────────────────┤
│  BEST EFFORT (may be lost on worker crash):          │
│                                                      │
│  ⚠ Callbacks (run after the atomic block)            │
│  ⚠ Failure callbacks                                 │
│  ⚠ next_transition                                   │
│                                                      │
├─────────────────────────────────────────────────────┤
│  USER OBLIGATIONS:                                   │
│                                                      │
│  ! Every BackgroundTransition declares queue=        │
│  ! Side-effects MUST be idempotent                   │
│  ! Critical work goes in side-effects,               │
│    not callbacks                                     │
│  ! If a callback MUST run, chain another             │
│    BackgroundTransition                              │
└─────────────────────────────────────────────────────┘
```

---

## 6. Queue strategy

Explicit queues per transition are how you prevent a slow workload
(exports, bulk syncs) from starving a fast one (notifications, status
updates). The framework enforces the "no default" part; the queue
layout is the user's decision.

### Rule the framework enforces

`BackgroundTransition(queue=<str>)` is required. Omission raises
`ImproperlyConfigured` at import. There is no `CELERY_QUEUE` setting
that supplies a fallback.

### Suggested layout (guidance only, not shipped as default)

```
django_logic.fast       — < 1s work, high concurrency
                          notifications, cache updates, tracking events
django_logic.critical   — user-facing with SLA
                          fulfilment, payment authorisation, checkout
django_logic.slow       — > 30s work, low concurrency
                          exports, reports, bulk imports
django_logic.starter    — the framework's own periodic tasks
                          (retry_stale_transitions, cleanup, detect_stuck)
```

Worker configuration matches resource profile to queue:

```bash
celery -A myapp worker -Q django_logic.fast -c 8 --prefetch-multiplier 4
celery -A myapp worker -Q django_logic.critical -c 4 --prefetch-multiplier 1
celery -A myapp worker -Q django_logic.slow -c 1 --prefetch-multiplier 1
celery -A myapp worker -Q django_logic.starter -c 1
```

Crucially, the periodic starter **re-dispatches each message to its
own `queue_name`**. A retried slow export always goes back to the slow
queue. Retries never jump queues.

### Priority within a queue

Celery queues are FIFO. If you need priority, use sub-queues:

```python
BackgroundTransition(
    action_name='fulfil_vip',    queue='django_logic.critical.high',   ...
)
BackgroundTransition(
    action_name='fulfil_standard', queue='django_logic.critical.normal', ...
)
```

with workers consuming both but preferring the high queue:

```bash
celery -A myapp worker -Q django_logic.critical.high,django_logic.critical.normal
```

### What the framework does not do

- **Per-client sequential queues.** If you need
  "one fulfilment at a time per client", build it on top of the
  `queue=` hook in your project. Not a framework concern.
- **Rate limiting.** Use Celery's `rate_limit`.
- **Dynamic scaling.** Use Kubernetes HPA / Celery autoscale.
- **Queue monitoring UI.** Use Flower / Datadog / your ops stack.

---

## 7. Settings reference

```python
DJANGO_LOGIC = {
    'LOCK_TIMEOUT': 7200,

    # Required. Framework's own periodic tasks run here.
    'STARTER_QUEUE': 'django_logic.starter',

    'TRANSITION_MESSAGE_MAX_ERRORS': 5,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}
```

No `CELERY_QUEUE` default. Every `BackgroundTransition` carries its own
queue.

---

## 8. Execution modes — Celery and Sync

The same `BackgroundTransition` definition runs under two modes,
selected by `DJANGO_LOGIC['BACKGROUND_EXECUTION']` (`'celery'` or
`'sync'`) or per-block via `sync_execution()`.

### Where they differ

Only phase 2's dispatch changes. Phase 1 is identical.

```
Celery mode:
  atomic { set_state(in_progress_state); create TransitionMessage }
  transaction.on_commit(lambda:
      run_background_transition.apply_async(args=[tm.id], queue=tm.queue_name)
  )
  return tr_id                              # caller sees 200 immediately

Sync mode:
  atomic { set_state(in_progress_state); create TransitionMessage }
  run_background_transition(tm.id)          # inline, same process, same thread
  return tr_id                              # caller sees 200 only after phase 2
```

Phase 2 itself — the atomic block, the `select_for_update`, the
side-effects, the state write, the mark-completed — is the *exact same
function* in both modes. That's a deliberate design constraint:
whatever behaviour tests verify in Sync mode is the behaviour that
will run under Celery.

### Why this matters in practice

| | Celery mode | Sync mode |
|---|---|---|
| Phase 1 return | Immediate | After phase 2 completes |
| Side-effect exceptions | Logged + recorded on TM; caller already got 200 | Propagated to the caller |
| Retry on failure | Periodic starter | Not automatic |
| Worker isolation | Yes | No — same process |
| `on_commit` in use | Yes | No — bypassed |
| Works in `TestCase` | No (transaction never commits) | Yes |
| Celery required | Yes | No |

### Why not `CELERY_TASK_ALWAYS_EAGER`

`task_always_eager` still runs through Celery's machinery:

- You must have Celery installed. For consumer projects that want to
  unit-test business processes without pulling in Celery, this is
  prohibitive.
- Kwargs go through Celery's serialization layer, which papers over
  "this kwarg actually isn't JSON-serializable" bugs. Our Sync mode
  uses the same dispatch path as Celery mode — kwargs are serialized
  into the TransitionMessage, then read back before phase 2 — so
  serialization bugs are caught in tests instead of at 2 a.m. in
  production.
- `task_always_eager` does not bypass `transaction.on_commit`. Under
  `TestCase`, the wrapping transaction never commits, so `on_commit`
  never fires, so the task never dispatches. Sync mode removes the
  `on_commit` dance entirely for phase 2.

### Where Sync mode belongs

- **Unit tests.** Every business-process test in the consumer project
  can now call `instance.orders.fulfil(...)` and immediately assert on
  the post-phase-2 state. No broker, no worker, no `freezegun` on
  `on_commit`.
- **Management commands.** One-shot data fixes, backfills, admin
  scripts. Set `BACKGROUND_EXECUTION='sync'` in the management command
  context or wrap the call in `sync_execution()`.
- **Django shell / REPL.** For debugging a single transition without
  touching the Celery cluster.
- **CI.** Test matrices don't need a Redis/RabbitMQ container.

### Exceptions propagate — by design

In Celery mode the HTTP caller gets 200 the moment phase 1 commits.
Phase 2's errors live in logs and on the TransitionMessage. In Sync
mode the call doesn't return until phase 2 finishes, and any
unhandled exception from a side-effect propagates to the caller. This
is the right default for tests (`assertRaises(StripeAPIException)`
is the thing you want to write). Management commands can catch the
exception themselves if they want production-shaped behaviour.

### Concurrency is still enforced in Sync mode

The partial unique constraint on `TransitionMessage` and the
`select_for_update(nowait=True)` still run. Sync mode doesn't weaken
the concurrency contract — it just removes the broker hop.

### `ImproperlyConfigured` paths

- `BACKGROUND_EXECUTION='celery'` but Celery isn't installed → raised
  at Django app-ready time.
- `BACKGROUND_EXECUTION='celery'` but `STARTER_QUEUE` not set → raised
  at Django app-ready time.
- `BackgroundTransition` missing `queue=` → raised at class creation.
- Two transitions on the same `Process` sharing an `in_progress_state`
  → raised at class creation.

---

## 9. Open questions

1. **Callback durability.** Current design: best-effort. If we ever want
   fully durable callbacks, the path is to add a `phase` column to
   `TransitionMessage` (`side_effects` → `target_set` → `callbacks` →
   `done`) and gate the periodic starter on phase. Deferred to v1.0+;
   the chain-another-`BackgroundTransition` pattern covers the cases
   that need it.
2. **Checkpointing within side-effects.** No. Users require
   idempotency instead. Revisit if a real-world workflow makes this
   untenable.
3. **Non-Celery backends.** The dispatch path is the only Celery-aware
   code. The introduction of Sync mode is half of that abstraction
   already; if there is demand later, we can formalise the interface
   and add an RQ / Dramatiq backend. Not for v0.3.0.
