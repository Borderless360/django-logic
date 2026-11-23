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
It refuses reentry without creating a persistent ownership record. A killed process
therefore leaves no ownership record that delays a saved completion job's retry.

This database-only promise includes **all nested transitions and submissions**,
including their ordinary callbacks. They must use the same database.
The scope ends before `transaction.on_commit()` callbacks run. Save required
external work as a background job; do not call providers, publish messages,
or perform other irreversible work in this scope. In a synchronous test runtime,
background work runs inline and must also satisfy that promise.

Ordinary `Transition` calls outside this scope keep their committed ownership protection.
`DatabaseTransition` also refuses an existing ownership record, including one held by
an external call whose database connection failed. Do not clear that lock to
force a retry. Replacing an external operation with `DatabaseTransition` is unsafe.

For example, an import completion saves its results and updates stock in one transaction.
Its notifications and shipping requests run as separately saved background jobs.

## State protection

On PostgreSQL, lock-taking transitions acquire the physical business-row lock.
The engine uses `FOR NO KEY UPDATE`. Other transactions cannot update or delete the protected row until the transaction ends.
Related records can complete foreign-key checks without waiting for that row's state transition.
This avoids an unnecessary conflict when one transaction holds a related record and another needs to change it.
See PostgreSQL's [row-lock rules](https://www.postgresql.org/docs/current/explicit-locking.html#LOCKING-ROWS).

Protecting one row does not freeze the set of related records.
Applications must coordinate collection changes when business rules require a fixed set.
Explicit application locks still apply; a stronger `FOR UPDATE` lock on the same row can retain the reference-check conflict.
Application writes that change referenced keys still acquire PostgreSQL's required stronger lock.

Transitions wait at most two seconds for another transaction that holds the row.
A holder that outlasts the wait causes `TransitionTemporarilyUnavailable` before application work starts, and the engine logs a warning that names the record.
A background job that meets this refusal records an error and retries after the normal pause. It does not wait quietly.
The database retains the lock through the actual outer commit or rollback.
Inside this transaction, synchronous side effects run in a savepoint. A database error in a side effect discards their writes and still records the failed state and runs the failure callbacks. Any other error keeps the writes made before it.
The committed ownership record also prevents overlapping external calls while its timeout
remains valid, including after a lost database connection. PostgreSQL protects
database state; neither lock can undo an external request already sent.
Saving a background job calls no provider. On PostgreSQL that save holds the business row and writes no ownership record.
It still refuses a record that has an ownership record. A process that stops during the save leaves no lock on the record.
Each synchronous callback runs in a savepoint inside the transaction described above. A database error discards its writes; any other error keeps the writes made before it, including the failed state of a transition it started.
The ownership record is released first, so callbacks can call another transition on the same instance.

SQLite is supported only for single-process business tests with the test runtime
explicitly enabled. Its ownership records use the ordinary connection and can
participate in the caller's transaction. It does not provide the PostgreSQL
independent-commit or concurrent-state guarantees. Other backends are refused.

State ownership lives on the default database, even when a synchronous action
writes a business object on another alias. Each time the library takes or releases
ownership, it opens a short independent PostgreSQL connection and closes it at once.
An ownership check does the same inside a transaction on the default database.
Outside one, a check reads through the default connection, because each statement
there sees every committed record.
The tables have no foreign keys to business objects or job records, so taking a
lock does not wait for the caller's uncommitted business row.

A pull worker creates a unique process owner before starting each job process.
After the operating system confirms that process has stopped, the worker removes
only that owner's locks and records the outcome in the same bounded transaction.
Other workers wait for the job's unexpired state locks before retrying. The process
record alone does not delay database-only recovery. A delayed
acquisition cannot recreate a removed owner, and a delayed release cannot delete
another owner's token.

The full configured `LOCK_TIMEOUT` still limits exclusion, including when an
entire worker disappears before it can confirm a job process's exit. The duration
starts when PostgreSQL starts the acquisition statement, including time spent
waiting for its result. It does not restart when the caller receives the token. Expiry is
not proof that an old process or provider request stopped. Application receipts
and duplicate protection remain required. Expired lock rows are removed in bounded
maintenance batches; unknown process-owner records are retained, not treated as
confirmed dead merely because they are old.

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
completion. Normal retries keep their error count and pause. The same
completion check applies to `atomic=False` jobs and stuck-job completion; a
non-atomic job keeps any provider confirmations already committed.

Applications own cancellation state and any confirmed external effects.
A state change supersedes an unfinished state-changing job before its next attempt.
It does not interrupt a provider request already running or undo a confirmed external change.
