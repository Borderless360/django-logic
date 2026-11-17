# Upgrading and rollback

## From 5.1.2 to 5.2.0

Migration 0020 adds the internal state-lock and job-process ownership tables on the
default database. No business transition declaration changes are needed. The
library no longer reads or writes cache state locks, and there is no compatibility
path between the two stores.

Stop every old process that runs transitions before changing versions: web/API processes,
job producers, scheduled commands, pull workers, other task workers and management
commands. Confirm their job processes have stopped and resolve uncertain provider
results from their receipts. Apply migrations with the application stopped, then
start only the new version. Old and new processes must never overlap. Existing job
rows and business data are not rewritten by this migration.

The default PostgreSQL database must permit both ownership tables to be migrated
and written. Ownership operations use short independent connections; include their
peak connection count and connection creation rate in deployment sizing. SQLite
is supported only in the explicitly enabled single-process test runtime. Other
backends are refused. A shared cache remains useful for worker-presence reporting,
but no longer owns state locks.

The configured LOCK_TIMEOUT remains the full ownership lease. A worker removes
exact process ownership after confirmed exit, before the job becomes available to
other workers. Whole-worker loss still relies on the lease limit; it is not proof
that an external operation stopped. Preserve provider receipts and duplicate
protection. Unknown process-owner records remain available for investigation.

Rollback requires the same complete stop of all processes that run transitions and a review of unfinished
work and provider outcomes. Do not restart an old cache-locking process alongside
a new process merely because the schema migration is additive.

## From 5.1.1 to 5.1.2

Version 5.1.2 changes PostgreSQL state protection to allow foreign-key checks from related records.
It still prevents concurrent updates and deletion of the protected row. No new migration or declaration change is needed.
Review explicit application locks and any business rules that depend on a fixed related-record collection.
See [state protection](transactions.md#state-protection).
Stop producers and workers before replacing their application version, then restart and verify retained jobs.

## From 5.1.0 to 5.1.1

Version 5.1.1 fixes application-context error handling and removes the unused `TransitionMessage.operation_name` alias.
Use `transition_name`. No new migration is needed. Stop producers and workers before replacing their application version,
then verify retained jobs and their application context after restarting.

## From 5.0 to 5.1

Migration 0019 adds nullable application job context and removes six database defaults used by older producers.
Python model defaults remain. Existing jobs keep `job_context=None`; configured adapters must clear attribution for that value.
Stop all job producers, scheduled submissions and workers. Confirm that no job process remains.
Install the selected application version, run `migrate`, then start only that version's producers and workers.
Migration 0019 has no bounded table-lock wait. A running job or another open transaction can block it and delay later submissions.
If it still waits with the application stopped, inspect the PostgreSQL lock holder and resolve that transaction before continuing.
No historical jobs are rewritten. Do not run mixed producer and worker versions.
Django 5.2 is the minimum supported framework version. PostgreSQL workers require psycopg 3.2 or later, or Django’s supported psycopg2 driver.
Install the `postgres` extra for psycopg 3 and the `redis` package when using Django’s Redis cache.
Unknown transition declaration options now raise `TypeError`. Bind every process through an installed `django_logic` application.
`ProcessScenario.retry_transition()` refuses a future deferral; assert that wait with `assert_deferred()` before advancing the test clock.

## From 4.3.2 to version 5

Version 5 is a breaking release. Its supported upgrade baseline is 4.3.2.
Earlier installations must complete their earlier upgrade steps first, using
the corresponding release documentation. Historical migration files remain so
Django can recognize applied migrations; they do not provide an older runtime.

Before upgrading:

1. Remove every `next_transition` declaration and `NextTransition` import.
   Put required work in saved jobs. Use an ordinary callback only when its
   failure or loss is acceptable; replacing the name alone does not make work durable.
   Remove `BackgroundEffect` and `background_effects` declarations too.
   Move required functions into their job that performs the work, or save business jobs with the result that requests them.
2. Make injected `ProcessScenario` failures explicit with `expect_raises=ErrorType`
   or `expect_raises=False`. Omitting it no longer absorbs the injected exception.
3. Keep root process classes importable and preserve their `process_name`, bound
   field, class that declares the transition and action names while jobs using them remain unfinished.
   Submit background jobs through a `Process` to record all identity fields.
   Low-level `change_state` calls must supply the same complete, valid identity. Attribute lookup, renamed-class and action-name fallbacks are removed.
4. Finish or explicitly resolve unsupported unfinished jobs on 4.3.2, including
   jobs with `required_next` instructions or incomplete identity. Do not mark work
   complete merely to pass the migration. When application declarations are removed,
   finish their jobs before installing that application change.
   Finish all pending background effects and parents that still promise them using the installed version.
5. Stop all job producers, scheduled submissions and old workers. Confirm that no
   old job process remains. Install the new application and run `migrate` before
   starting the new application processes. Preserve any removed metadata that the environment must retain.

Migration 0017 checks unfinished work before removing the physical
`request_fingerprint` and `required_next` columns. A failed check leaves the
columns and jobs intact. Resolve the reported work using the previous application,
then retry. The migration does not call providers, replay jobs or rewrite identities.
A bounded PostgreSQL lock wait can also stop the migration; retry after the holder
finishes, with producers still stopped.

Migration 0018 refuses unfinished jobs with background-effect metadata or a nonempty request key.
It then removes `request_key`, `background_effect_names`, `effect_name`, `parent_transition_name` and `parent_job_id`.
Compatible ordinary pending jobs remain available and completed job rows remain in history.
Reversing the migration cannot restore removed metadata. Preserve an archive if the environment requires that history.

Start only producers and workers of the selected version. Verify the installed package, queue coverage
and completion of retained jobs. Mixing different producer and worker versions is
unsupported. This release is not a normal rolling upgrade.

Before rollback, stop new submissions and workers and review every unfinished job
against the previous application's declarations. Reversing 0017 and 0018 recreates empty
obsolete columns; it cannot restore removed values or reverse application changes.
Do not start the previous application until its schema and saved jobs are compatible.
