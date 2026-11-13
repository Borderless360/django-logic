# Long imports: save progress after each page

A large import must keep its finished pages when a later attempt fails.
One ordinary background job can own the whole import.
Each attempt processes one page and saves its records and cursor together.
The job remains pending while another page remains.

Use `atomic=False` so an attempt can commit a finished page before postponing itself.
Put each page's database changes inside an explicit transaction.
Do not put the entire import inside one transaction.

```python
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django_logic import Process
from django_logic.background import BackgroundTransition, Defer

from imports.models import ImportRun


def import_next_page(instance, **kwargs):
    cursor = instance.import_cursor
    if cursor is None:
        return
    page = fetch_page(cursor)
    with transaction.atomic():
        current = ImportRun.objects.select_for_update().get(pk=instance.pk)
        if current.status != 'importing':
            return
        for record in page.records:
            import_record(current, record)
        current.import_cursor = page.next_cursor
        current.save(update_fields=['import_cursor'])
    if page.next_cursor is not None:
        raise Defer(
            until=timezone.now() + timedelta(seconds=1),
            reason='The import has another page.',
        )


class ImportProcess(Process):
    transitions = [
        BackgroundTransition(
            action_name='import_pages',
            sources=['pending'],
            in_progress_state='importing',
            target='imported',
            failed_state='failed',
            atomic=False,
            side_effects=[import_next_page],
        ),
    ]
```

This example uses the default PostgreSQL database and a process bound to `status`.
The application calls `import_pages()` once.
That call saves the job and the `importing` state together.
`ImportRun` is the application's import record; only this workflow changes its cursor.
`None` means there are no more pages.

The page transaction rolls back imported records and the cursor if any page write fails.
After that transaction commits, `Defer` keeps the same job available for another attempt.
A stopped job process also leaves that job available.
Its next attempt reads the saved cursor and continues from the next unfinished page.
No completion callback needs to submit another job.

When the last page commits, the worker records `imported` and job completion together.
If that final write fails, the saved cursor remains `None`.
The next attempt completes the job without importing a page again.
The worker checks the current state before recording completion, so cancellation cannot become a successful import.

Keep provider requests outside the page's database transaction.
External changes still need saved confirmations or provider request keys that prevent duplicates.
See [provider receipts](provider-receipts.md).
Choose a measured attempt timeout and a bounded page size.
An import that reaches its error limit needs an explicit business recovery decision.

Use one import record per run.
A new import uses a new record, so an old attempt cannot change a restarted import.
For independent records, submit one ordinary background job per record and save the parent check with those submissions.
See [parent and child processes](nested-processes.md).
