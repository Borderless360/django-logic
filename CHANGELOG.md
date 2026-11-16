# Changelog

## [5.1.0] — 2026-11-16

### Added

- Preserve application audit context with the project-level `JOB_CONTEXT` adapter.
  Capture trusted context when accepting a job and restore it for attempts, retries and completion hooks.
  The context has its own nullable field; business declarations and arguments do not carry it.
- Add `ProcessScenario.assert_deferred()` to check a saved wait. A premature `retry_transition()` now fails instead of reporting an unexecuted attempt as successful.

### Changed

- Declare synchronous transition options explicitly and keep background-only validation in `BackgroundTransition`.
  Unknown options raise `TypeError`. Every process binding requires the installed `django_logic` app.
- Keep business arguments named `action_name` instead of silently dropping them.
  Both synchronous and background hooks receive the actual declaring process class.
- Organize current documentation into declarations, jobs, transactions, refusals, workers and upgrades.
  Preserve release notes before version 4 in a separate historical archive.
- Require PostgreSQL and Redis tests on every supported Django version before publishing.
  Validate a dated changelog entry before uploading package files.

### Fixed

- A state object without its own lock token cannot release another object's cache lock.
- Reject reserved action arguments before waiting for a lock, and refuse unknown or malformed saved type tags.
- Preserve trace identifiers through background success and failure callbacks.
- Complete unrestorable jobs through one failure path that clears current waiting metadata.
- Complete jobs as failed after repeated application-context setup errors, without running work under incorrect attribution.
  Context cleanup cannot suppress required-work errors or retry already committed success.
- Log unexpected job-process exceptions before exit so the recorded crash can be investigated.
- Export `AlreadyInProgress` from `django_logic.background` and reject conflicting `dl_transitions --send` and `--queues` options.

### Upgrade

- Apply migration 0019. It adds nullable application context and removes six database defaults used by older producers; model defaults remain.
- Use Django 5.2 or later. Tested versions are 5.2, 6.0 and 6.1.
  PostgreSQL workers require psycopg 3.2 or later, or Django's supported psycopg2 driver.
  The `postgres` extra installs psycopg 3; install `redis` when using Django's Redis cache.
- Adapters must clear missing context with `restore(None)` and restore ambient context when their scope exits.
- Concurrent production state guarantees require PostgreSQL. SQLite remains suitable for single-process business tests.
- See [upgrading and rollback](docs/upgrading.md) and the [API reference](docs/reference.md).

## [5.0.0] — 2026-11-13

### Removed

- Remove `BackgroundEffect`, `background_effects`, `next_transition` and `NextTransition`.
  Required work uses ordinary business jobs or functions within one job; callbacks remain optional.
- Remove saved-job restoration guesses. Workers require the recorded process class, declaring class, field and action.
- Migrations 0017 and 0018 refuse unsupported unfinished work before removing seven obsolete job columns.
  Historical migration files and compatible ordinary jobs remain available.

### Changed

- Require explicit `expect_raises` for injected `ProcessScenario` failures.
- A process call records its actual bound root class instead of trusting a forwarded `process_class` value.
- Document paged imports using saved progress and `Defer`, and manual and automatic actions sharing functions with separate queues.

### Fixed

- Temporary errors while constructing a saved job's Process or State remain retryable.
- Bound database waits while reading timeouts and recording stopped-job failures, so other running jobs retain timeout checks.

### Upgrade

- Upgrade from 4.3.2 with old producers and workers stopped. Mixed versions are unsupported.
- Finish jobs affected by removed declarations or application renames before applying migrations.
- Removed column values cannot be restored by reversing the migrations. Preserve historical metadata where required.
- See [upgrading and rollback](docs/upgrading.md).

## [4.3.2] — 2026-11-13

- `dl_worker` handles SIGTERM. It takes no new job, waits for its running jobs, and exits with code 0.
  A job process ignores SIGTERM, because Heroku sends the signal to every process of a dyno.
  A program that the job starts inherits this: stop such a program with `kill()`, not `terminate()`.
  Before, the worker and its job processes stopped at once. The next worker ran each job again from its first step.
  An application reproduced the cost with real worker processes and a mock courier.
  A stop came during a courier label request that the courier had accepted.
  The next run sent the request again, and the courier held the order twice.
  On Kubernetes the worker is the first process of its container, and it did not get SIGTERM without a handler.
  The pod worked for its whole grace period and then stopped at once.
  The platform still ends the wait at its own limit: 30 seconds on Heroku, `terminationGracePeriodSeconds` on Kubernetes.
  A job that still runs at that limit stops there, as before.
  A job that the worker claimed at the moment of the signal does not start; it stays ready for another worker.
  A safety-net pass that still runs gets five seconds, then the worker stops it. The next pass repeats its work.
  `run_worker` takes an optional `stop_requested` callable for the same purpose.
  No setting, migration or queue changes.

## [4.3.1] — 2026-11-12

