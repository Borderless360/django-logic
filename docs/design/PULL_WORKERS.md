# Pull workers — the design cut for the broker mirror

> Design record for issue #217. Status: **the shipped design as of 0.16.0**
> — pull is the default execution mode and the push machinery is removed.
> The push design it replaced is recorded in
> [BACKGROUND_TRANSITION_ANALYSIS.md](../history/BACKGROUND_TRANSITION_ANALYSIS.md),
> which stays as the crash-by-crash analysis this design answers to.

---

## 1. The problem is the mirror, not the code

The engine's truth is the `TransitionMessage` row. Execution is push: every
row is mirrored into a broker message, and a worker runs whatever the
broker delivers. Truth in two places needs reconciling, and the
reconciling machinery is most of the engine's size and most of its
incident history:

- the dispatcher (`transaction.on_commit` + `apply_async`),
- the periodic starter, for rows whose message was lost,
- the committed `started_at` stamp,
- the stuck-detector,
- the beat wiring — with its own defect class: the silently ignored
  schedule assignment, the interval that reset on every deploy (#203),
  the missing-entries check,
- and, in 0.15.0, the dispatch claim, the dispatch counter, the ceiling,
  and the refund — all bounding the mirror when nothing consumes it
  (#211, #215).

Five shipped incidents share one root: *the message and the row disagree*.
The release policy says a third fix on one defect class triggers a design
cut. This is that cut.

## 2. The design in one sentence

The committed row is the signal: a worker asks the database for one
claimable row, runs the existing execute path on it, and asks again — with
`LISTEN/NOTIFY` as the wake-up so the ask is immediate, and a slow poll as
the floor.

```
┌─── ENQUEUE (unchanged) ────────────────────────────────────────────┐
│ atomic { set_state(in_progress_state); create TransitionMessage } │
│ on commit: NOTIFY django_logic_work (best effort)                 │
└────────────────────────────────────────────────────────────────────┘
┌─── WORKER LOOP (new, replaces broker + starter) ───────────────────┐
│ wait for NOTIFY, or POLL_SECONDS, whichever comes first           │
│ claim: SELECT pk FROM transitionmessage                           │
│        WHERE is_completed = false                                 │
│          AND queue_name IN (my queues)                            │
│          AND errors_count < MAX_ERRORS                            │
│          AND (last_error_dt IS NULL                               │
│               OR last_error_dt < now - RETRY_MINUTES)             │
│        ORDER BY created                                           │
│        FOR UPDATE SKIP LOCKED LIMIT 1                             │
│ run_background_transition(pk)      ← the existing execute path    │
└────────────────────────────────────────────────────────────────────┘
```

The claim's WHERE clause **is** the retry rule. A fresh row is claimable
at once. A row whose attempt just failed becomes claimable again after
`RETRY_MINUTES` — no task has to re-dispatch it; it is simply visible
again. A row whose attempt is running right now is row-locked by that
attempt, so `SKIP LOCKED` passes over it. A worker that dies releases its
lock with its connection, so its row is claimable immediately — faster
than today's starter, which waits out the retry interval.

## 3. What each mechanism becomes

| Today (push) | Under pull |
|---|---|
| dispatcher: `on_commit` + `apply_async` | one best-effort NOTIFY |
| lost broker message + starter recovery | impossible — nothing is sent |
| periodic starter | the claim's WHERE clause |
| duplicate dispatches for a long attempt (#211) | impossible — `SKIP LOCKED` |
| unbounded publishing to a dead queue (#215) | impossible — nothing is published |
| dispatch claim, counter, ceiling, refund | deleted |
| "does that queue have a consumer?" | a query: uncompleted rows older than X in that queue |
| beat schedule + its checks | deleted — cleanup runs inside the worker loop |
| `timeout=` enforcement | moved into the worker: it kills an attempt process that runs past its budget (0.17.0; the watchdog scan is gone) |
| stuck-detector | a check at claim time, plus the never-started report |
| queue routing (broker queues) | a column filter: `--queues critical,fast` per worker process |
| `acks_late` / redelivery semantics | not needed — the row never left the database |

**Unchanged, because it is the product:** the declarations and the whole
consumer API, enqueue's atomic block and its concurrency guards, the
runner (savepoints, the state guard, the superseded rule, the failure
paths, permanent failures), sync mode, the testing package.

## 4. The crash table, revisited

Every recovery in the push design's crash table holds under pull, with
fewer mechanisms:

| Crash | Push recovery | Pull recovery |
|---|---|---|
| between commit and the send | starter re-dispatches within RETRY_MINUTES | nothing was sent; the row is already claimable; NOTIFY was best effort and the poll is the floor |
| worker dies mid side-effects | broker redelivery + starter | the attempt runs in its own forked attempt process: the crash kills that process, the worker records it as an error on the row, the retry wait paces the next claim, and MAX_ERRORS bounds a crash loop |
| two workers reach one row | `select_for_update(nowait)` skip | same guard, plus `SKIP LOCKED` prevents most collisions before they happen |
| attempt hangs but holds its connection | nothing could stop it (the watchdog only reached unlocked rows) | the worker kills the attempt process at its declared `timeout=` (0.17.0) |
| broker loses everything | starter rebuilds from rows | there is no broker to lose |

## 5. Latency, and the wake-up

Push delivers in milliseconds. A bare poll delivers in `POLL_SECONDS`.
`LISTEN/NOTIFY` closes the gap: enqueue fires one NOTIFY after commit,
every worker holds one LISTEN connection, and a payload-free notification
means "ask the database now". Losing a notification costs one poll
interval, nothing more — the row waits in the database either way.

The wake-up pays off only on a direct Postgres connection. There it is
worth keeping: the Heroku rig measured pickup at 0.9 s against the 5 s
poll floor. pgbouncer transaction pooling rejects LISTEN, so a worker
behind it falls back to the poll floor and every row still runs within
`POLL_SECONDS`.

## 6. Two open choices

**The lease.** 0.17.0 replaced the watchdog: the worker kills an
attempt process that runs past its declared `timeout=`, and a dead
worker's row lock dies with its connection. A later
step could fold that into the claim — a `claimed_until` column the worker
extends while it runs — making "is this attempt alive?" one column read
instead of a stamp plus a probe. Deferred: the worker-side kill works,
and a cut should change one thing at a time.

**Crash containment (decided during validation).** The first Heroku run
showed why the worker cannot run attempts in its own process: an injected
crash killed the whole worker, and the platform's repeated-crash backoff
parked the queue group for ten minutes — the process pool the old broker
worker provided had absorbed exactly this. The loop therefore runs every
attempt in its own forked attempt process. A crash kills that process;
the worker records it on the row, so crashing attempts get the same
paced, bounded retries as
failing ones — which the push design never had (a crash left no error and
relied on the platform's restart policy).

**Build or adopt.** Procrastinate is a maintained PostgreSQL job library
with exactly this shape (SKIP LOCKED, LISTEN/NOTIFY, locks, periodic
tasks). Adopting it would outsource the worker loop and its process
management; the cost is a new core dependency, mapping our per-instance
gate onto its locks, and a consumer migration story we do not control.
The shipped loop is ~200 lines because the hard parts (the attempt, the
guards, the accounting) already exist in the runner — which is what
decided build over adopt; the comparison stays here for the record.

## 7. What the worker process looks like

```
python manage.py dl_worker --queues django_logic.critical,django_logic.fast
```

One process per SLA group, exactly like one broker worker per queue
before. Celery is no longer a dependency: nothing imports it, and an
unknown `BACKGROUND_EXECUTION` value fails loudly at boot naming the
valid modes.

`--concurrency=N` says how many attempts one worker runs at a time
(default 1). Each attempt still runs in its own forked process, and
`SKIP LOCKED` already makes concurrent claims safe.

## 7a. Sizing a deployment

Two numbers decide the shape: memory and database connections.

**Memory.** Every worker process carries the full Django image, and
every running attempt adds its own peak on top. Demand is spiky — most
attempts need little and a few need a lot — so N one-slot workers each
have to be sized for their heaviest attempt, while one worker with N
slots absorbs the same peak out of one shared pool. Prefer fewer, larger
workers with `--concurrency`. Keep separate workers where the queues
need SLA isolation, not to buy parallelism.

The first production day on 0.16.0 showed the cost of the other shape:
three one-slot workers on 512 MB, two of them killed by the platform's
memory quota, an import attempt peaking at 804 MB, and an hourly fan-out
of 119 rows draining at 2.6 rows a minute across two swapping workers.

**Connections.** Each running attempt holds one database connection —
two where the app opens a second one. Budget
`workers × (concurrency + 1)` connections per queue group (the extra one
is the worker's own LISTEN connection) and keep the total under the
database plan's cap, or under the pgbouncer pool size. A worker that
cannot connect logs and retries; a database at its cap refuses the web
processes too, so leave headroom for them.

## 7b. Knowing a worker stopped

The safety nets run inside the worker loop. A dead `dl_worker` therefore
means no stuck finalizer and no cleanup sweep as well as no attempts —
nothing else reports the backlog. Alert on the process, not on the rows:

- a process supervisor that restarts the worker and reports the restart
  (on Heroku, dyno-crash alerts);
- a heartbeat check — a cron or Sentry cron monitor that fails if no
  worker has logged `pull worker starting` or completed a row inside a
  known window;
- `python manage.py dl_transitions` during an incident: it lists the
  uncompleted rows and names the ones nothing is serving.

`detect_stuck_transitions` already logs at ERROR when a row has waited
past the retry window with no attempt started, naming the queue and the
`dl_worker` line that would serve it. That log line is the one to page
on. It only fires while some worker is alive to run it, which is why the
process alert comes first.

## 8. Migration path

1. 0.15.0 is the last push release; 0.16.0 ships pull as the default and
   removes the push machinery. An unknown mode fails loudly at boot
   naming the valid ones.
2. The Heroku harness runs the full matrix under pull next to the
   recorded push results before 0.16.0 publishes.
3. A consumer moves by setting `'pull'`, replacing its Celery worker and
   beat lines with `dl_worker` processes, and draining the old broker
   queues once.

## 9. Validation plan

- The SQLite suite must stay green untouched (pull is Postgres-only and
  additive).
- New PostgreSQL tests: a fresh row is claimed and completed; a failing
  row is invisible until `RETRY_MINUTES` and claimable after; a row whose
  attempt is running is skipped; a crashed claim is re-claimable at once;
  the queue filter holds; NOTIFY wakes a waiting worker.
- The Heroku matrix (`django-logic-test`): every row that exercises
  dispatch, recovery, retries, the timeout kill, and concurrency, driven under
  pull, alongside the recorded push results. The rows the mirror made
  necessary (lost dispatch, dispatch bounds) must become trivially green
  or provably meaningless.
