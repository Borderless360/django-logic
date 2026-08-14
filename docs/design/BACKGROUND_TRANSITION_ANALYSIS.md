# Background Transition — Design & Failure Analysis

> Design document for `BackgroundTransition` / `BackgroundAction`, first
> shipped in Django Logic v0.3.0. This file records the rationale and the
> crash-by-crash analysis behind the design. The README documents the
> resulting API.

---

## 1. What background work has to survive

A background transition splits the work across a process boundary:

```
enqueue (web process):    validate, then record that this work must happen
execute (Celery worker):  run the side-effects, then write the final state
```

Between enqueue and execute a production system fails in many ways. These
are the ones we see, roughly in order of how often they happen:

- worker OOM or SIGKILL
- a deploy in the middle of a task
- a broker restart
- a database blip
- a network partition
- a Celery pre-fetch loss

The design question is one sentence: what keeps the intent to run this
work through all of that?

Our answer is a database row, `TransitionMessage`. The web process writes
it in the same atomic block as the `in_progress_state` change.
`transaction.on_commit` then sends it to a queue the transition author
names. A single Celery task with `acks_late=True` consumes it and owns the
whole transition — the side-effects and the final state write.

The same design carries a second execution mode for tests and scripts. It
runs execute inline in the same process and needs no Celery broker — see
[section 8](#8-execution-modes-celery-and-sync).

---

## 2. The chosen design in one picture

```
┌─── ENQUEUE (web process) ──────────────────────────────────────────────────┐
│                                                                            │
│  instance.orders.fulfil(user=user)                                         │
│      │                                                                     │
│      ▼                                                                     │
│  validate conditions and permissions                                       │
│      │                                                                     │
│      ▼                                                                     │
│  atomic {                                                                  │
│      set_state(in_progress_state)              ── database                 │
│      TransitionMessage.objects.create(                                     │
│          app_label, model_name, instance_id,                               │
│          process_name, transition_name,                                    │
│          queue_name=<transition.queue or DEFAULT_QUEUE>,                   │
│          kwargs=<serialized>,                                              │
│      )                                                                     │
│  }                                                                         │
│      │                                                                     │
│      ▼                                                                     │
│  transaction.on_commit(                                                    │
│      lambda: run_background_transition.apply_async(                        │
│          args=[transition_message.id],                                     │
│          queue=transition_message.queue_name,                              │
│      )                                                                     │
│  )                                                                         │
│      │                                                                     │
│      ▼                                                                     │
│  return the transition id to the caller                                    │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘

┌─── EXECUTE (Celery task, acks_late=True) ──────────────────────────────────┐
│                                                                            │
│  @shared_task(acks_late=True)                                              │
│  def run_background_transition(transition_message_id):                     │
│      with atomic():                                                        │
│          transition_message = (                                            │
│              TransitionMessage.objects                                     │
│              .select_for_update(nowait=True)                               │
│              .get(pk=transition_message_id, is_completed=False)            │
│          )                                                                 │
│          # OperationalError: another worker holds the row, so exit         │
│                                                                            │
│          instance, process, transition = restore(transition_message)       │
│                                                                            │
│          try:                                                              │
│              for side_effect in transition.side_effects.commands:          │
│                  side_effect(instance, **transition_message.kwargs)        │
│          except Exception as error:                                        │
│              transition_message.errors_count += 1                          │
│              transition_message.last_error_message = str(error)            │
│              transition_message.last_error_dt = timezone.now()             │
│              if transition_message.errors_count >= MAX_ERRORS:             │
│                  process.state.set_state(transition.failed_state)          │
│                  transition_message.mark_as_completed()                    │
│              else:                                                         │
│                  # stays uncompleted, so the starter retries it            │
│                  transition_message.save()                                 │
│              raise                                                         │
│          else:                                                             │
│              process.state.set_state(transition.target)                    │
│              transition_message.mark_as_completed()                        │
│                                                                            │
│      # outside the atomic block, best effort:                              │
│      if transition_message.is_completed and state == target:               │
│          callbacks.execute(...)                                            │
│          next_transition.execute(...)                                      │
│      elif transition_message.is_completed and state == failed_state:       │
│          failure_callbacks.execute(...)                                    │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘

┌─── SAFETY NET (periodic task on STARTER_QUEUE) ────────────────────────────┐
│                                                                            │
│  @shared_task  # retry_stale_transitions, every 2 minutes                  │
│  def retry_stale_transitions():                                            │
│      for transition_message in TransitionMessage.objects.filter(           │
│          is_completed=False,                                               │
│          errors_count__lt=MAX_ERRORS,                                      │
│          created__lt=now - RETRY_MINUTES,                                  │
│      ):                                                                    │
│          run_background_transition.apply_async(                            │
│              args=[transition_message.id],                                 │
│              queue=transition_message.queue_name,                          │
│          )                                                                 │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

This gives you three properties, effectively for free:

1. **The intent to do the work is a database row.** The row outlives a
   lost Celery message, a dead worker, and an `on_commit` that never
   fires. The periodic starter sends the row to the queue again. The
   broker is a fast path, not the source of truth.
2. **Each attempt is one atomic unit.** Either the side-effects and the
   target-state write both commit, or neither does and the task retries.
   The instance never sits in a state where the side-effects ran but the
   state never changed.
3. **Every transition runs on a queue its author can name.** `queue=` is
   optional; a transition without one goes to
   `DJANGO_LOGIC['DEFAULT_QUEUE']` (`'django_logic'`). Give heavy or
   SLA-sensitive transitions their own queue and a dedicated worker, so a
   saturated slow queue cannot delay critical work.

Two smaller rules keep the design honest:

- **`in_progress_state` need not be unique, and only a background
  transition may declare it.** Execute restores the transition from the
  process and transition names on the `TransitionMessage`, not from the
  state. Two transitions may therefore share the state, and the engine
  needs no ownership rule (the old `django_logic.E001` check is retired).
  Since 0.12.0 a synchronous transition cannot declare the state at all.
  A killed synchronous run rolls back to its source state, and the caller
  can start it again. Nothing needs a sweep, so `recover_stranded_states`
  is retired with it.
- **`BackgroundAction` uses the same durable path.** It changes no state
  on success, but it writes the same `TransitionMessage` row and gets the
  same retry and the same concurrency guard. A background action without
  the row would be fire-and-forget in disguise, and we rejected that.

---

## 3. Crash-point analysis

The full execution has twelve numbered steps. A crash can happen between
any two of them. The table below is the exhaustive list.

```
ENQUEUE (web process):
  ① validate
  ② atomic { set_state(in_progress_state); create TransitionMessage }
  ③ transaction.on_commit sends the Celery task

EXECUTE (Celery task, inside the atomic block):
  ④ fetch the TransitionMessage with select_for_update(nowait=True)
  ⑤ restore the instance and the transition
  ⑥ run side_effect_1
  ⑦ run side_effect_2
  ⑧ run side_effect_N
  ⑨ set_state(target)                    (success path)
     or set_state(failed_state)          (failure path, at max errors)
  ⑩ mark the TransitionMessage as completed

AFTER THE ATOMIC BLOCK (same task, best effort):
  ⑪ callbacks / failure_callbacks
  ⑫ next_transition
```

### Crash table

| Crash point | What survives | Recovery |
|---|---|---|
| Between ① and ② | Nothing changed | The HTTP caller gets an error. Clean. |
| During ② (inside the atomic block) | Nothing | The database rolls back. Clean. |
| Between ② and ③ (the commit lands, `on_commit` never fires) | The row and `in_progress_state` | **Automatic.** The periodic starter sends the row to the queue again within about `RETRY_MINUTES`. |
| During ④ while another worker holds the lock | The row | **Automatic.** The worker that loses the race exits silently, and the worker holding the lock finishes. |
| During ⑥–⑧ (the worker dies while side-effects run) | The row, `in_progress_state`, and the side-effects that already ran | **Automatic.** The starter sends the row to the queue again. Side-effects re-run from the start, so they **must be idempotent**. |
| During ⑨–⑩ (the worker writes the target state, the completion never commits) | Nothing — the whole atomic block rolls back | **Automatic.** The starter sends the row to the queue again. Side-effects re-run and the worker writes the state again. |
| Between the atomic commit and ⑪ (callbacks) | The state is target or failed_state, and the row is completed | **Lost.** The callbacks do not run. This is the documented best-effort boundary. |
| During ⑪–⑫ (callbacks, next_transition) | The state is correct, and the row is completed | **Lost.** Same as above. |

### What this means for users

- **Side-effects must be idempotent.** A call like "reserve stock" must
  tolerate "already reserved". Every external API call needs a key that
  makes a replay safe. This rule is non-negotiable.
- **Put critical work in side-effects, never in callbacks.** Anything the
  result depends on belongs before step ⑩.
- **If a follow-up step is critical, chain another
  `BackgroundTransition`.** The second transition gets its own row and its
  own retry. Only the callback layer is best-effort.
- **`failure_callbacks` run after the atomic block**, so they are
  best-effort too.

---

## 4. Why not the rejected alternatives

Briefly, for the record:

| Approach | Why we rejected it |
|---|---|
| **Fire-and-forget** (the early `run_in_background` proposals) | A worker crash, a broker loss, or a dropped `on_commit` leaves the instance in `in_progress_state` with no recovery. Every interesting failure then needs a person. |
| **A Celery chain of tasks** (the legacy `django-logic-celery` package) | A crash between side-effect N and side-effect N+1 leaves the instance stuck part-way. It also worsens the nested-transition re-raise problem described in [`docs/recipes/nested-processes.md`](../recipes/nested-processes.md). That package is no longer part of this workspace. |
| **A database-backed queue with a separate handler task and side-effects outside the atomic block** (an earlier version of this design) | The worst failure we know is side-effects that succeed while a worker crash loses the state write. The data is then wrong and nothing reports it. Moving the state write into the same atomic block as the side-effects removes that failure. |

---

## 5. Reliability contract (the user-facing version)

```
┌────────────────────────────────────────────────────────────────────────────┐
│  GUARANTEED (survives any crash):                                          │
│                                                                            │
│  ✓ The state reaches target or failed_state                                │
│  ✓ Side-effects re-run from the start until they succeed or                │
│    reach max errors                                                        │
│  ✓ No two workers run the same transition at once                          │
│  ✓ A retry keeps the queue_name on the row                                 │
│  ✓ The row records errors_count and last_error_message                     │
│                                                                            │
├────────────────────────────────────────────────────────────────────────────┤
│  BEST EFFORT (a worker crash can lose these):                              │
│                                                                            │
│  ⚠ Callbacks, which run after the atomic block                             │
│  ⚠ Failure callbacks                                                       │
│  ⚠ next_transition                                                         │
│                                                                            │
├────────────────────────────────────────────────────────────────────────────┤
│  WHAT THE USER OWES:                                                       │
│                                                                            │
│  ! Route by SLA: name a queue for each heavy transition                    │
│  ! Side-effects must be idempotent                                         │
│  ! Critical work goes in side-effects, not in callbacks                    │
│  ! If a callback must run, chain another BackgroundTransition              │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 6. Queue strategy

A queue per transition is how you stop a slow workload (exports, bulk
syncs) from starving a fast one (notifications, status updates). The queue
layout is the user's decision.

### Rule the framework enforces

`BackgroundTransition(queue=<str>)` is optional, and the engine resolves it
at run time: a transition without a queue goes to
`DJANGO_LOGIC['DEFAULT_QUEUE']` (`'django_logic'`). The framework does
check that the safety-net tasks are scheduled — `django_logic.W002`
reports a beat schedule that never installed them. It cannot check that a
queue you name has a worker. Running that worker is your job.

### Suggested layout (guidance only, not shipped as default)

```
django_logic.fast       — work under 1s, high concurrency
                          notifications, cache updates, tracking events
django_logic.critical   — user-facing work with an SLA
                          fulfilment, payment authorisation, checkout
django_logic.slow       — work over 30s, low concurrency
                          exports, reports, bulk imports
django_logic.starter    — the framework's own four periodic tasks
                          (retry_stale_transitions,
                           cleanup_completed_transitions,
                           detect_stuck_transitions,
                           watchdog_stale_attempts)
```

Worker configuration matches the resource profile to the queue:

```bash
celery -A myapp worker -Q django_logic.fast -c 8 --prefetch-multiplier 4
celery -A myapp worker -Q django_logic.critical -c 4 --prefetch-multiplier 1
celery -A myapp worker -Q django_logic.slow -c 1 --prefetch-multiplier 1
celery -A myapp worker -Q django_logic.starter -c 1
```

The periodic starter sends each row back to the `queue_name` on the row.
A retried slow export returns to the slow queue. A retry never changes
queue.

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

Then run workers that consume both and prefer the high queue:

```bash
celery -A myapp worker -Q django_logic.critical.high,django_logic.critical.normal
```

### What the framework does not do

- **Per-client sequential queues.** If you need "one fulfilment at a time
  per client", build it on the `queue=` hook in your own project. It is
  not a framework concern.
- **Rate limiting.** Use Celery's `rate_limit`.
- **Dynamic scaling.** Use Kubernetes HPA or Celery autoscale.
- **A queue monitoring UI.** Use Flower, Datadog, or your own ops stack.

---

## 7. Settings reference

The README's settings block is the reference: it lists every key with its
default. This section repeats only the background-specific defaults, with
the values the code applies:

```python
DJANGO_LOGIC = {
    'LOCK_TIMEOUT': 7200,
    # Optional, like every key here — the default below applies when unset.
    # The framework's own periodic tasks run on this queue.
    'STARTER_QUEUE': 'django_logic.starter',
    'DEFAULT_QUEUE': 'django_logic',
    'TRANSITION_MESSAGE_MAX_ERRORS': 5,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}
```

`queue=` is optional on a `BackgroundTransition`: one declared without it
routes to `DEFAULT_QUEUE`.

`manage.py check` warns about a key this list does not contain
(`django_logic.W004`) and about a key an earlier release removed
(`django_logic.W003`). A typo never passes silently.

---

## 8. Execution modes: Celery and sync

The same `BackgroundTransition` definition runs under two modes.
`DJANGO_LOGIC['BACKGROUND_EXECUTION']` (`'celery'` or `'sync'`) selects
one, and `sync_execution()` selects sync for a single block.

### Where they differ

Only the dispatch of execute changes. Enqueue is identical.

```
Celery mode:
  atomic { set_state(in_progress_state); create TransitionMessage }
  transaction.on_commit(lambda:
      run_background_transition.apply_async(
          args=[transition_message.id],
          queue=transition_message.queue_name,
      )
  )
  return the transition id       # the caller sees 200 at once

Sync mode:
  atomic { set_state(in_progress_state); create TransitionMessage }
  run_background_transition(transition_message.id)   # same process and thread
  return the transition id       # the caller waits for execute to finish
```

Execute itself is the *same function* in both modes: the atomic block, the
`select_for_update`, the side-effects, the state write, and the
mark-completed. That is a deliberate design constraint. Whatever
behaviour a test proves in sync mode is the behaviour that runs under
Celery.

### Why this matters in practice

| | Celery mode | Sync mode |
|---|---|---|
| Enqueue returns | At once | After execute finishes |
| Side-effect exceptions | Logged and recorded on the row; the caller already got 200 | They reach the caller |
| Retry after a failure | The periodic starter | Not automatic |
| Worker isolation | Yes | No — the same process |
| Uses `on_commit` | Yes | No |
| Works in `TestCase` | No (the transaction never commits) | Yes |
| Needs Celery | Yes | No |

### Why not `CELERY_TASK_ALWAYS_EAGER`

`task_always_eager` still runs through Celery's machinery:

- Celery has to be installed. A consumer project that wants to unit-test
  its business processes without Celery cannot use it.
- Kwargs go through Celery's serialization layer, which hides a kwarg
  that is not JSON-serializable. Sync mode uses the same dispatch path as
  Celery mode. It serializes the kwargs into the `TransitionMessage` row
  and reads them back before execute. A serialization bug therefore fails
  in a test, not in production.
- `task_always_eager` does not bypass `transaction.on_commit`. Under
  `TestCase` the wrapping transaction never commits, so `on_commit` never
  fires and the task never dispatches. Sync mode drops the `on_commit`
  step for execute.

### Where sync mode belongs

- **Unit tests.** A test in the consumer project calls
  `instance.orders.fulfil(...)` and asserts on the state that execute
  wrote. No broker, no worker, and no clock patching around `on_commit`.
- **Management commands.** One-shot data fixes, backfills, admin scripts.
  Set `BACKGROUND_EXECUTION='sync'` for the command, or wrap the call in
  `sync_execution()`.
- **Django shell.** Debug a single transition without touching the Celery
  cluster.
- **CI.** A test matrix needs no Redis or RabbitMQ container.

### Exceptions propagate — by design

In Celery mode the HTTP caller gets 200 as soon as enqueue commits.
Execute then reports its errors to the logs and to the
`TransitionMessage` row. In sync mode the call returns only after execute
finishes, and an unhandled side-effect exception reaches the caller. That
is the right default for tests, where `assertRaises(StripeAPIException)`
is the assertion you want. A management command can catch the exception
itself if it wants production-shaped behaviour.

### Concurrency still holds in sync mode

Sync mode still applies the partial unique constraint on
`TransitionMessage` and the `select_for_update(nowait=True)`. It removes
the broker hop and nothing else.

### Where the engine raises `ImproperlyConfigured`

At Django app-ready time:

- `BACKGROUND_EXECUTION='celery'` while Celery is not installed.
- An invalid `DJANGO_LOGIC` value: a non-bool strict flag, a non-positive
  `LOCK_TIMEOUT`, or a `DJANGO_LOGIC` that is not a dict.
  `STARTER_QUEUE` has a default (`'django_logic.starter'`), so it is
  never one of these.

At class creation:

- An `action_name` that a `Process` attribute already uses.
- `sources` given as a bare string.
- `failed_state == in_progress_state`.

At `bind_model_process`:

- A `process_name` that already names something on the model's MRO.

Two transitions may share an `in_progress_state` and need no validation
since 0.12.0. Only a background transition declares the state. Every
marked instance carries its transition on the `TransitionMessage` row, so
recovery reads the row instead of guessing (`django_logic.E001` is
retired).

---

## 9. Open questions

1. **Callback durability.** The design is best-effort today. Fully durable
   callbacks would need a step column on `TransitionMessage`
   (`side_effects` → `target_set` → `callbacks` → `done`) and a periodic
   starter that reads it. Deferred to v1.0 and later; chaining another
   `BackgroundTransition` covers the cases that need it.
2. **Checkpoints inside side-effects.** No. Users make side-effects
   idempotent instead. Revisit if a real workflow makes idempotency
   impossible.
3. **Non-Celery backends.** The dispatch path is the only Celery-aware
   code, and sync mode is already half of that abstraction. If consumers
   ask for it, we can define the interface and add an RQ or Dramatiq
   backend. Not for v0.3.0.
