# Testing Guide — how to test django-logic processes

> Two rules this whole guide follows:
>
> 1. **You test your process, not the background machinery.** Delivery,
>    retries, durability, crash recovery and queue routing are the library's
>    job — guaranteed by its own regression suite, a PostgreSQL + Redis
>    stability suite, and a production-style Heroku validation matrix (see
>    [How the library itself is tested](#how-the-library-itself-is-tested)).
>    Your tests run the *business process* **entirely without Celery**.
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
  against the definition instead of asserting *availability behaviour* (a
  blocked-by-condition or permission-denied journey — see scenarios
  [2](#2-condition-gating), [3](#3-permission-gating),
  [14](#14-conditions-in-the-blocking-direction));
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
def setUp(self):
    super().setUp()
    # User fixtures are the test author's responsibility — ProcessScenario
    # does not create any.
    self.staff = User.objects.create(username='staff', is_staff=True)
    self.customer = User.objects.create(username='customer')

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

### 4. Synchronous failure → failed_state + failure hooks + re-raise

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
    # self.assert_failure_side_effects_ran(['void_reservation'])
    # self.assert_failure_callbacks_ran(['notify_ops'])
```

`expect_raises` is what makes this a journey test rather than a mirror:
without it the harness absorbs the injected exception, so the test would pass
whether the engine re-raised or silently swallowed. See
[scenario 16](#16-the-caller-boundary-re-raise-vs-swallow) for the full
re-raise/swallow contract.

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

(Assumes a process with `Transition('pay', sources=['pending'], target='paid', next_transition='process')` and `Transition('process', sources=['paid'], target='processing')` — chains aren't part of the running example above.)

```python
def test_pay_chains_into_processing(self):
    order = self.create_instance(status='pending')
    self.transition(order, 'pay')
    self.assert_state(order, 'processing')        # follow-up ran after unlock
```

Tracking covers the whole process tree, so the follow-up's side-effects are
visible to `assert_side_effects_ran` even though you only drove `'pay'`.

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

### 14. Conditions in the *blocking* direction

Most condition tests only prove a transition fires when the condition passes.
A condition that silently always-returned-`True` would pass every one of those.
Pin the negative too — the transition must be **refused** when the condition
does not hold, with the object unchanged:

```python
def test_partial_fulfilment_cannot_be_marked_fulfilled(self):
    order = self.create_instance(status='fulfilling')
    order.lines.create(status='fulfilled')
    order.lines.create(status='pending')          # not all lines done

    # Unavailable...
    self.assert_not_available(order, ['mark_fulfilled'])
    # ...and refused if forced, with no state change.
    self.transition(order, 'mark_fulfilled',
                    expect_raises=TransitionNotAllowed)
    self.assert_state(order, 'fulfilling')
```

### 15. Asserting the domain outcome (not just that a hook ran)

`assert_side_effects_ran` proves the *wiring*; to pin the *outcome* — what the
object became — capture a baseline and assert the delta. This is the assertion
that fails if a side-effect is called but does the wrong thing.

```python
def test_approve_normalises_and_stamps_the_order(self):
    order = self.create_instance(status='draft')
    before = self.capture(order, ['status', 'total', 'approved_at'])

    self.transition(order, 'approve', user=self.staff)

    self.assert_side_effects_ran(['validate_order'])       # wiring
    self.assert_changed(order, before, {                   # outcome
        'status': ('draft', 'approved'),
        'approved_at': (None, order.approved_at),           # now set
    })
    self.assert_unchanged(order, before, ['total'])         # must NOT move
```

For a hook whose whole job is a *related-row* effect (a `delete_*` callback, a
side-effect that generates N records), assert the row delta directly:

```python
def test_cancel_deletes_reservations(self):
    order = self.create_instance(status='approved')
    order.reservations.create(); order.reservations.create()

    self.transition(order, 'cancel')

    self.assert_callbacks_ran(['release_reservations'])     # wiring
    self.assert_related_count(order.reservations.all(), 0)  # outcome
```

### 16. The caller boundary: re-raise vs swallow

The engine treats the four hook families asymmetrically at the *caller
boundary*, and that asymmetry is exactly what the `0.1.6 → 0.2.0` regression
flipped. Pin which one each transition relies on:

| Hook | On failure | Assert with |
|---|---|---|
| `side_effects` | runs `fail_transition`, then **re-raises** | `expect_raises=Err` / `assert_raised(Err)` |
| `callbacks` | **swallowed** (best-effort) | `expect_raises=False` / `assert_not_raised()` |
| `next_transition` follow-up | follow-up failure **swallowed** | `expect_raises=False` |
| `failure_side_effects` | **swallowed**, does not mask the original | `expect_raises=OriginalErr` |

```python
def test_side_effect_failure_reaches_the_caller(self):
    order = self.create_instance(status='approved')
    self.background_transition(order, 'fulfil',
                               fail_side_effect='call_courier',
                               fail_with=ConnectionError('down'),
                               expect_raises=ConnectionError)
    self.assert_raised(ConnectionError, match='down')

def test_callback_failure_is_swallowed_and_target_kept(self):
    order = self.create_instance(status='approved')
    self.transition(order, 'confirm',
                    fail_side_effect='send_receipt_email',   # a callback hook
                    fail_with=SMTPError(),
                    expect_raises=False)                     # must NOT propagate
    self.assert_not_raised()
    self.assert_state(order, 'confirmed')                    # target survives
```

`expect_raises` accepts an exception type (or tuple) to assert it propagated,
or `False` to assert nothing did. Leave it out (the legacy default) only when
you are asserting on the *recorded* error of a background failure rather than
the caller boundary.

### 17. Pinning the whole journey in one assertion

`assert_state_trace` pins the ordered states the object passed through;
`assert_journey` pins each drive's full observable transformation — action,
before → after, side-effects, callbacks, and whether it reached the caller.

```python
from django_logic.testing import JourneyStep

def test_fulfilment_journey(self):
    order = self.create_instance(status='approved')
    self.background_transition(order, 'fulfil')

    # Every state the object moved through (in-progress -> target, chains, …):
    self.assert_state_trace(['fulfilling', 'fulfilled'])

    # The whole end-to-end story as one assertion:
    self.assert_journey([
        JourneyStep(action='fulfil', before='approved', after='fulfilled',
                    side_effects=['reserve_stock', 'call_courier'],
                    callbacks=['send_confirmation_email'],
                    failed=False),
    ])
```

`JourneyStep.failed` means *an exception propagated to the caller* — so a
failure-path `assert_journey(..., failed=True)` alone detects a
swallow-vs-reraise flip.

### 18. The cross-machine cascade (anti-pattern contract)

If a side-effect drives a transition on *another* instance and lets its
exception propagate (the "fundamental problem" anti-pattern — see
[docs/recipes/nested-processes.md](recipes/nested-processes.md) for the correct
fan-out alternative), that behaviour is worth pinning as one journey so a
refactor can't silently change it: the inner machine lands in its
`failed_state` with its failure hooks run, the outer's later side-effects are
skipped, and the exception reaches the caller. See
`tests/test_cross_machine_cascade.py` for a worked contract test.

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

`transition`, `background_transition` and `retry_transition` all accept:

- `fail_side_effect='name'` + `fail_with=SomeError(...)` — only the named
  side-effect is wrapped to raise; everything else runs for real. Any *other*
  (unexpected) exception fails the test loudly.
- `expect_raises=` — pin the caller boundary. An **exception type** (or tuple)
  asserts it propagated to the caller (the `SideEffects` re-raise contract);
  **`False`** asserts nothing propagated (the swallow contract). Omitted (the
  legacy default), an injected failure is absorbed so you can assert on the
  *recorded* error instead. See [scenario 16](#16-the-caller-boundary-re-raise-vs-swallow).

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
| `assert_failure_side_effects_ran(names)` / `assert_failure_callbacks_ran(names)` | Which failure hooks executed (for failure-path scenarios). |

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
   ~340 tests): every reproduced defect from the 0.3 stability review has a
   permanent regression test — savepoint isolation of side-effects
   (`tests/background/test_savepoint_isolation.py`), the phase-2 state guard
   (`test_phase2_state_guard.py`), the per-process in-flight constraint
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
   pooling, stuck-row finalization, timeout watchdogs.

That layering is exactly why your own tests can stop at the process level.
