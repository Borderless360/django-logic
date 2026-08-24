# Testing Guide — how to test django-logic processes

> Two rules this whole guide follows:
>
> 1. **You test your process, not the background machinery.** Delivery,
>    retries, durability, crash recovery and queue routing are the library's
>    job — guaranteed by its own regression suite, a PostgreSQL + Redis
>    stability suite, and a production-style Heroku validation matrix (see
>    [How the library itself is tested](#how-the-library-itself-is-tested)).
>    Your tests run the *business process* **entirely without services**.
> 2. **You test the object's journey, not the wiring.** Assert what the object
>    *became* as it moved through the process — its state trajectory, the
>    fields the side-effects changed, and what happens to the caller on failure
>    — not merely that a hook you declared got called. See
>    [Journeys, not mirrors](#journeys-not-mirrors).

## Table of contents

1. [Journeys, not mirrors](#journeys-not-mirrors)
2. [Setup](#setup)
3. [The scenario catalog](#the-scenario-catalog)
4. [ProcessScenario API reference](#processscenario-api-reference)
5. [Testing without ProcessScenario](#testing-without-processscenario)
6. [How the library itself is tested](#how-the-library-itself-is-tested)

---

## Journeys, not mirrors

A process test is only worth writing if it can *fail* when the process
misbehaves. Two kinds of test live in most FSM suites — one of them can't:

**Mirror tests** restate the definition or the implementation. They assert
that the transition you declared is available, that the side-effect you listed
got called, or they `patch` an engine internal and check it was invoked. They
pass whenever the code and the test were written from the same source — the
code itself — so a regression that keeps the wiring intact but changes the
*behaviour* sails straight through. Worse: when an AI regenerates the
implementation it regenerates the matching mirror test in the same pass, so the
suite becomes self-fulfilling and prevents nothing.

**Journey tests** drive a real, persisted object through a transition and
assert what happened *to it*: the state before → after (including the
in-progress and failed states, not just the happy target), the fields the
side-effects changed, the order effects ran in, and — on failure — where the
object landed, which failure hooks ran, and **what reached the caller**. These
express intent the code doesn't contain, so they still fail when the engine (or
a refactor, or an AI rewrite) changes behaviour.

> The real-world proof: the `0.1.6 → 0.2.0` upgrade flipped one line
> (`SideEffects.execute` began swallowing the exception instead of re-raising
> it), cascading into double failure-hook runs and changed HTTP semantics —
> while every definition-mirroring test stayed green. A journey test that
> pinned *"a failing charge must re-raise to the caller"* would have caught it
> before the upgrade shipped. See `tests/test_exception_semantics.py`.

### The rule for every process test

Assert at least:

1. the **before → after state** of a real, persisted instance;
2. at least one **field / DB effect** the side-effects produced (via
   `assert_changed` / `assert_related_count`, or a direct DB read — not a mock);
3. for any transition with a `failed_state` or failure hooks, one
   **failure-path** variant showing where the object lands, which failure hooks
   ran, and — with `expect_raises` — what propagates to the caller.

Mock only true externals (an HTTP client, a courier API, a payment gateway),
**never the process machinery itself**.

### Guardrails (worth enforcing in review, human or AI)

Reject a process test that:

- **(a)** asserts `get_available_transitions` / the transition list back
  against the definition instead of asserting *availability behaviour*. Write
  a blocked-by-condition or permission-denied journey instead: call
  `assert_not_available`, then force the transition and assert it raises
  `TransitionNotAllowed` and changed no state. Worked examples live in the
  library's own `tests/test_process_guards.py`;
- **(b)** `patch`es any `django_logic` internal (use `fail_side_effect=` to
  inject a failure into the real path instead);
- **(c)** contains no assertion against a persisted instance (a test that only
  checks a module global or a mock can pass while the object is wrong).

`assert_side_effects_ran` / `assert_callbacks_ran` are **wiring** checks — they
prove a hook was called, not that it did the right thing. Always pair them with
an outcome assertion (`assert_changed`, `assert_related_count`, `assert_state`,
or a direct DB read).

---

## Setup

Background transitions execute on pull workers by default
(`BACKGROUND_EXECUTION` defaults to `'pull'`). Tests opt into **sync
execution mode**, where enqueue (validate + persist the `TransitionMessage` +
write `in_progress_state`) and execute (side-effects + target state) run
inline in the test process — the *real* code paths, not a re-implementation:

```python
# settings_test.py
DJANGO_LOGIC = {
    'BACKGROUND_EXECUTION': 'sync',
}
```

SQLite is fine for process tests (the library refuses SQLite only in pull
mode, where the claim needs real row locking). No services, no worker
processes.

If a specific test file needs sync mode while the global setting is
`'pull'`, use the context manager instead:

```python
from django_logic.background import sync_execution

with sync_execution():
    order.process.fulfil()
```

**What you should test** — your states, transitions, conditions, permissions,
side-effect behaviour, failure handling, and retry outcomes.

**What you should NOT test** — that the worker loop claims rows, that the database
survives restarts, that the safety nets run on their cadence, that a
crashed worker's row is claimed again. Those are library guarantees; sync
mode deliberately removes them from your test surface.

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
    process_name = 'process'    # optional; defaults to process_class.process_name
```

### 1. Happy path through several transitions

The baseline scenario: drive the process end to end, assert each state.

```python
def test_order_lifecycle(self):
    order = self.create_instance(status='draft')
    self.transition(order, 'approve', user=self.staff)
    self.assert_state(order, 'approved')

    self.background_transition(order, 'fulfil')   # enqueue + execute inline, no Celery
    self.assert_state(order, 'fulfilled')
    self.assert_side_effects_ran(['reserve_stock', 'call_courier'])
    self.assert_callbacks_ran(['send_confirmation_email'])
```

### 2. Synchronous failure → failed_state + failure hooks + re-raise

Inject a failure into one *named* side-effect — every other side-effect runs
for real, so the genuine failure path executes. A synchronous `SideEffects`
failure runs `fail_transition` (writes `failed_state`, runs the failure hooks)
and then **re-raises to the caller** — pin that with `expect_raises`.

```python
def test_validation_failure_voids_the_order(self):
    order = self.create_instance(status='draft')
    self.transition(order, 'approve',
                    fail_side_effect='validate_order',
                    fail_with=ValueError('bad address'),
                    expect_raises=ValueError)          # <- the caller sees it
    self.assert_state(order, 'draft')                  # or failed_state if declared
    # If approve declared failure hooks, assert they ran:
    # self.assert_failure_callbacks_ran(['notify_ops'])
```

`expect_raises` is what makes this a journey test rather than a mirror:
without it the harness absorbs the injected exception, so the test would pass
whether the engine re-raised or silently swallowed. The full contract:
`side_effects` re-raise to the caller; `callbacks` and `next_transition`
are swallowed (best-effort).

### 3. Background failure → in-progress + recorded error

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

### 4. Terminal failure at MAX_ERRORS → failed_state

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

### 5. Snapshot & replay — turn a production bug into a test

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

The five scenarios above are the canonical shapes; every other case
(condition/permission gating, retry-to-success, the one-in-flight gate,
superseded rows, next_transition chains, nested processes, the caller
boundary, the cross-machine cascade) follows the same pattern and is
pinned in the library's own suite under `tests/` — copy from there when
you need one.

## ProcessScenario API reference

Class attributes: `process_class`, `model`, `state_field` (default
`'status'`), `process_name` (optional — defaults to `process_class.process_name`).

**Driving the process**

| Method | What it does |
|---|---|
| `create_instance(**fields)` | Create a model instance (state via the `state_field` kwarg). Override for factories. |
| `transition(obj, action, **kwargs)` | Run a synchronous transition through the normal entrypoint. |
| `background_transition(obj, action, **kwargs)` | Run a `BackgroundTransition`/`BackgroundAction` enqueue **and** execute inline. |
| `retry_transition(obj)` | Re-run the instance's uncompleted `TransitionMessage` — simulates a worker's next claim. |
| `snapshot(obj)` / `from_snapshot(data_or_path)` | Capture / rebuild instance + `TransitionMessage` state. |

`transition`, `background_transition` and `retry_transition` all accept:

- `fail_side_effect='name'` + `fail_with=SomeError(...)` — only the named
  side-effect is wrapped to raise; everything else runs for real. Any *other*
  (unexpected) exception fails the test loudly.
- `expect_raises=` — pin the caller boundary. An **exception type** (or tuple)
  asserts it propagated to the caller (the `SideEffects` re-raise contract);
  **`False`** asserts nothing propagated (the swallow contract). Omitted (the
  legacy default), an injected failure is absorbed so you can assert on the
  *recorded* error instead.

**Assertions**

*State & availability*

| Assertion | Checks |
|---|---|
| `assert_state(obj, expected)` | The persisted state field. |
| `assert_state_trace(states)` | The ordered states the object passed through in the last drive (in-progress → target, `next_transition` follow-ups, `failed_state`). |
| `assert_available(obj, actions, user=None)` / `assert_not_available(...)` | Actions offered / not offered by `get_available_actions` — test availability *behaviour*, not the definition. |

*Domain outcome (assert what the object became)*

| Assertion | Checks |
|---|---|
| `capture(obj, fields)` | Snapshot named fields as a baseline (DB-fresh; does not mutate `obj`). Returns a dict for the asserts below. |
| `assert_changed(obj, before, {field: (old, new)})` | Each field held `old` before and holds `new` now — fails if a hook ran but produced the wrong change. |
| `assert_unchanged(obj, before, fields)` | The named fields still hold their `before` values. |
| `assert_related_count(queryset, n)` | A queryset / related manager currently has `n` rows (for `delete_*` / generate-style hooks). |

*Wiring (a hook ran — pair with an outcome assertion above)*

| Assertion | Checks |
|---|---|
| `assert_side_effects_ran(names)` / `assert_side_effects_not_ran(names)` | Which side-effects executed in the last tracked drive (by function `__name__` — tracked, not mocked: the real code ran). Tracking covers the whole process tree, including `next_transition` follow-ups. |
| `assert_callbacks_ran(names)` | Which callbacks executed. |
| `assert_failure_callbacks_ran(names)` | Which failure hooks executed (for failure-path scenarios). |

*Caller boundary & durable row*

| Assertion | Checks |
|---|---|
| `assert_raised(exc_type=None, match=None)` | The last drive propagated an exception to the caller (optionally of a type / containing a substring). |
| `assert_not_raised()` | The last drive propagated nothing (the swallow contract). |
| `assert_error_recorded(obj, contains)` | Substring of `last_error_message` on the latest `TransitionMessage`. |
| `assert_error_count(obj, expected)` | `errors_count` on the latest `TransitionMessage`. |
| `assert_transition_owner(obj, cls, transition_name=None)` | The `owning_process_class` recorded on a `TransitionMessage` (for chained / condition-disambiguated background transitions). |

*The whole journey*

| Assertion | Checks |
|---|---|
| `assert_journey([JourneyStep(...)])` | Each drive's full observable transformation — action, before → after, side-effects, callbacks, and `failed` (an exception reached the caller). Import `JourneyStep` from `django_logic.testing`. |

On failure, every assertion raises with a numbered timeline of each step the
test took and the relevant `TransitionMessage` — built
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
        #
        # NB: patch what the side-effect CALLS, never the side-effect
        # itself — the Transition captured the function object at
        # class-definition time, so patching its module attribute does NOT
        # replace it and the injection would silently never fire.
        # ProcessScenario's fail_side_effect= avoids this footgun entirely.
        order = Order.objects.create(status='approved')
        with patch('myapp.services.courier_client.book', side_effect=CourierError):
            with self.assertRaises(CourierError):
                order.process.fulfil()
        transition_message = TransitionMessage.objects.get(instance_id=str(order.pk))
        self.assertEqual(transition_message.errors_count, 1)
```

Two lower-level helpers mirror production behaviour exactly:

```python
from django_logic.background import retry_pending
retry_pending()   # run every claimable row inline, as a worker pass would (sync mode)

from django_logic.background.runner import run_background_transition
run_background_transition(transition_message.pk)   # one worker attempt for a specific row
```

Use `TransactionTestCase` (or `ProcessScenario`, which extends it) when your
assertions depend on real transaction boundaries — e.g. proving a failed
attempt's writes rolled back.

### An uncompleted row as a fixture

Tests that pin behaviour around the one-uncompleted-row gate need a live
`TransitionMessage`. Do not build the row by hand — one wrong field and the
gate never sees it. The helper writes every field from the instance itself:

```python
from django_logic.testing import open_transition_message

row = open_transition_message(order, 'process', 'fulfil')
with self.assertRaises(TransitionTemporarilyUnavailable):
    order.process.cancel()          # the gate answers "busy"

# An attempt that started an hour ago, for the retry-window branches:
row = open_transition_message(order, 'process', 'fulfil', started_minutes_ago=60)
```

### Which transitions did the suite never drive?

Wrap a block of drives and diff against the declarations:

```python
from django_logic.testing import record_driven_transitions

with record_driven_transitions() as record:
    order.process.fulfil()
    order.process.generate_export()

self.assertEqual(record.undriven(OrderProcess), ['cancel'])
```

A drive counts when the transition ran, including one whose side-effect
failed. A refusal (`TransitionNotAllowed`) does not count. Names are
compared across the whole nested tree, so when nested processes share an
action name, one drive covers the name.

---

## How the library itself is tested

You don't have to take the durability contract on faith — this is the test
pyramid backing it:

1. **Unit + regression suite** (`python tests/manage.py test`, SQLite,
   ~340 tests): every reproduced defect from the 0.3 stability review has a
   permanent regression test — savepoint isolation of side-effects
   (`tests/background/test_savepoint_isolation.py`), the worker's state guard
   (`test_worker_state_guard.py`), the per-process in-flight constraint
   (`test_constraint_per_process.py`), restore verification
   (`test_restore_verification.py`), sync/background mutual exclusion
   (`test_sync_background_mutex.py`), and lock revalidation
   (`tests/test_lock_revalidate.py`). The engine's *behavioural contracts* are
   pinned as journey tests too: the re-raise/swallow asymmetry
   (`tests/test_exception_semantics.py`), the cross-machine failure cascade
   (`tests/test_cross_machine_cascade.py`), and process-level
   conditions/permissions (`tests/test_process_guards.py`) — each verified by
   mutation to fail on the exact regression it names.
2. **PostgreSQL + Redis stability suite** (`make stability-up &&
   make stability-test`, also a GitHub Actions workflow on every PR and
   nightly): real row locking, real concurrent transactions, deadlock and
   crash scenarios under `tests/stability/`.
3. **The Heroku validation matrix** ([django-logic-test](https://github.com/Borderless360/django-logic-test)):
   a deployed harness (RabbitMQ + PostgreSQL + Redis + separate worker/beat
   dynos) running an 18-row matrix on real infrastructure — worker SIGKILL
   mid-task, deploys mid-flight, queue isolation, pgbouncer transaction
   pooling, stuck-row finalization, the timeout kill.

That layering is exactly why your own tests can stop at the process level.
