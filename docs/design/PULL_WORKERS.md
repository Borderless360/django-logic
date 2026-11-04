# Pull workers

The database stores each submitted job and its retry history.
Workers select available jobs directly from that database.
There is no second copy in a broker to lose or reconcile.
Release tags and the changelog retain the history of the replaced design.

## Selecting and retrying jobs

A worker selects an eligible job from its queues using `SELECT FOR UPDATE SKIP LOCKED`.
A running job holds its database row lock, so other workers skip that job.
Client turn records let each client make progress without limiting its parallel jobs.
Within a client, workers select jobs by when they became ready, then by submission order.
A postponed job follows work ready before its new due time, even when the postponed job was submitted first.
This lets required newer work run before an older job checks its result again.
Jobs without a postponement keep their submission order, including jobs without a client key.
A failed attempt waits until its retry time. The configured error limit stops repeated failures.
Error retries keep their submission order once the retry wait ends.
A lost database connection releases the lock, allowing another worker to try again.
External side effects still require application duplicate protection.

Each job runs in a separate process. The worker records crashes and stops
jobs that exceed their declared timeout. The job row remains the source for recovery.

Submission sends a `LISTEN/NOTIFY` notification after committing the job.
A lost notification costs at most the normal polling interval; the saved job remains available.
Transaction pooling uses that polling interval because it cannot keep a session subscribed to notifications.

## Worker processes

```
python manage.py dl_worker --queues django_logic.critical,django_logic.fast
```

One worker serves one group of queues. Set `--concurrency` to run several
jobs in parallel. Several workers can serve the same queues.
An unknown `BACKGROUND_EXECUTION` value fails at startup.

When every job slot is busy, the worker checks completed job processes every
10 milliseconds. It also checks job deadlines and starts periodic maintenance.
When a job process finishes, the worker can start another job. When capacity
is available, a PostgreSQL notification wakes the worker for newly submitted work.

Once the worker detects that a job process has finished, it can start
another job. If a database lock prevents it from recording the failure,
it saves that information separately and tries again later. It keeps the
message ID separately from the process ID, which the operating system may
reuse. It does not claim that message again until the failure is recorded.

The worker retries each failure record at most once a second. Each check
retries at most 16 records and checks running jobs' deadlines between writes.
At 1,000 unsaved records, it stops starting jobs until that count falls.
Already running jobs can still finish, adding at most the configured
concurrency to that bound. Other workers continue to claim work normally.

A database row-lock wait stays quiet for five seconds. The worker then logs
its message ID and elapsed wait, repeating at most once a minute per record.
Inspect the PostgreSQL row holder if the wait continues. This warning does
not mean that the job currently running against that row has failed.

`--concurrency=N` says how many attempts one worker runs at a time
(default 1). Each attempt still runs in its own forked process, and
`SKIP LOCKED` already makes concurrent claims safe.

## Sizing a deployment

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

**Connections.** Each running attempt normally holds one database connection.
An `atomic=False` attempt uses two: one locks its job, while the other saves
model writes independently. Applications may open additional connections. Budget
`workers × (concurrency + 2)` connections per queue group for atomic jobs.
For `atomic=False` jobs, budget `workers × (2 × concurrency + 2)`. The two extras
are the worker's LISTEN connection and its separate safety-net process.
Add any connections that consumer callbacks open. Keep the total under the
database plan's cap, or under the pgbouncer pool size. A worker that
cannot connect logs and retries; a database at its cap refuses the web
processes too, so leave headroom for them.

## Knowing a worker stopped

The worker starts one safety-net process once a minute. It stops that
process after 60 seconds. This keeps failure callbacks outside the
supervisor that enforces attempt timeouts. A stopped pass leaves
uncompleted rows for the next pass. Callbacks for completed rows remain
best-effort and are not retried.

Completed-message cleanup commits groups of at most 1,000 rows. Earlier
groups stay deleted if the safety-net process stops during a later group.
Each delete keeps the age filter and newest terminal-failure retention rule.
An enclosing caller transaction still controls when its batches commit.
The row limit does not bound database time: a blocked group can still
reach the process deadline and must be retried on the next pass.

A dead `dl_worker` therefore
means no stuck finalizer and no cleanup sweep as well as no attempts —
nothing else reports the backlog. Alert on the process, not on the rows:

- a process supervisor that restarts the worker and reports the restart
  (on Heroku, dyno-crash alerts);
- a heartbeat check — a cron or Sentry cron monitor that fails if no
  recent worker presence remains for a queue;
- `python manage.py dl_transitions` during an incident: it lists the
  uncompleted rows and names the ones nothing is serving.

`detect_stuck_transitions` already reports rows that have waited past
the retry window with no attempt started, in one line per queue. When no
worker started a row from that queue inside the window and none holds one
now, the line is an ERROR. It names the oldest row, the count behind it
and the `dl_worker` line that would serve the queue. That log line is the
one to page on. When a worker still takes rows from the queue, the queue
is behind, and the line is a WARNING: a backlog drains in turn, so it is
no reason to page. A worker can serve several queues. If work from its other queues fills every
slot for the whole window, a queue it serves still gets the ERROR, because
nothing has run from that queue for that long. Give that queue its own
worker or more slots. The ERROR only fires while some worker is alive to
run it, which is why the process alert comes first.

Each maintenance process records every queue its worker serves in the shared
cache. A full worker still starts this process. The observation expires after
the retry window. Stopping one worker never deletes another worker's observation.
This is recent maintenance evidence, not proof that a worker is still running
or that a queue is making progress. A stopped worker can remain visible until
the record expires. Cache failures can also leave presence unknown or absent.

When an observation exists but no job has progressed, the ERROR says to check
capacity, other served queues and blocked jobs. Without it, the message suggests
starting a worker. Recent presence never downgrades a stalled queue to a WARNING.
`queue_worker_presence(queue_name)` exposes this evidence without changing jobs.
Cache writes run inside the bounded maintenance process. A blocked cache cannot
delay the worker's job timeout checks. Cache failure leaves database progress
checks unchanged. The default cache must be shared between reporting processes
and workers; an in-memory cache cannot provide cross-process observations.

## Provider receipts that must survive a failed attempt

`BackgroundTransition(atomic=False)` keeps the same job-row lock on a separate
connection. Model side effects use their normal connection in autocommit.
Final state and job completion commit together on the connection holding the lock.
The default atomic execution remains unchanged.

This option works with transaction pooling: it uses no session advisory lock.
The locking transaction remains open during provider work, so the database
idle-transaction timeout must exceed the declared job timeout. Losing that
connection releases its lock. An external request already running may continue;
durable pending receipts or provider idempotency keys must prevent duplicate requests.
The engine checks the connection before starting each subsequent side effect.
See [provider receipts](../recipes/provider-receipts.md) for the consumer contract.

## PostgreSQL connection pooling

For PgBouncer transaction pooling, set `DISABLE_SERVER_SIDE_CURSORS=True` on
the Django database configuration. Django disables prepared statements by default
with psycopg 3. Do not add psycopg 3 connection options to a psycopg2 connection.
Configure SSL for the actual application-to-pooler connection; do not copy
an environment-specific SSL setting into every deployment.

A job keeps a transaction open while it holds its row lock. Set the database
idle-transaction timeout above the declared job timeout. Budget the extra connection
for `atomic=False` as described above. A session lost during an external request
cannot stop that request; the application must prevent duplicate external changes.