- On PostgreSQL, saving a background job no longer writes a cache lock. The business-row lock protects the save.
  Before, a process killed during the save left the cache lock on the record until `LOCK_TIMEOUT`, two hours by default.
  Every transition on that record was refused with "State is locked" for that time.
  An application reproduced it. Its order import completion saves one shipping job for each order.
  An application test kills the job process right after it saves that row. The retry was refused, because the order stayed locked.
  The save still refuses a record that has a cache lock, and it still refuses a nested call for the same record.
  Other databases keep the cache lock for the save. Synchronous transitions keep their cache lock.
  No public name, setting, migration or worker configuration changes.

## [4.3.0] — 2026-11-11

- A callback or follow-up transition that fails with an ordinary error keeps what it saved before the error,
  also inside a transaction. Before, every error rolled back the callback's savepoint.
  An application's order and fulfilment workflows start each other from callbacks: a fulfilment's callback moves its order.
  When the order transition failed there, its failed state and the records of its failure callbacks were rolled back
  with the savepoint. The order kept its old state and nothing recorded the failure.
  A pull worker runs callbacks outside a transaction and already kept these writes, so tests and web requests
  behaved differently from workers.
  A database error still discards the callback's writes and keeps the caller's transaction usable.
  The error is still logged and swallowed, and later callbacks still run.
  Check callbacks that relied on their partial writes being discarded when they raise.
- In the test runtime (`enable_sync()`), an `atomic=False` background transition called inside a transaction
  runs in that transaction, on the caller's connection. Before, it refused, and such a test had to be a `TransactionTestCase`.
  An application's courier label requests need `atomic=False`, and several hundred application tests reach one inside `TestCase`.
  An ordinary error keeps the writes made so far; a database error discards the attempt's writes.
  The writes disappear if the test's own transaction rolls back.
  Pull workers and inline execution with autocommit keep the separate job connection.
  A `sync_execution()` block in a deployment that did not call `enable_sync()` keeps the refusal inside a transaction,
  because a caller rollback would discard provider receipts.
  No public name, setting, migration or worker configuration changes.

## [4.2.0] — 2026-11-11

- Add explicit `atomic=True` support to synchronous `Transition` and `DatabaseTransition` declarations.
  Required database changes and the final state save now succeed or roll back together when this option is enabled.
  An application's claim acceptance test reproduced retained claim and credit writes after a failed final state save.
  Application receipt completion and integration results also need this guarantee.
  A consumer qualification enables this option for a synchronous reconciliation action.
  Its final-state-save fault test checks rollback of required writes and a successful retry.
  Consumer declarations must explicitly enable the option and verify their rollback behavior before deployment.
  The synchronous default remains `False`; background defaults do not change.
  After rollback, failure callbacks receive reloaded model values, and the original error reaches the caller.
  Optional callbacks, writes to another database and external effects remain outside this guarantee.
  Subclasses that override `complete_transition` cannot enable the option; existing subclasses retain their default behavior.
- Save an explicitly empty target state. `target=None` still leaves the state unchanged;
  callers using `target=''` for that purpose must change it to `None`.
  An application's tracking reset tests require a saved empty status.
  Empty-state resets retain state protection, stale-state checks and transaction rollback.
  Background work can finish in the empty state after an in-progress state.
  No database migration or worker configuration change is required.

## [4.1.2] — 2026-11-10

- Release a status check's temporary job-row lock before returning, even inside a caller's transaction.
  Reading `in_flight()` no longer prevents another worker from starting the job until that caller commits.
- Keep optional follow-up lookup and guard failures inside the existing error handler and transaction savepoint.
  A failed optional follow-up no longer makes a completed action appear to fail or breaks its surrounding transaction.
- Include action, model record and saved job identity in background failure logs.
  After `dl_transitions --send`, distinguish a cleared retry wait from an attempt with no recorded result.
- Remove insert-conflict handling that public PostgreSQL submissions cannot reach.
  Keep the unfinished-job check, transaction rollback and database uniqueness constraint.
- Remove redundant worker checks and attribute defaults used only by unsupported test fixtures.
  Copy saved inputs into independent effect jobs without reading the submitting user again.
  Simplify synchronous database-only scope tracking while retaining row protection, cache-lock checks and reentry refusal.
- Correct temporary-refusal, retry, lock-visibility and competing-start documentation.
  Restore the list of engine-managed arguments; callers must use other names for business data.
  No database migration, queue setting or provider rate change is required.

## [4.1.1] — 2026-11-05

- Remove the state re-check after the job insert. On PostgreSQL the business row is locked before the first state read and stays locked until the caller's transaction commits, so the re-check could not fire. Its two tests go with it.
- Validate hook signatures in one walk of the process tree. The undocumented `collect_hook_signature_offenders` and `collect_request_param_offenders` are removed; no consumer imported them.
- Move the boot gate into `django_logic.conf` and delete `django_logic/background/apps.py`, which defined no app.
- Delete `django_logic/background/operations.py`; the fairness key check lives in its one caller. Drop two always-true clauses from the cleanup subquery and four dead lines: an unused options dict, a class attribute that is always overwritten, a traceback re-attachment, and a `getattr` on an attribute every transition has.
- Delete test fixtures and helpers that no test used.
- Run the `DatabaseTransition` tests in the stability job. They need PostgreSQL and Redis and ran in no CI job.

