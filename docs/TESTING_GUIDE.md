# Testing Guide — how to test django-logic processes

> The one rule this whole guide follows: **you test your process, not the
> background machinery.** Delivery, retries, durability, crash recovery and
> queue routing are the library's job — guaranteed by its own regression
> suite, a PostgreSQL + Redis stability suite, and a production-style Heroku
> validation matrix (see [How the library itself is tested](#how-the-library-itself-is-tested)).
> Your tests run the *business process* — every transition, condition,
> permission, failure and retry — **entirely without Celery**.

## Table of contents

1. [Setup](#setup)
2. [The scenario catalog](#the-scenario-catalog)
3. [ProcessScenario API reference](#processscenario-api-reference)
4. [Testing without ProcessScenario](#testing-without-processscenario)
5. [How the library itself is tested](#how-the-library-itself-is-tested)

---

## Setup

Background transitions execute through Celery by default
(`BACKGROUND_EXECUTION` defaults to `'celery'`). Tests opt into **sync
execution mode**, where phase 1 (validate + persist the `TransitionMessage` +
write `in_progress_state`) and phase 2 (side-effects + target state) run
inline in the test process — the *real* code paths, not a re-implementation:

```python
# settings_test.py
DJANGO_LOGIC = {
    'BACKGROUND_EXECUTION': 'sync',
}
```

SQLite is fine for process tests (the library refuses SQLite only in celery
mode, where the concurrency guard needs real row locking). No broker, no
worker, no beat.

If a specific test file needs sync mode while the global setting is
`'celery'`, use the context manager instead:

```python
from django_logic.background import sync_execution

with sync_execution():
    order.process.fulfil()
```

**What you should test** — your states, transitions, conditions, permissions,
side-effect behaviour, failure handling, and retry outcomes.

**What you should NOT test** — that Celery delivers tasks, that the broker
survives restarts, that beat schedules the safety nets, that `acks_late`
re-delivers after a worker crash. Those are library guarantees; sync mode
deliberately removes them from your test surface.

---

## The scenario catalog

Every scenario below uses `django_logic.testing.ProcessScenario` — a
`TransactionTestCase` subclass where tests read like the business story.
The running example:

```python
# processes.py
class OrderProcess(Process):
    process_name = 'process'
    transitions = [
        Transition(
            action_name='approve', sources=['draft'], target='approved',
            conditions=[has_stock], permissions=[is_staff],
            side_effects=[validate_order],
        ),
        BackgroundTransition(
            action_name='fulfil', sources=['approved'], target='fulfilled',
            in_progress_state='fulfilling', failed_state='fulfilment_failed',
            queue='critical',
            side_effects=[reserve_stock, call_courier],
            callbacks=[send_confirmation_email],
        ),
        BackgroundAction(
            action_name='sync_inventory', sources=['fulfilled'],
            side_effects=[push_to_erp],
        ),
        Transition(action_name='cancel', sources=['draft', 'approved'],
                   target='cancelled'),
    ]

# tests.py
from django_logic.testing import ProcessScenario

class OrderScenario(ProcessScenario):
    process_class = OrderProcess
    model = Order
    state_field = 'status'      # default 'status'
    process_name = 'process'    # default 'process'
```

### 1. Happy path through several transitions

The baseline scenario: drive the process end to end, assert each state.

```python
def test_order_lifecycle(self):
    order = self.create_instance(status='draft')
    self.transition(order, 'approve', user=self.staff)
    self.assert_state(order, 'approved')

    self.background_transition(order, 'fulfil')   # phase 1 + 2 inline, no Celery
    self.assert_state(order, 'fulfilled')
    self.assert_side_effects_ran(['reserve_stock', 'call_courier'])
    self.assert_callbacks_ran(['send_confirmation_email'])
```

### 2. Condition gating

Conditions decide whether a transition is *available*. Test both sides.

```python
def test_cannot_approve_without_stock(self):
    order = self.create_instance(status='draft', stock=0)
    self.assert_not_available(order, ['approve'])

def test_can_approve_with_stock(self):
    order = self.create_instance(status='draft', stock=5)
    self.assert_available(order, ['approve'])
```

### 3. Permission gating

```python
def test_only_staff_can_approve(self):
    order = self.create_instance(status='draft')
    self.assert_available(order, ['approve'], user=self.staff)
    self.assert_not_available(order, ['approve'], user=self.customer)
```

> ⚠️ Permissions are only evaluated when a `user=` is passed. A call without
> one is a *system call* and bypasses permissions entirely — so a test that
> drives `self.transition(order, 'approve')` without `user=` proves the
> machine works, **not** that the endpoint is protected. Test permission
> denial with an explicit unauthorized user, and make sure your views always
> pass `user=request.user`.

### 4. Synchronous failure → failed_state + failure hooks

Inject a failure into one *named* side-effect — every other side-effect runs
for real, so the genuine failure path executes.

```python
def test_validation_failure_voids_the_order(self):
    order = self.create_instance(status='draft')
    self.transition(order, 'approve',
                    fail_side_effect='validate_order',
                    fail_with=ValueError('bad address'))
    self.assert_state(order, 'draft')        # or the failed_state if declared
```

### 5. Action — side-effects without a state change

```python
def test_sync_inventory_keeps_state(self):
    order = self.create_instance(status='fulfilled')
    self.background_transition(order, 'sync_inventory')
    self.assert_state(order, 'fulfilled')     # unchanged on success
    self.assert_side_effects_ran(['push_to_erp'])
```

### 6. Background failure → in-progress + recorded error

A failed background attempt leaves the instance in `in_progress_state`, the
error recorded on the durable row, and the row uncompleted (the periodic
starter would retry it in production).

```python
def test_courier_failure_is_recorded(self):
    order = self.create_instance(status='approved')
    self.background_transition(
        order, 'fulfil',
        fail_side_effect='call_courier',
        fail_with=ConnectionError('Aramex timeout'))

    self.assert_state(order, 'fulfilling')        # left in progress
    self.assert_error_recorded(order, 'Aramex timeout')
    self.assert_error_count(order, 1)
    self.assert_side_effects_not_ran(['call_courier'])
```

**Per-attempt rollback (0.4+):** the failed attempt's *database* writes are
rolled back — `reserve_stock`'s rows do not survive attempt 1, and the retry
re-creates them exactly once. The idempotency you still owe is for *external*
calls (`call_courier` may genuinely fire twice across attempts).

```python
def test_failed_attempt_rolls_back_db_writes(self):
    order = self.create_instance(status='approved')
    self.background_transition(order, 'fulfil',
                               fail_side_effect='call_courier',
                               fail_with=ConnectionError('boom'))
    self.assertFalse(StockReservation.objects.filter(order=order).exists())
```

### 7. Retry to success

`retry_transition` does exactly what the periodic starter does in production:
re-runs the instance's uncompleted transition.

```python
def test_retry_completes_after_transient_failure(self):
    order = self.create_instance(status='approved')
    self.background_transition(order, 'fulfil',
                               fail_side_effect='call_courier',
                               fail_with=ConnectionError('transient'))
    self.assert_state(order, 'fulfilling')

    self.retry_transition(order)                  # the starter's re-dispatch
    self.assert_state(order, 'fulfilled')
```

### 8. Terminal failure at MAX_ERRORS → failed_state

```python
@override_settings(DJANGO_LOGIC={'BACKGROUND_EXECUTION': 'sync',
                                 'TRANSITION_MESSAGE_MAX_ERRORS': 2})
def test_persistent_failure_reaches_failed_state(self):
    order = self.create_instance(status='approved')
    self.background_transition(order, 'fulfil',
                               fail_side_effect='call_courier',
                               fail_with=ConnectionError('down'))
    self.retry_transition(order,                     # attempt 2 = terminal
                          fail_side_effect='call_courier',
                          fail_with=ConnectionError('down'))
    self.assert_state(order, 'fulfilment_failed')
    self.assert_error_count(order, 2)
```

### 9. One in flight: AlreadyInProgress and the sync gate

While an uncompleted `TransitionMessage` exists for an instance + process,
a second background transition raises `AlreadyInProgress`, and a synchronous
transition on the same instance + process raises `TransitionNotAllowed`.
Both are worth pinning if your UX depends on them:

```python
from django_logic.background.exceptions import AlreadyInProgress
from django_logic.exceptions import TransitionNotAllowed

def test_cannot_cancel_mid_fulfilment(self):
    order = self.create_instance(status='approved')
    self.background_transition(order, 'fulfil',
                               fail_side_effect='call_courier',
                               fail_with=ConnectionError('x'))   # row stays open

    with self.assertRaises(TransitionNotAllowed):
        order.process.cancel()                    # sync transition gated

    with self.assertRaises(AlreadyInProgress):
        with sync_execution():
            order.process.fulfil()                # second background gated
```

Design consequence: chain follow-up background work from a *terminal* hook
(a callback that fires after the row completes), never from inside the flight.

### 10. The superseded scenario (manual ops fix wins)

If something external moves the instance while a background row is pending —
a support engineer in the admin, a data migration — phase 2 must NOT undo it.
Reproduce it by completing the ops fix between failure and retry:

```python
def test_ops_fix_is_not_clobbered_by_a_late_retry(self):
    order = self.create_instance(status='approved')
    self.background_transition(order, 'fulfil',
                               fail_side_effect='call_courier',
                               fail_with=ConnectionError('x'))
    self.assert_state(order, 'fulfilling')

    # Support manually resolves the order while the row is pending.
    order.status = 'cancelled_by_support'
    order.save(update_fields=['status'])

    self.retry_transition(order)                  # late retry fires...
    self.assert_state(order, 'cancelled_by_support')   # ...and yields
    self.assert_error_recorded(order, '[superseded]')
```

### 11. next_transition chains

```python
def test_pay_chains_into_processing(self):
    order = self.create_instance(status='pending')
    self.transition(order, 'pay')
    self.assert_state(order, 'processing')        # follow-up ran after unlock
```

### 12. Nested processes

Background transitions declared on nested processes restore correctly in
phase 2 — drive them through the parent's bound property like production code
would:

```python
class CourierScenario(ProcessScenario):
    process_class = OrderParentProcess   # has nested_processes=[CourierProcess]
    model = Order

    def test_nested_dispatch(self):
        order = self.create_instance(status='submitted')
        self.background_transition(order, 'dispatch')   # lives on the nested process
        self.assert_state(order, 'dispatched')
```

### 13. Snapshot & replay — turn a production bug into a test

Capture a stuck instance in production (shell, admin action, Sentry hook):

```python
from django_logic.testing import snapshot
data = snapshot(order)    # JSON-able: fields, state, TransitionMessage
```

Reproduce and prove the fix:

```python
def test_reproduce_stuck_order_12345(self):
    order = self.from_snapshot('fixtures/bug_12345.json')
    self.assert_state(order, 'fulfilling')
    self.retry_transition(order)
    self.assert_state(order, 'fulfilled')
```

---

## ProcessScenario API reference

Class attributes: `process_class`, `model`, `state_field` (default
`'status'`), `process_name` (default `'process'`), `snapshot_on_failure`
(default `False` — when `True`, assertion failures attach a reproducible
snapshot).

**Driving the process**

| Method | What it does |
|---|---|
| `create_instance(**fields)` | Create a model instance (state via the `state_field` kwarg). Override for factories. |
| `transition(obj, action, **kwargs)` | Run a synchronous transition through the normal entrypoint. |
| `background_transition(obj, action, **kwargs)` | Run a `BackgroundTransition`/`BackgroundAction` phase 1 **and** phase 2 inline. |
| `retry_transition(obj)` | Re-run the instance's uncompleted `TransitionMessage` — simulates the periodic starter. |
| `snapshot(obj)` / `from_snapshot(data_or_path)` | Capture / rebuild instance + `TransitionMessage` state. |

`transition`, `background_transition` and `retry_transition` all accept
`fail_side_effect='name'` + `fail_with=SomeError(...)`: only the named
side-effect is wrapped to raise; everything else runs for real, and the
injected exception is absorbed so you can assert on the recorded outcome.
Any *other* (unexpected) exception fails the test loudly.

**Assertions**

| Assertion | Checks |
|---|---|
| `assert_state(obj, expected)` | The persisted state field. |
| `assert_available(obj, actions, user=None)` | Actions currently offered by `get_available_actions`. |
| `assert_not_available(obj, actions, user=None)` | Actions not offered. |
| `assert_side_effects_ran(names)` / `assert_side_effects_not_ran(names)` | Which side-effects executed in the last tracked transition (by function `__name__` — tracked, not mocked: the real code ran). |
| `assert_callbacks_ran(names)` | Which callbacks executed. |
| `assert_error_recorded(obj, contains)` | Substring of `last_error_message` on the latest `TransitionMessage`. |
| `assert_error_count(obj, expected)` | `errors_count` on the latest `TransitionMessage`. |

On failure, every assertion raises with a numbered timeline of each step the
test took, the relevant `TransitionMessage`, and (opt-in) a snapshot — built
for humans *and* AI agents to diagnose without re-running.

---

## Testing without ProcessScenario

Plain `TestCase` works fine once sync mode is on — the scenario class is
convenience, not a requirement:

```python
class FulfilmentTests(TestCase):     # DJANGO_LOGIC['BACKGROUND_EXECUTION']='sync'
    def test_happy_path(self):
        order = Order.objects.create(status='approved')
        order.process.fulfil()
        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')

    def test_side_effect_failure_propagates(self):
        # In sync mode the side-effect exception re-raises to the caller
        # AFTER being recorded on the TransitionMessage.
        order = Order.objects.create(status='approved')
        with patch('myapp.services.call_courier', side_effect=CourierError):
            with self.assertRaises(CourierError):
                order.process.fulfil()
        tm = TransitionMessage.objects.get(instance_id=str(order.pk))
        self.assertEqual(tm.errors_count, 1)
```

Two lower-level helpers mirror production behaviour exactly:

```python
from django_logic.background.dispatch import retry_pending
retry_pending()   # one tick of the periodic starter, inline (sync mode)

from django_logic.background.runner import run_background_transition
run_background_transition(tm.pk)   # one phase-2 attempt for a specific row
```

Use `TransactionTestCase` (or `ProcessScenario`, which extends it) when your
assertions depend on real transaction boundaries — e.g. proving a failed
attempt's writes rolled back.

---

## How the library itself is tested

You don't have to take the durability contract on faith — this is the test
pyramid backing it:

1. **Unit + regression suite** (`python tests/manage.py test`, SQLite,
   ~310 tests): every reproduced defect from the 0.3 stability review has a
   permanent regression test — savepoint isolation of side-effects
   (`tests/background/test_savepoint_isolation.py`), the phase-2 state guard
   (`test_phase2_state_guard.py`), the per-process in-flight constraint
   (`test_constraint_per_process.py`), restore verification
   (`test_restore_verification.py`), sync/background mutual exclusion
   (`test_sync_background_mutex.py`), and lock revalidation
   (`tests/test_lock_revalidate.py`).
2. **PostgreSQL + Redis stability suite** (`make stability-up &&
   make stability-test`, also a GitHub Actions workflow on every PR and
   nightly): real row locking, real concurrent transactions, deadlock and
   crash scenarios under `tests/stability/`.
3. **The Heroku validation matrix** ([django-logic-test](https://github.com/Borderless360/django-logic-test)):
   a deployed harness (RabbitMQ + PostgreSQL + Redis + separate worker/beat
   dynos) running an 18-row matrix on real infrastructure — worker SIGKILL
   mid-task, deploys mid-flight, queue isolation, pgbouncer transaction
   pooling, stuck-row finalization, timeout watchdogs.

That layering is exactly why your own tests can stop at the process level.
