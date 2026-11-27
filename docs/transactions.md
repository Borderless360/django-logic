# Locks and transactions

A synchronous `Transition` runs its required side effects and target save in one database transaction on the model's database. PostgreSQL locks the row declaring the state field through commit. A second caller waits up to two seconds for that row. If it remains busy the second caller receives `TransitionTemporarilyUnavailable`.

After acquiring the row, the engine checks the source state and the conditions and permissions of the process, nested processes, and action on the freshly loaded record. If another declaration with the same name now matches, the call is refused. Hooks receive the caller's original object, preserving unsaved input. Listing available actions evaluates the supplied object and does not reserve the action.

An exception in required work rolls back its database changes. The engine then writes any declared `failed_state` and raises the original error. An enclosing application transaction still controls whether that failure state commits. Success and failure callbacks are optional and run after the outer transaction commits. Rolling back an enclosing transaction discards its callbacks. Another connection can read the committed result from a callback, and a callback can call the next action of the same process.

A required side effect may call a synchronous action on another record or state field. Reentering the same field from a synchronous side effect raises a refusal. A `Transition(lock=False)` can opt out of row locking for actions with no target or failure state. Its required database work still commits together.

## Background actions

Submission locks the business row, checks the action and writes the job and its in-progress state in one transaction. For preparation that must commit with the job, enclose the application changes and the call in a Django `transaction.atomic()` block on the same database.

A worker records a claim before starting its process. During execution it locks the job row. Required work in an ordinary attempt commits together with the target and completed job. A killed process rolls back its transaction and PostgreSQL releases its locks.

A running job's side effect can call a synchronous transition on its own process. That call can select a result according to the job's inputs. The worker preserves the state chosen by the nested action when it completes its job. Other callers still encounter the unfinished-job guard.

With `BackgroundTransition(atomic=False)` the worker locks the job on a separate connection. Each model write in the side effects commits independently. Finalization locks the business row and commits the result with job completion. If another workflow changes the state, the worker checks its expected state before writing a result. Use durable receipts or provider idempotency keys when a retry could repeat a request. PostgreSQL rolls back database changes, while an external request can outlive a lost connection or killed process.

See [provider receipts](recipes/provider-receipts.md) and [worker operations](workers.md) for transaction pooling and connection requirements.
