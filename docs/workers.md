# Running workers

The database stores each submitted job and its retry history.
Workers select available jobs directly from that database.

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

A database lock wait stays quiet for five seconds. The worker then logs
its message ID and elapsed wait, repeating at most once a minute per record.
Reading a job deadline and recording a job failure each allow at most 100 milliseconds
for a PostgreSQL lock and one second for a statement, while preserving stricter
database settings. A queued table lock therefore cannot block these worker checks
indefinitely. The worker keeps an unrecorded failure and retries the write later.
Inspect the PostgreSQL lock holder if the wait continues. This warning does
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

**Connections.** Each running attempt normally holds one database connection.
An `atomic=False` attempt uses two: one locks its job, while the other saves
model writes independently. Applications may open additional connections. Budget
`workers × (concurrency + 2)` connections per queue group for atomic jobs.
For `atomic=False` jobs, budget `workers × (2 × concurrency + 2)`. The two extras
are the worker's LISTEN connection and its separate maintenance process.
Add any connections that consumer callbacks open. Keep the total under the
database plan's cap, or under the pgbouncer pool size. A worker that
cannot connect logs and retries; a database at its cap refuses the web
processes too, so leave headroom for them.

## Stopping a worker

A platform sends SIGTERM before it replaces a worker, for example when it
deploys. `dl_worker` then takes no new job, waits for its running jobs, and
exits with code 0. A job process ignores SIGTERM, because some platforms send
the signal to every process. A program that the job starts inherits this, so
`terminate()` does not stop it unless the program handles SIGTERM itself. Stop
such a program with `kill()`.

A job that the worker claimed at the moment of the signal does not start. It
stays ready for another worker. A maintenance pass that still runs gets five
seconds. Then the worker stops it, and the next pass of any worker repeats
its work.

The platform ends the wait at its own limit: 30 seconds on Heroku, and
`terminationGracePeriodSeconds` on Kubernetes. A job that still runs at that
limit stops there, and the next worker runs it again from its first step. Set
the limit above the longest provider request of the worker's jobs.

## Knowing a worker stopped

The worker starts one maintenance process once a minute. It stops that
process after 60 seconds. This keeps failure callbacks outside the
supervisor that enforces attempt timeouts. A stopped pass leaves
uncompleted rows for the next pass. Callbacks for completed rows remain
best-effort and are not retried.

Completed-message cleanup commits groups of at most 1,000 rows. Earlier
groups stay deleted if the maintenance process stops during a later group.
Each delete keeps the age filter and newest terminal-failure retention rule.
An enclosing caller transaction still controls when its batches commit.
The row limit does not bound database time: a blocked group can still
reach the process deadline and must be retried on the next pass.

When `dl_worker` stops, jobs, failed-job completion and history cleanup all stop.
Monitor the worker process directly:

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
The job list uses the latest recorded error or postponement to distinguish waiting work from an unfinished attempt.
An expired wait does not mean the previous attempt is still running.
Text statistics include the full declaring process class, so same-named provider actions remain distinguishable.
Cache writes run inside the bounded maintenance process. A blocked cache cannot
delay the worker's job timeout checks. Cache failure leaves database progress
checks unchanged. The default cache must be shared between reporting processes
and workers; an in-memory cache cannot provide cross-process observations.

## Provider receipts that must survive a failed attempt

`BackgroundTransition(atomic=False)` keeps the same job-row lock on a separate
connection. Model side effects use their normal connection in autocommit.
Final state and job completion commit together on the connection holding the lock.
A side effect's failure or postponement remains its result when another transaction holds the business row.
A postponed job keeps its due time without spending an allowed error.
A permanent failure completes the job. If its business row remains locked, the worker records that problem separately.
It leaves the business state unchanged and skips failure callbacks in that case.
The default atomic execution remains unchanged.

This option works with transaction pooling: it uses no session advisory lock.
The locking transaction remains open during provider work, so the database
idle-transaction timeout must exceed the declared job timeout. Losing that
connection releases its lock. An external request already running may continue;
durable pending receipts or provider idempotency keys must prevent duplicate requests.
The engine checks the connection before starting each subsequent side effect.
See [provider receipts](recipes/provider-receipts.md) for the consumer contract.

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

### See what is not moving

```bash
python manage.py dl_transitions
python manage.py dl_transitions --send 1234
python manage.py dl_transitions --stats --since 7d --queues django_logic --json
```

`dl_transitions` lists every uncompleted background transition and says why
each one is not moving: it has spent every attempt, it waits out the retry
pause, its last attempt has no recorded result, or it waits for worker capacity.
The report also distinguishes recent worker presence from actual queue progress. `--queues` narrows the list.
Workers take turns between eligible clients. Within a client, jobs run by when they became ready, then submission time and ID.
A postponed older job can therefore follow newer work that was ready first.

Workers record their served queues during each maintenance check, even when all
job slots are full. This recent observation is separate from queue progress.
A queue with no progress still reports an error when its worker is present.
The message directs operators to check capacity and blocked jobs.

`--stats` reports retained jobs created during the selected period, grouped by
queue, model, process and transition. It includes outcomes, errors,
recorded starts, repeated starts and deferrals. Initial waiting uses the first
recorded start. Duration covers the last measured attempt, excluding completion
callbacks. Each timing includes its sample count, median, 90th percentile and
maximum in milliseconds. Missing history stays unknown. Repeated starts include
deferrals and competing starts; they are not an exact retry count. The report
cannot infer total runtime or worker utilization. It changes no job records.
Use `--json` for machine-readable output. `--queue` also accepts the queue filter.

The same report is available as
`django_logic.background.transition_statistics(since=cutoff, queues=None, using=None)`.
`cutoff` is a datetime; the optional database alias selects where to read jobs.
`queue_worker_presence(queue_name)` returns `True` for recent presence, `False`
for no retained observation, or `None` when the cache cannot answer.

`--send <pk>` clears the retry wait on one row and wakes the workers, so the
next claim takes it. The command runs no side-effects itself.

### Periodic maintenance

The worker starts maintenance checks once a minute in one separate process.
Only one maintenance process runs per worker. Its time limit is 60 seconds.
Nothing else needs a schedule.

- `detect_stuck_transitions` completes a job that has spent every allowed error.
  It writes `failed_state` and runs failure callbacks only when the saved state still applies and can be checked under its lock.
  Otherwise it records why the state change was skipped. It also reports rows that waited past the retry window with no attempt, in one
  line per queue. If a worker started a row from that queue inside the window,
  or holds one now, the queue is behind, and the line is a WARNING. If not, no
  worker has taken a row from the queue for the whole window, and the line is
  an ERROR. Either no worker serves the queue, or work from other queues fills
  every slot of the workers that do. Recent worker presence changes the
  explanation, but does not reduce the severity of missing progress.
- `cleanup_completed_transitions` deletes completed rows older than
  `TRANSITION_MESSAGE_CLEANUP_DAYS`. It keeps the newest failed row per
  instance and process, because that row is the only explanation for an
  instance parked in its `failed_state`.

Alert when the worker processes stop. Periodic maintenance stops with them.
Reserve one extra database connection for the maintenance process, plus any
connections your callbacks open. A slow or exiting callback cannot block
the supervisor's attempt timeouts. If a pass stops, the next pass checks
the remaining uncompleted rows. Callbacks for completed rows remain
best-effort and are not retried.
