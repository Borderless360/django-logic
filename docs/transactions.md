# Locks and transactions

On PostgreSQL, a lock-taking synchronous `Transition` runs callbacks inside its transaction, before the target state commits.

Other connections see the prior committed state until the outermost transaction commits, including any transaction opened by the caller.
A process interruption during callbacks can therefore discard the state and side-effect writes too.
Use `transaction.on_commit()` for best-effort work that needs committed data; it does not guarantee delivery of required work.
Pull workers run background success callbacks after the job commits; a crash can lose callbacks after completion.
Inline background execution can still be inside a caller's outer transaction.

Declare `Transition(..., atomic=True)` when its required database changes and
final target save must succeed together. A side-effect error or a target save
error discards both, including writes made by model save overrides and signals.
The transaction uses the bound instance's database. This option also works with
`DatabaseTransition`; both synchronous classes default to `atomic=False`.

After rollback, the engine reloads the bound instance, writes `failed_state`
when declared, releases its lock, runs failure callbacks, and re-raises the
original error. Failure callbacks therefore see the restored persisted fields.
If reloading fails, the engine releases its lock and reports the original error
without running failure writes or callbacks on an unverified object. A failed
failure-state save also preserves the original error. An atomic transition may
declare `failed_state` without side effects because its target save can fail.
An error saving `failed_state` triggers another reload before failure callbacks.

Synchronous transitions default to `atomic=False`: an ordinary application error can leave earlier writes committed.
Background jobs default to `atomic=True`: a failed attempt discards its database changes before retrying.
Choose `atomic=True` for a synchronous action whose database changes must succeed together.
This option cannot reverse external requests or guarantee optional callbacks. Success callbacks run after the required transaction block and retain their
best-effort behavior. Use `transaction.on_commit(..., robust=True)` for optional
publication that must wait for the containing transaction to commit.
An exception from a non-robust commit callback still reaches the caller after
commit; it does not undo the completed action or run its failure callbacks.

The atomic option separates the target save from optional completion. A subclass
that overrides `complete_transition` is refused with `atomic=True`, because the
engine cannot place arbitrary completion code on the correct side of that
transaction. A subclass that overrides completion must use `atomic=False`.
Use the declared `side_effects` and `callbacks` for the atomic option.

Nested required actions share the containing database transaction. If an outer
action fails, successful nested state changes and their commit callbacks are
discarded too. A nested failed-state write can also be discarded by its outer
action's rollback. Django does not restore other Python instances after that
rollback; reload related objects before retrying. Writes to another database
and external effects are outside this guarantee.

Use `DatabaseTransition` for changes that PostgreSQL can roll back completely.
It takes the same declaration arguments as `Transition` and requires PostgreSQL.
It keeps row locks, permissions, conditions, source checks, pending-job checks,
targets, failure handling and callbacks.
It refuses reentry without creating a persistent cache lock. A killed process
therefore leaves no cache lock that delays a saved completion job's retry.

This database-only promise includes **all nested transitions and submissions**,
including their ordinary callbacks. They must use the same database.
The scope ends before `transaction.on_commit()` callbacks run. Save required
external work as a background job; do not call providers, publish messages,
or perform other irreversible work in this scope. In a synchronous test runtime,
background work runs inline and must also satisfy that promise.

Ordinary `Transition` calls outside this scope keep their shared cache protection.
`DatabaseTransition` also refuses an existing cache lock, including one held by
an external call whose database connection failed. Do not clear that lock to
force a retry. Replacing an external operation with `DatabaseTransition` is unsafe.

For example, an import completion saves its results and updates stock in one transaction.
Its notifications and shipping requests run as separately saved background jobs.

## State protection

On PostgreSQL, lock-taking transitions acquire the physical business-row lock.
They wait at most two seconds for another transaction that holds the row.
A holder that outlasts the wait causes `TransitionTemporarilyUnavailable` before application work starts, and the engine logs a warning that names the record.
A background job that meets this refusal records an error and retries after the normal pause. It does not wait quietly.
The database retains the lock through the actual outer commit or rollback.
Inside this transaction, synchronous side effects run in a savepoint. A database error in a side effect discards their writes and still records the failed state and runs the failure callbacks. Any other error keeps the writes made before it.
The shared cache lock also prevents overlapping external calls while its timeout
remains valid, including after a lost database connection. PostgreSQL protects
database state; neither lock can undo an external request already sent.
Saving a background job calls no provider. On PostgreSQL that save holds the business row and writes no cache lock.
It still refuses a record that has a cache lock. A process that stops during the save leaves no lock on the record.
For a lock-taking synchronous `Transition`, callbacks also run inside the PostgreSQL transaction, before the outermost commit.
Each one runs in a savepoint. A database error discards its writes; any other error keeps the writes made before it, including the failed state of a transition it started.
The cache lock is released first, so callbacks can call another transition on the same instance.
Other connections still see the prior committed state. An interruption can discard the target and side-effect writes before commit.
Pull workers run background success callbacks after the job commits; a crash can lose callbacks after completion.
Inline background execution can still be inside a caller's outer transaction.
Use `transaction.on_commit()` for best-effort publication that requires committed data, not as a guarantee of required work.

On other databases, `State.protected()` takes no row lock and opens no transaction.
Without a caller transaction, synchronous writes commit independently and swallowed database errors may not be detected.
SQLite is supported for single-process business tests, not concurrent production state transitions.
Even with `atomic=True`, its cache lock can be released while an outer transaction remains open.
A second connection can then read the earlier state and run an external action before the first transaction commits.
Atomic rollback does not provide concurrent-state protection on this backend. Use PostgreSQL for that guarantee.

Default atomic state-writing background jobs retain the business-row lock during their transaction.
Targetless jobs without a failed state protect only finalization, so independent provider work can overlap.
`atomic=False` jobs retain a separate job-row lock and protect business state only when finalizing.
Allow for both database connections. Provider requests must be bounded.
A connection loss cannot undo an external request that was already sent.

Completing a failed job makes a bounded check of the business row before writing
`failed_state`. If the state changed, the job completes as `superseded` and skips
failure callbacks. If another transaction still holds the row, or its state cannot
be checked, the job completes
as failed, records why the failed state could not be written, and leaves the
business state unchanged without running failure callbacks. A final attempt can
therefore make two bounded lock attempts: one before its work and one before
completion. Normal retries keep their existing error count and pause. The same
completion check applies to `atomic=False` jobs and stuck-job completion; a
non-atomic job keeps any provider confirmations already committed.

Applications own cancellation state and any confirmed external effects.
A state change supersedes an unfinished state-changing job before its next attempt.
It does not interrupt a provider request already running or undo a confirmed external change.
