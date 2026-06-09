# Scenario-Based Testing for Document-Driven Development

> Design document for Django Logic's testing framework.
> Built for the document-driven development triangle and AI/vibe-coding workflows.

---

## Table of Contents

1. [The Triangle](#1-the-triangle)
2. [Why Django Logic Is Uniquely Suited](#2-why-django-logic-is-uniquely-suited)
3. [Scenario Tests vs Unit Tests](#3-scenario-tests-vs-unit-tests)
4. [ProcessScenario API](#4-processscenario-api)
5. [Key Design Decisions](#5-key-design-decisions)
6. [AI-Readable Test Output](#6-ai-readable-test-output)
7. [How AI Uses This in Practice](#7-how-ai-uses-this-in-practice)
8. [File Structure](#8-file-structure)
9. [Integration with the Plan](#9-integration-with-the-plan)

---

## 1. The Triangle

Document-driven development with Django Logic follows a triangle:

```
    ┌──────────────────────┐
    │      DOCUMENT         │
    │  (Business Process    │
    │      Spec)            │
    └──────┬───────┬────────┘
           │       │
    AI generates   AI generates
           │       │
           ▼       ▼
┌──────────────┐  ┌──────────────────┐
│IMPLEMENTATION│  │  SCENARIO TESTS   │
│ (Process     │◄─│ (Realistic e2e    │
│  classes,    │  │  flows verifying  │
│  transitions,│  │  implementation   │
│  side effects│  │  matches document)│
│  )           │  │                   │
└──────────────┘  └───────────────────┘
```

The power: AI reads the document, generates BOTH the declarative Process
definition AND the scenario tests. A human reviews whether the scenarios
make sense and whether the Process definition is correct — both are
readable without understanding implementation details.

---

## 2. Why Django Logic Is Uniquely Suited

Django Logic's Process definitions are already declarative and AI-readable:

```python
class OrderProcess(Process):
    transitions = [
        Transition(
            action_name='approve',
            sources=['draft'],
            target='approved',
            conditions=[has_stock],
            permissions=[is_staff],
        ),
        BackgroundTransition(
            action_name='fulfill',
            sources=['approved'],
            target='fulfilled',
            in_progress_state='fulfilling',
            failed_state='fulfillment_failed',
            side_effects=[reserve_stock, generate_labels, call_courier],
            callbacks=[send_confirmation_email],
        ),
        Transition(
            action_name='cancel',
            sources=['draft', 'approved'],
            target='cancelled',
            permissions=[is_staff_or_customer],
        ),
    ]
```

An AI can look at this and understand: "orders go from draft to approved
(if staff + stock), then to fulfilled in background (with 3 side effects
that can fail)." No framework internals to understand. No scattered
decorators. The entire business logic is in one place.

---

## 3. Scenario Tests vs Unit Tests

### Current state (verbose, mechanical, hard for AI to generate correctly)

```python
class TestOrder(TestCase):
    def test_approve(self):
        order = Order.objects.create(status='draft')
        state = State(order, 'status')
        # ... manual setup, mock side effects ...
        order.process.approve(user=staff)
        order.refresh_from_db()
        self.assertEqual(order.status, 'approved')

    def test_approve_no_stock(self):
        order = Order.objects.create(status='draft')
        order.stock_available = False
        order.save()
        with self.assertRaises(TransitionNotAllowed):
            order.process.approve()
```

Problems with unit tests for AI coding:
- Test implementation details, not business behavior
- Require understanding of `State`, `refresh_from_db`, exception types
- Each test is isolated — no sense of the process as a whole
- Hard for AI to generate correctly (easy to forget `refresh_from_db`, etc.)

### Proposed: scenario-based (reads like a business story)

```python
from django_logic.testing import ProcessScenario

class TestOrderFulfillment(ProcessScenario):
    """
    Business Process: Order Fulfillment
    Document: docs/processes/order_fulfillment.md

    Tests the complete order lifecycle from creation through
    fulfillment, including failure and retry scenarios.
    """
    process_class = OrderProcess
    model = Order
    state_field = 'status'

    def test_happy_path(self):
        """Draft -> Approved -> Fulfilling -> Fulfilled"""
        order = self.create_instance(status='draft', client=self.client)
        self.assert_available(order, ['approve', 'cancel'])

        self.transition(order, 'approve', user=self.staff)
        self.assert_state(order, 'approved')

        self.background_transition(order, 'fulfill')
        self.assert_state(order, 'fulfilled')
        self.assert_side_effects_ran(['reserve_stock', 'generate_labels', 'call_courier'])
        self.assert_callbacks_ran(['send_confirmation_email'])

    def test_fulfillment_fails_and_retries(self):
        """Courier API fails, system retries, succeeds on second attempt"""
        order = self.create_instance(status='approved')

        self.background_transition(order, 'fulfill',
            fail_side_effect='call_courier',
            fail_with=ConnectionTimeout("Aramex timeout"))

        self.assert_state(order, 'fulfilling')
        self.assert_error_recorded(order, 'ConnectionTimeout')
        self.assert_side_effects_ran(['reserve_stock', 'generate_labels'])
        self.assert_side_effects_not_ran(['call_courier'])

        self.retry_transition(order)
        self.assert_state(order, 'fulfilled')

    def test_max_retries_exhausted(self):
        """After N failures, order moves to failed state"""
        order = self.create_instance(status='approved')

        for i in range(5):
            self.background_transition(order, 'fulfill',
                fail_side_effect='call_courier',
                fail_with=ConnectionTimeout())
            self.retry_transition(order)

        self.assert_state(order, 'fulfillment_failed')
        self.assert_error_count(order, 5)

    def test_staff_can_approve_customer_cannot(self):
        """Permission check: only staff can approve"""
        order = self.create_instance(status='draft')
        self.assert_available(order, ['approve'], user=self.staff)
        self.assert_not_available(order, ['approve'], user=self.customer)

    def test_cannot_fulfill_without_stock(self):
        """Condition check: stock must be available"""
        order = self.create_instance(status='approved', stock_available=False)
        self.assert_not_available(order, ['fulfill'])

    def test_cancel_from_draft(self):
        """Customer cancels a draft order"""
        order = self.create_instance(status='draft')
        self.transition(order, 'cancel', user=self.customer)
        self.assert_state(order, 'cancelled')

    def test_cancel_from_approved(self):
        """Staff cancels an approved order before fulfillment"""
        order = self.create_instance(status='approved')
        self.transition(order, 'cancel', user=self.staff)
        self.assert_state(order, 'cancelled')

    def test_cannot_cancel_fulfilled_order(self):
        """Fulfilled orders cannot be cancelled"""
        order = self.create_instance(status='fulfilled')
        self.assert_not_available(order, ['cancel'])
```

Advantages for AI coding:
- Tests read like business requirements — each maps 1:1 to a document bullet
- No framework internals (`State`, `refresh_from_db`, exception handling)
- `ProcessScenario` handles all boilerplate (DB refresh, locking, background execution)
- AI-readable failure output tells exactly where the process diverged

---

## 4. ProcessScenario API

### Class attributes

| Attribute | Type | Purpose |
|-----------|------|---------|
| `process_class` | `type[Process]` | The Process class under test |
| `model` | `type[Model]` | The Django model |
| `state_field` | `str` | Name of the state field (default: `'status'`) |
| `process_name` | `str` | Name of the process attribute on the model (default: `'process'`) |

### Instance creation

```python
order = self.create_instance(status='draft', client=self.client, **kwargs)
```

Creates a model instance using `model.objects.create(**kwargs)`. The state
field is set via the `status` keyword (or whatever `state_field` is named).
Users can override `create_instance` for complex setup (factories, related
objects).

### Synchronous transition execution

```python
self.transition(order, 'approve', user=self.staff)
```

Executes a synchronous transition. Internally:
1. Calls `getattr(instance, process_name).action_name(**kwargs)`
2. Refreshes instance from DB
3. Records the transition in the test timeline

### Background transition execution

```python
self.background_transition(order, 'fulfill')
```

Executes a background transition synchronously (no Celery). Internally:
1. Runs phase 1 (lock, set in_progress_state, create TransitionMessage)
2. Runs phase 2 inline (side effects, complete/fail transition)
3. Refreshes instance from DB
4. Records all steps in the test timeline

With failure injection:

```python
self.background_transition(order, 'fulfill',
    fail_side_effect='call_courier',
    fail_with=ConnectionTimeout("Aramex timeout"))
```

Wraps the named side effect to raise the given exception. All other side
effects run normally. The transition follows the failure path.

### Retry

```python
self.retry_transition(order)
```

Finds the uncompleted `TransitionMessage` for this instance and runs the
handler synchronously — simulating what the periodic starter task would do.

### Assertions

**State assertions:**

```python
self.assert_state(order, 'fulfilled')
```

Refreshes from DB and checks the state field value. On failure, shows
current state and available transitions.

**Availability assertions:**

```python
self.assert_available(order, ['approve', 'cancel'])
self.assert_available(order, ['approve'], user=self.staff)
self.assert_not_available(order, ['fulfill'], user=self.customer)
```

Checks which transition action names are available (via
`get_available_actions`). User parameter is optional.

**Side effect tracking assertions:**

```python
self.assert_side_effects_ran(['reserve_stock', 'generate_labels'])
self.assert_side_effects_not_ran(['call_courier'])
self.assert_callbacks_ran(['send_confirmation_email'])
```

Checks which side effects and callbacks were executed during the last
transition. Side effect functions are identified by their `__name__`.

**Error assertions (for background transitions with DB persistence):**

```python
self.assert_error_recorded(order, 'ConnectionTimeout')
self.assert_error_count(order, 5)
```

Checks the `TransitionMessage` for the instance — verifies error message
content and error count.

### State snapshots for bug reproduction

When a bug is found in production, the full state of the object can be
captured as JSON and used to reproduce the issue in a test. This closes
the loop between production debugging and test coverage.

**Capturing a snapshot:**

```python
from django_logic.testing import snapshot

# In a Django shell, admin view, error handler, or logging callback:
json_data = snapshot(order)
# Returns:
# {
#     "model": "order.Order",
#     "pk": 12345,
#     "state_field": "status",
#     "state": "fulfilling",
#     "fields": {
#         "client_id": 42,
#         "total_amount": "199.99",
#         "tracking_number": "",
#         "created_at": "2026-04-10T14:30:00Z",
#         ...
#     },
#     "related": {
#         "fulfilment": { "state": "pending", "manifest_id": null, ... },
#         "order_items": [
#             { "product_id": 7, "quantity": 2, "sku": "WIDGET-01" },
#         ]
#     },
#     "transition_message": {
#         "id": 42,
#         "transition_name": "fulfill",
#         "errors_count": 3,
#         "last_error": "ConnectionTimeout: Aramex API",
#         "kwargs": { "user_id": 5 }
#     },
#     "process": {
#         "class": "order.processes.OrderProcess",
#         "available_actions": [],
#         "is_locked": true
#     }
# }
```

The snapshot captures everything needed to reproduce the state: the model
fields, related objects, the TransitionMessage (if any), and the process
status. Copy this JSON from a log, Sentry, admin panel, or Django shell.

**Reproducing in a test:**

```python
from django_logic.testing import ProcessScenario

class TestBug12345(ProcessScenario):
    """
    Bug: Order #12345 stuck in 'fulfilling' after Aramex timeout.
    Snapshot taken from production on 2026-04-10.
    """
    process_class = OrderProcess
    model = Order
    state_field = 'status'

    def test_reproduce_and_fix(self):
        # Restore the exact state from production
        order = self.from_snapshot('fixtures/bug_12345.json')
        self.assert_state(order, 'fulfilling')
        self.assert_error_count(order, 3)

        # Verify the fix: retry should now succeed
        self.retry_transition(order)
        self.assert_state(order, 'fulfilled')
```

**`from_snapshot(path_or_dict)`** creates the model instance and all
related objects from the JSON snapshot. It also restores the
TransitionMessage if present, so `retry_transition()` can pick it up.

**Automatic snapshot on failure (opt-in):**

```python
class TestOrderFulfillment(ProcessScenario):
    snapshot_on_failure = True  # dump state JSON when any test fails
    ...
```

When enabled, every failed test assertion automatically captures a
snapshot of the instance at that moment and includes it in the test
output:

```
FAILED: test_happy_path

  Timeline:
    [1] create_instance(status='draft')     -> OK
    [2] transition('approve', user=staff)   -> OK, status: draft -> approved
    [3] background_transition('fulfill')    -> FAILED

  Snapshot (copy to reproduce):
    {"model": "order.Order", "pk": 1, "state": "fulfilling", ...}
```

An AI or developer copies this JSON, creates a test with
`self.from_snapshot(...)`, reproduces the bug, fixes it, and the test
stays as a regression guard.

**How this fits the AI workflow:**

1. Bug reported or detected in production
2. Support/monitoring captures snapshot JSON (from logs, Sentry, admin)
3. Developer pastes snapshot to AI: "this order is stuck, fix it"
4. AI creates a test using `from_snapshot()`, reproduces the bug, identifies
   the root cause, fixes the code, and the test proves it works
5. The snapshot test stays in the test suite as a regression guard

---

## 5. Key Design Decisions

### 5.1 ProcessScenario is a base class, not a mixin

A dedicated base class inheriting from `TransactionTestCase`. This ensures
background transitions with DB persistence (TransitionMessage + atomic
blocks) work correctly. Users extend it directly.

### 5.2 background_transition() runs both phases synchronously

No Celery needed in tests. This follows the pattern GV already uses in
production tests:

```python
def _run_background_transition_sync(self, state, **kwargs):
    task_kwargs = self.get_task_kwargs(state, **kwargs)
    run_transition_in_background(**task_kwargs)
```

The framework patches `BackgroundTransition.run_in_background` during
test execution to run phase 2 inline instead of dispatching to Celery.

### 5.3 Side effect tracking, not mocking

Instead of patching/mocking side effects, the framework wraps them during
test execution to track what ran. The actual side effects execute — you
get real behavior. The framework just records which functions were called.

For failure simulation, only the targeted side effect is wrapped to raise.
All others run normally. This means you test the real failure path: earlier
side effects succeed, the failing one raises, `fail_transition` runs.

### 5.4 retry_transition() simulates the periodic starter

Finds the uncompleted `TransitionMessage` for the instance and runs the
handler synchronously — exactly what `handle_transition_messages_starter`
would do, but inline. This lets you test the full retry cycle without
Celery Beat or waiting.

### 5.5 Test timeline for AI-readable output

Every action (`create_instance`, `transition`, `background_transition`,
`retry_transition`) is recorded in a timeline. On test failure, the
timeline is printed as structured output that AI can parse and reason about.

---

## 6. AI-Readable Test Output

When a scenario test fails, the output reads like a story:

```
FAILED: TestOrderFulfillment.test_happy_path

  Timeline:
    [1] create_instance(status='draft')         -> OK, Order(id=1, status='draft')
    [2] assert_available(['approve', 'cancel'])  -> OK
    [3] transition('approve', user=staff)        -> OK, status: draft -> approved
    [4] background_transition('fulfill')         -> FAILED
        Phase 1: OK, status: approved -> fulfilling
        Phase 2: side_effect 'reserve_stock'     -> OK (0.02s)
                 side_effect 'generate_labels'   -> FAILED: LabelServiceError("API down")
                 side_effect 'call_courier'      -> SKIPPED (previous failed)

  Expected: assert_state(order, 'fulfilled')
  Actual:   order.status = 'fulfilling'

  TransitionMessage:
    errors_count: 1
    last_error: LabelServiceError: API down
```

This structured output is critical for AI debugging — it reads the timeline
and understands exactly where the process diverged from expectations,
without needing to read stack traces or Django internals.

---

## 7. How AI Uses This in Practice

### The workflow

**Step 1: Human writes a business process document**

```markdown
## Order Fulfillment Process

- Orders start in 'draft'
- Staff approves orders (checks stock availability)
- Approved orders are fulfilled in background
- Fulfillment: reserve stock, generate shipping labels, call courier API
- If courier fails, retry up to 5 times
- After fulfillment, send confirmation email
- Staff or customer can cancel draft/approved orders
```

**Step 2: AI generates the Process class**

Straightforward because the Process definition is declarative and maps 1:1
to the document. The AI produces a `Process` subclass with `Transition`
and `BackgroundTransition` entries, condition functions, permission
functions, and side effect function stubs.

**Step 3: AI generates scenario tests**

Each requirement in the document becomes one or more scenarios:

| Document requirement | Test scenario |
|---------------------|---------------|
| "Staff approves orders" | `test_staff_can_approve_customer_cannot` |
| "checks stock availability" | `test_cannot_fulfill_without_stock` |
| "fulfilled in background" | `test_happy_path` (includes background) |
| "If courier fails, retry up to 5 times" | `test_fulfillment_fails_and_retries` + `test_max_retries_exhausted` |
| "send confirmation email" | `test_happy_path` (asserts callback) |
| "cancel draft/approved orders" | `test_cancel_from_draft` + `test_cancel_from_approved` + `test_cannot_cancel_fulfilled_order` |

**Step 4: Human reviews**

The Process definition and scenarios are both readable without understanding
Django internals. The human checks:
- Does the Process match the document?
- Do the scenarios cover all requirements?
- Do the scenario names and docstrings make business sense?

**Step 5: Tests run**

If they pass, the implementation matches the document. If they fail, the
AI-readable output tells exactly where the process diverged.

### Why this is better than unit tests for AI coding

- Unit tests test implementation details. Scenario tests test business
  behavior.
- AI can generate both Process classes AND scenario tests from the same
  document.
- Scenarios serve as executable documentation — a new developer (or AI)
  reads them and understands the business process.
- When requirements change, update the document and regenerate — scenarios
  show exactly what broke.

---

## 8. File Structure

```
django_logic/
  testing/
    __init__.py          # exports ProcessScenario, snapshot, etc.
    scenario.py          # ProcessScenario base class
    tracking.py          # Side effect/callback execution tracker
    runner.py            # Synchronous background transition runner
    assertions.py        # Custom assertions (assert_state, assert_available, etc.)
    snapshot.py          # State capture (snapshot) and restore (from_snapshot)
    output.py            # AI-readable failure output formatter
```

### Module responsibilities

**`scenario.py`** — `ProcessScenario` base class with `create_instance`,
`from_snapshot`, `transition`, `background_transition`, `retry_transition`
methods. Manages the test timeline and orchestrates tracking + runner.

**`tracking.py`** — Wraps side effects and callbacks during test execution
to record which functions ran, their arguments, return values, and any
exceptions. Provides the data for `assert_side_effects_ran` etc.

**`runner.py`** — `run_background_transition_sync()` and
`run_pending_transitions()` — executes background transitions and
TransitionMessage handlers without Celery.

**`assertions.py`** — Custom assertion methods: `assert_state`,
`assert_available`, `assert_not_available`, `assert_side_effects_ran`,
`assert_side_effects_not_ran`, `assert_callbacks_ran`,
`assert_error_recorded`, `assert_error_count`.

**`snapshot.py`** — `snapshot(instance)` captures the full state of a model
instance as JSON (fields, related objects, TransitionMessage, process
status). `from_snapshot(data)` restores an instance from a snapshot for
test reproduction. Also provides `snapshot_on_failure` hook for automatic
capture when tests fail.

**`output.py`** — Formats the test timeline into the structured AI-readable
output shown in section 6. Hooks into test failure reporting. When
`snapshot_on_failure` is enabled, appends the snapshot JSON to the output.

---

## 9. Integration with the Plan

### Built during Stage 2/3 (BackgroundTransition)

Internal helpers needed for the framework's own tests:
- `runner.py` — `run_background_transition_sync()` (we need this to test
  BackgroundTransition without Celery)
- `tracking.py` — side effect execution tracking (we need this to verify
  side effects ran correctly in our own tests)

### Shipped in Stage 5 (v1.0.0 — Developer Experience)

Public API for users:
- `ProcessScenario` base class
- All assertion methods
- AI-readable output formatter
- Documentation: "Testing Your Processes" guide in the docs site

### Documentation site page (Stage 5)

The docs site should include a dedicated **"Testing Your Processes"** guide
that teaches the document-driven triangle approach with a complete example:

1. Start with a business process document
2. Show the generated Process class
3. Show the generated scenario tests
4. Show the test output (passing and failing)
5. Show how to iterate when requirements change
