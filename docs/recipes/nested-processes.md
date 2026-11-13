# Recipe: a parent process with many child processes

An order can start several fulfilments and wait for their results. Each fulfilment
has its own saved job and failure state. The order also needs saved work that
checks those results; optional completion callbacks cannot guarantee that check.

## Save the submissions together

Use the parent's `submission_context` to submit the child jobs in the same database
transaction as the parent job. If any submission fails, the transaction rolls back
all submissions and state changes. The parent job then checks committed child states
and uses `Defer` while work remains. It does not depend on child callbacks.

This example assumes the application's child list is fixed while the order is
processing, and that all records and jobs use the same PostgreSQL database.

```python
from contextlib import contextmanager
from datetime import timedelta

from django.utils import timezone
from django_logic import Process
from django_logic.background import BackgroundTransition, Defer, PermanentFailure


@contextmanager
def submit_fulfillments(transition, state, kwargs, *, using):
    for fulfillment in state.instance.fulfillments.order_by('pk'):
        fulfillment.process.fulfill(user=kwargs.get('user'))
    yield


def check_fulfillments(order, **kwargs):
    statuses = list(order.fulfillments.values_list('status', flat=True))
    if any(status not in {'fulfilled', 'failed'} for status in statuses):
        raise Defer(
            until=timezone.now() + timedelta(seconds=10),
            reason='Waiting for fulfilment results',
        )
    if 'failed' in statuses:
        raise PermanentFailure('One or more fulfilments need attention')


class OrderProcess(Process):
    transitions = [
        BackgroundTransition(
            action_name='fulfill',
            sources=['new'],
            in_progress_state='fulfilling',
            target='fulfilled',
            failed_state='action_required',
            submission_context=submit_fulfillments,
            side_effects=[check_fulfillments],
        ),
    ]


class FulfillmentProcess(Process):
    transitions = [
        BackgroundTransition(
            action_name='fulfill',
            sources=['pending'],
            in_progress_state='fulfilling',
            target='fulfilled',
            failed_state='failed',
            side_effects=[do_fulfill],  # Application function with safe retry behavior.
        ),
    ]
```

The parent waits until every child has finished before deciding its result. A child
failure changes the parent's result to `action_required`; it does not undo other
children's completed work. Read the failed child records to show which need attention.
A retry policy must explicitly decide which failed children can safely run again.

Do not submit children for the first time inside an atomic attempt that then raises
`Defer`: that exception rolls back the new child jobs. The submission context above
commits them before the parent worker checks their results. Keep provider requests
out of submission preparation; it must contain only reversible database changes.

## Completion notifications

Use callbacks for optional notifications.
Keep required functions in the child's ordinary job when they share its completion result.
Otherwise save a separate business job with the result that requests it, inside one database transaction.
A required parent check must remain independently retryable. Catching every
`TransitionNotAllowed` and returning success can lose the check when another
transaction temporarily holds the parent record.

## Write each sibling process in full

When nested processes share an action name, write each declaration in full: sources,
conditions, side effects and queue. The worker records the exact owning class and
restores that declaration. Keep it importable until its saved jobs have finished.
