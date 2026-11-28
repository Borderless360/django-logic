# Running workers

The database stores each submitted job and its retry history.
Workers select available jobs directly from that database.

The worker restores the user saved when the job was submitted.
If that account no longer exists, the worker records a failed job and stops it without retrying.
It leaves the business state unchanged and runs no business functions or callbacks.
Review the failed job before submitting replacement work under an authorized user.
Jobs submitted without a user still run as system work.

`dl_worker` requires Linux. On macOS or Windows, run the worker in a Linux VM
or container. The command refuses other operating systems before running its
checks or starting worker activity. Native macOS jobs can abort when looking
up a provider hostname after the worker starts a job process with `os.fork()`.
Synchronous transitions and single-process SQLite developer tests do not
require Linux.

## Selecting and retrying jobs

A worker selects an eligible job from its queues using `SELECT FOR UPDATE SKIP LOCKED`.
A running job holds its database row lock, so other workers skip that job.
Fairness-group records let each group make progress without limiting its parallel jobs.
Within a fairness group, workers select jobs by when they became ready, then by submission order.
A deferred job follows work ready before its new due time, even when the deferred job was submitted first.
This lets required newer work run before an older job checks its result again.
Jobs without a deferral keep their submission order, including jobs without a fairness key.
A failed attempt waits until its retry time. The configured error limit stops repeated failures.
Error retries keep their submission order once the retry wait ends.
A lost business connection releases its row lock. If a worker exits between selecting a job and acquiring its execution lock, another worker can recover the job after five seconds. External side effects require application duplicate protection.

When a job calls a synchronous transition on its own state field, the selected
state and its job record commit together. A savepoint rollback restores the
previous choice. If a non-atomic job commits a choice and then retries, its
side effects can run again and complete the remaining work. Give external
requests a durable receipt or idempotency key. A later change to a different
state still supersedes the job.

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

Each job runs in its own process. When that process finishes, the worker can start another job.
The worker continues checking timeouts while it records failures and runs maintenance.
If a database lock prevents failure recording, the worker keeps the failure and retries the write.
When the wait persists, a warning names the job and elapsed wait.
Inspect the PostgreSQL lock holder so the worker can record the failure before another attempt starts.

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

## Monitoring worker availability

When `dl_worker` stops, jobs, failed-job completion and history cleanup all stop.
Monitor the worker process directly:

- a process supervisor that restarts the worker and reports the restart
  (on Heroku, dyno-crash alerts);
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

## Provider receipts that must survive a failed attempt

`BackgroundTransition(atomic=False)` keeps the same job-row lock on a separate
connection. Model side effects use their normal connection in autocommit.
Final state and job completion commit together on the connection holding the lock.
A side effect's failure or deferral remains its result when another transaction holds the business row.
A deferred job keeps its due time without spending an allowed error.
A permanent failure completes the job. If its business row remains locked, the worker records that problem separately.
It leaves the business state unchanged and skips failure callbacks in that case.

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

## Inspecting jobs

```bash
python manage.py dl_transitions
python manage.py dl_transitions --send 1234
```

`dl_transitions` lists every uncompleted background transition and says why
each one is not moving: it has spent every attempt, it waits out the retry
pause, its last attempt has no recorded result, or it waits for worker capacity.
Use `--queues` to filter the list.

`--send <pk>` clears the retry wait on one job and wakes its workers.
The job can then run when a worker has capacity. The command runs no business functions itself.
It cannot override a saved deferral or be combined with `--queues`.

## Periodic maintenance

The worker starts one maintenance process once a minute, with a 60-second limit.
Nothing else needs a schedule. Reserve its database connection as described under sizing.
The worker continues enforcing job timeouts while maintenance runs application callbacks.
If a pass stops, the next pass checks the remaining unfinished jobs.
Optional callbacks for completed jobs are not retried.

- `detect_stuck_transitions` completes jobs that have used their error allowance.
  It writes `failed_state` and runs failure callbacks only when the saved state still applies and can be checked under its lock.
  For `record_failure`, it saves the required business outcome with failed-job completion.
  A failed recording remains pending for maintenance; ordinary workers do not repeat its provider work.
  Otherwise, it records why the business state was left unchanged.
  It also reports queues without progress, as described under monitoring.
- `cleanup_completed_transitions` deletes completed jobs older than `TRANSITION_MESSAGE_CLEANUP_DAYS`.
  It keeps the newest failed job per record and process so the failure remains available for investigation.
  A pass reads the jobs that are old enough to delete, and every failed job.
  It does not read the rest of the job table.

Cleanup commits bounded groups of rows. Earlier groups stay deleted if a later group is interrupted.
An enclosing caller transaction still controls when those deletions commit.
A blocked deletion can reach the maintenance time limit and be retried on the next pass.
