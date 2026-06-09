# Recipe: a parent process with many child processes

> e.g. an **order** with many **fulfillments**. The order is fulfilled only
> when *all* fulfillments succeed; if one fails, surface *which* one and why —
> without failing the whole order.

This is the supported alternative to the anti-pattern that motivated the
durable model (see `fundamental problem.md`): **do not** call a child's
transition inside the parent transition's side-effect and let its exception
propagate. That couples the parent's success to every child and cascades
failures across state machines.

## The four rules

1. **One transition = one task = one unit of failure.** Each child is its own
   `BackgroundTransition` with its own `failed_state`. A child failure is
   *contained* on the child row, never raised.
2. **The parent fans out, it does not drive.** The parent transition only
   *starts* each child's background transition (phase 1) and returns. No child
   work runs inside the parent transition, so no child exception can reach it.
3. **Children report back via best-effort callbacks** (`Callbacks.execute`
   swallows exceptions) that run an **idempotent, guarded completion check**.
4. **Aggregate errors by reading child rows**, not by catching exceptions. Give
   the parent an explicit partial-failure state (e.g. `action_required`).

## Sketch

```python
from django_logic import Process, Transition
from django_logic.background import BackgroundTransition
from django_logic.conditions import all_related_in, any_related_in

class FulfillmentProcess(Process):                  # the child
    transitions = [
        BackgroundTransition(
            action_name='fulfill', sources=['pending'], target='fulfilled',
            in_progress_state='fulfilling', failed_state='failed',   # contained
            queue='django_logic.critical',
            side_effects=[do_fulfill],                # idempotent
            callbacks=[report_to_order],              # success → notify parent
            failure_callbacks=[record_error_and_report],  # failure → notify parent
        ),
    ]

class OrderProcess(Process):                         # the parent (synchronous)
    transitions = [
        Transition('start', sources=['new'], target='fulfilling',
                   side_effects=[fan_out], callbacks=[recheck]),
        Transition('mark_fulfilled', sources=['fulfilling'], target='fulfilled',
                   conditions=[all_related_in('fulfillments', 'status', {'fulfilled'})]),
        Transition('mark_action_required', sources=['fulfilling'], target='action_required',
                   conditions=[all_related_in('fulfillments', 'status', {'fulfilled', 'failed'}),
                               any_related_in('fulfillments', 'status', {'failed'})],
                   side_effects=[aggregate_errors]),
    ]

def fan_out(order, **kw):
    for child in order.fulfillments.all():
        try:
            child.process.fulfill()        # phase 1 only — dispatches the child's task
        except Exception as exc:           # contain: one child that can't START ≠ failed fan-out
            log.warning('could not start fulfillment %s: %s', child.pk, exc)

def report_to_order(child, **kw):          # success callback
    _check(child.order_id)

def record_error_and_report(child, exception=None, **kw):   # failure callback
    child.error = str(exception); child.save(update_fields=['error'])   # record on the CHILD
    _check(child.order_id)

def recheck(order, **kw):                  # parent callback after `start` unlocks
    _check(order.pk)                       # closes the "child finished during fan-out" race

def _check(order_id):
    order = Order.objects.get(pk=order_id)
    try:
        order.process.mark_fulfilled()         # guards decide if it fires; idempotent
    except TransitionNotAllowed:
        pass
    try:
        order.process.mark_action_required()
    except TransitionNotAllowed:
        pass

def aggregate_errors(order, **kw):
    failed = order.fulfillments.filter(status='failed')
    order.error_summary = ' | '.join(f'{c.label}: {c.error}' for c in failed)
    order.save(update_fields=['error_summary'])
```

## Why it's safe under concurrency

Many children finish at once; each callback calls `_check`. The parent's state
lock + the guard conditions mean only the **last** child to reach a terminal
state actually fires the transition; the rest get `TransitionNotAllowed` and
are swallowed. Each child's callback runs after its own commit, so at least one
observes the complete terminal set; the parent's post-`start` `recheck` is a
backstop. Crash safety is inherited from the children (durable background
transitions): if a worker dies mid-child, the periodic starter re-runs it and
its callback fires when it finally reaches a terminal state.

A full, tested implementation lives in the validation harness:
[django-logic-test `fulfillment/`](https://github.com/Borderless360/django-logic-test/tree/main/fulfillment)
+ `docs/design/NESTED_PROCESS_ERROR_HANDLING.md`.
