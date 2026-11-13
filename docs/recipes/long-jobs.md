# Long jobs: one row per chunk

## The problem

By default, `BackgroundTransition` runs its whole side-effect inside one attempt,
and the attempt is all-or-nothing: its database writes run in a savepoint
and roll back together when the attempt fails, is killed at its
`timeout=` budget, or dies with its worker. That rule is what lets a
retry run the side-effect again from scratch.

For a long job — importing a page of orders one by one — all-or-nothing
has two costs:

* an interrupted attempt loses every row it already wrote, so a job that
  cannot finish inside one attempt's budget never completes;
* nothing the job writes is visible to the rest of the system until the
  whole job commits, so downstream work waits for the last record.

For a bulk import, split the job into chunks so each attempt stays small.
Keep the default atomic behavior when a chunk must succeed or fail together.

Provider requests have another requirement: their saved confirmations must
survive a later local failure. Even one request can succeed before its receipt
rolls back. Use the explicit `atomic=False` option for that case, with durable
protection against repeated requests. See [provider receipts](provider-receipts.md).

## The shape

Give each chunk its own background transition. Each chunk is then its own
`TransitionMessage` row, its own attempt, its own savepoint, its own
retries, and its own commit:

* an interruption loses one chunk, not the job — the chunk's row stays
  uncompleted and a later claim retries it;
* every finished chunk is committed and visible at once;
* a timeout or failure concerns one chunk; the application must still choose
  bounded page work and handle uncertain external outcomes.

Store the cursor on the instance (or on the rows the job imports), and
make each chunk idempotent against it — the same rule every background
side-effect already follows.

```python
from django_logic import DatabaseTransition, Process
from django_logic.background import BackgroundEffect, BackgroundTransition


def import_next_page(instance, **kwargs):
    if instance.import_cursor is None:
        return
    page = fetch_page(instance.import_cursor)
    for record in page.records:
        import_record(instance, record)  # Application code that safely repeats a record.
    instance.import_cursor = page.next_cursor
    instance.save(update_fields=['import_cursor'])


def continue_import(instance, **kwargs):
    if instance.status != 'importing':
        return
    if instance.import_cursor is None:
        instance.process.finish_import()
    else:
        instance.process.import_page()


def no_more_pages(instance, **kwargs):
    return instance.import_cursor is None


class ImportProcess(Process):
    transitions = [
        BackgroundTransition(
            action_name='import_page',
            sources=['importing'],
            side_effects=[import_next_page],
            background_effects=[BackgroundEffect('continue_import', continue_import)],
        ),
        DatabaseTransition(
            action_name='finish_import',
            sources=['importing'],
            target='imported',
            conditions=[no_more_pages],
        ),
    ]
```

This PostgreSQL example uses the default database and a process bound to `status`.
The application starts with state `importing` and an initial cursor; `None` means
there are no more pages. It calls `import_page()` once to begin. Only this workflow
changes the cursor. Use one record per import run and a new record for another
import, so a delayed effect from a cancelled run cannot act on a restarted run.

Completing a page saves its cursor and the `continue_import` effect job together.
That effect either submits the next page or records the completed import. Its
atomic transaction saves that decision together with its own completion. If the
job process stops before commit, a retry repeats the decision; if it stops after
commit, the next page or final state is already saved. A temporarily refused
submission raises normally and leaves the effect available for retry.

Optional callbacks cannot provide this guarantee. Do not schedule required chunks
from a callback and rely on a periodic scan to recreate missing work. Keep the
continuation as a saved job, and monitor failed parent and effect jobs separately.
Use measured timeouts for page work and its continuation, and handle permanently
failed imports through an explicit application recovery action.

For a job whose chunks are independent of each other (per-child work
rather than a cursor), fan out to one background transition per child
instead of chaining — that is the
[nested-processes recipe](nested-processes.md).