## [4.1.0] — 2026-11-04

- Add `DatabaseTransition` for synchronous PostgreSQL work that can roll back completely.
  Its nested transitions and job submissions use transaction protection without persistent cache locks.
  An application import, stock and bulk-update completion can retry after a killed job without waiting for a two-hour cache timeout.
  Ordinary transitions retain shared cache protection for external calls.
  Existing cache locks, source checks, pending jobs, permissions and reentry refusal remain respected.

## [4.0.2] — 2026-11-04

- Correct guidance for synchronous transactions and callbacks, lock refusals, client ordering, effect retention, testing and release preparation.
  Keep existing APIs, execution behavior and upgrade prerequisites.

- Preserve non-atomic side effects' failures and postponements when another transaction holds the business row.
  A permanent failure stops retries; a postponed job keeps its due time and error allowance.
  An application uses both results for provider submissions and waits between manifest operations.
- Report eligible retries and expired postponements as waiting work instead of suggesting their previous attempt is still running.
- Include the full declaring process class in text statistics to distinguish same-named provider actions.
- Fail the worker-presence regression test when its worker process raises an exception after reporting presence.
- Correct worker, transaction, retry, retention and upgrade guidance to match the current implementation.

## [4.0.1] — 2026-11-04

- Select a client's ready work before an older job postponed until later.
  An application manifest completion repeatedly waited for shipping jobs that were queued behind it.
  Postponed jobs now use their existing due time for ordering within the client.
  Client turns, job locks, retry limits and database schema stay unchanged.

## [4.0.0] — 2026-11-03

- Model snapshots no longer copy partial background job records. An application's billing
  and shipping-completion tests create required jobs explicitly. Keep model-field
  capture because these current consumer tests use it.
- Remove the unused scenario process-name override and state-trace assertion.
  Engine tests observe state writes with a test-only helper. Consumer test drives
  no longer replace `State.set_state`. Unknown failure injections still fail tests.

- A refused action no longer runs every condition and permission again to build
  an unused action list. An application uses the existing reason and user message instead.
  Remove `TransitionNotAllowed.available_actions`; callers can explicitly list actions.
- Remove unused transition command-class overrides, `BackgroundEffect.no_retry_on`,
  and `ProcessManager.unbind_model_process`. Internal tests keep their own binding
  cleanup helper. An application uses stock command classes in the current engine.

- Remove unused public request keys, `operation_options`, `RequestConflict`,
  `required_next` and `cancel_pending`. An application uses none
  of these APIs. Its required work uses ordinary background jobs and saved
  business completion states. Background effects keep their unique request keys.
- Repeated submissions are refused while a job is unfinished. After completion,
  a valid action creates a new job. Concurrent duplicates roll back preparation
  writes and callbacks from the refused submission.
- Remove `request_fingerprint` and `required_next` from the model state only.
  The database columns and defaults remain so 3.3 producers can keep inserting
  during deployment. Before upgrading, verify no unfinished job has a nonempty
  `required_next`. Cleanup no longer retains public request keys indefinitely.
- Effect jobs are inserted with parent completion. Reopening a completed parent
  by hand now fails on the effect's unique key instead of reusing old effects.
- Require new public options, columns and settings to ship with a consumer that
  uses them. The release notes explain the problem solved.

- Complete jobs whose model or transition no longer exists while retaining the
  job lock. A failed completion write now reaches normal failure reporting.
- Check failed-job business state once, under its row lock. A superseded job
  now logs a warning. If that lock times out after a manual state change,
  completion is recorded as failed instead of superseded; the manual state
  remains unchanged and failure callbacks do not run.
- Share hook validation traversal and let Python reject positional transition
  arguments before work starts. Validation messages for declared hooks stay unchanged.
- Create client turn records when a worker first selects that client's jobs,
  avoiding the duplicate write during submission. Existing queued jobs remain eligible.
- Stop the worker if another handler consumed a job process's exit status.
  An unknown result can no longer count as a successful job process.

- Remove obsolete broker design notes, Docker test wrappers and duplicate
  contributor instructions. Keep the current worker, release and credential rules.
  The tag workflow remains the publishing path and retains its existing checks.

- Remove the ten migrations replaced by the initial squash. Keep its `replaces`
  history and the next migration's dependency so upgraded and rolled-back
  applications resolve the same schema. Databases on 0.17.x or earlier must
  first run `migrate` using Django Logic 3.2 before taking this release.
- Remove custom messages for the classes and app entry retired in 1.0/1.1.
  Install `django_logic` alone; removed names now use Python's normal import errors.

Earlier releases: [changelog before version 4](docs/history/CHANGELOG-pre-4.md).
