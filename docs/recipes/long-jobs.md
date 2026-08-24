# Long jobs: one row per chunk

## The problem

A `BackgroundAction` runs its whole side-effect inside one attempt, and
the attempt is all-or-nothing: its database writes run in a savepoint
and roll back together when the attempt fails, is killed at its
`timeout=` budget, or dies with its worker. That rule is what lets a
retry run the side-effect again from scratch.

For a long job — importing a page of orders one by one — all-or-nothing
has two costs:

* an interrupted attempt loses every row it already wrote, so a job that
  cannot finish inside one attempt's budget never completes;
* nothing the job writes is visible to the rest of the system until the
  whole job commits, so downstream work waits for the last record.

Do not ask for a partial-commit mode. The per-attempt rollback is a
guarantee other transitions rely on; giving it up inside one long
attempt would make that attempt's retries unsafe. Split the job instead.

## The shape

Give each chunk its own background action. Each chunk is then its own
`TransitionMessage` row, its own attempt, its own savepoint, its own
retries, and its own commit:

* an interruption loses one chunk, not the job — the chunk's row stays
  uncompleted and a later claim retries it;
* every finished chunk is committed and visible at once;
* every engine guarantee holds unchanged, because no attempt is long.

Store the cursor on the instance (or on the rows the job imports), and
make each chunk idempotent against it — the same rule every background
side-effect already follows.

```python
def import_next_page(instance, **kwargs):
    """One chunk: fetch one page, import it, advance the cursor."""
    page = fetch_page(instance.import_cursor)
    for record in page.records:
        import_record(instance, record)      # idempotent per record
    instance.import_cursor = page.next_cursor
    instance.save(update_fields=['import_cursor'])


def continue_if_more_pages(instance, **kwargs):
    """Callback: chain the next chunk after this row completes."""
    if instance.import_cursor:
        instance.process.import_page()


BackgroundAction(
    action_name='import_page',
    sources=['importing'],
    side_effects=[import_next_page],
    callbacks=[continue_if_more_pages],
)
```

The callback runs after the row completes, so the next enqueue never
collides with the one-uncompleted-row-per-instance-per-process
constraint. A crash between the completion and the callback loses only
the *chaining*, not the work — cover that window with a small periodic
check on instances left in the importing state, the same backstop the
nested-processes recipe uses for its parent re-check.

For a job whose chunks are independent of each other (per-child work
rather than a cursor), fan out to one background transition per child
instead of chaining — that is the
[nested-processes recipe](nested-processes.md).
