# Refused actions

`TransitionNotAllowed.reason` is a `RefusalReason` from `django_logic.exceptions`.
Its values are strings and can be included in JSON responses. Process calls
also attach `user_message`, ready to display. `str(error)` contains diagnostic detail.

| Reason value | What refused the call |
| --- | --- |
| `permission` | A process or transition permission check failed. |
| `condition` | A process or transition condition check failed. |
| `source_state` | The current or persisted state is outside the transition's sources. |
| `unknown_action` | No process declaration has this action name. |
| `locked` | The state lock could not be acquired. |
| `background_in_flight` | An uncompleted background transition still has retry coverage. |
| `background_stranded` | An uncompleted background transition is no longer being retried. |
| `ambiguous` | More than one transition passed the checks for this action. |

A PostgreSQL row-lock timeout raises `TransitionTemporarilyUnavailable` with reason `locked`.
A cache state-lock refusal raises plain `TransitionNotAllowed` with that same reason.
Use the exception type, not `reason='locked'` alone, to decide whether to suggest a short retry.
When declarations share an action name, prefer a transition whose sources
include the current state and whose process permissions and conditions pass.
Its failed check supplies the reason. Otherwise, use a process guard that
blocks a declaration for the current state. If all declarations require
another source state, report `source_state`.

Declaration order resolves ties within each group. A custom check that
supplies no reason keeps `None`, even if a lower-priority refusal supplies
a reason. This rule does not infer a unique intended branch or list every
failed check. Process guards still run before their transitions and nested
processes.

The resolver captures reasons during the checks. It does not run
permissions or conditions again to explain a refusal.
Call `get_available_actions()` explicitly when the caller needs a fresh action list.

A custom `is_valid` override can return `False` without explaining why.
Its reason is `None` unless a stock check on that same object recorded a
failure. Caller-created exceptions also default to `None`. Continue to catch
`TransitionNotAllowed` when handling these refusals.

### Refusal messages

Process calls provide generic messages, such as
`Action 'submit' is not allowed: this record does not meet the requirements for this action.`
Pending background work uses `is temporarily unavailable` in the prefix.
An unknown reason code or a custom check with no reason uses
`Action 'submit' is not allowed`.

Use `Process.refusal_messages` for process-wide wording and the same option
on a transition for its wording. These mappings contain overrides only.
Use `@refusal_message(text)` for one condition or permission callable:

```python
from django_logic import Process, Transition
from django_logic.commands import refusal_message
from django_logic.exceptions import RefusalReason, TransitionNotAllowed


@refusal_message("Upload an invoice before submitting.")
def has_invoice(claim):
    return claim.invoices.exists()


class ClaimProcess(Process):
    refusal_messages = {
        RefusalReason.PERMISSION: "Only the claim owner can perform this action.",
    }
    transitions = [
        Transition(
            "submit", sources=["draft"], target="submitted",
            conditions=[has_invoice],
            refusal_messages={
                RefusalReason.SOURCE_STATE: "This claim has already been submitted.",
            },
        ),
    ]


try:
    claim.process.submit(user=request.user)
except TransitionNotAllowed as error:
    response = {"reason": error.reason, "message": error.user_message}
```

Values are literal explanation text. The library adds the action prefix;
it does not substitute templates or call message functions. The decorator
returns the same callable with the same signature and behavior.

The first failed callable's message takes precedence over the transition
map, then the selected nested process map, the root process map, and built-in
defaults. A nested process with no override keeps the root's wording.
A process-level failure uses process messages because no transition was
selected. Only checks already evaluated can supply a message.

Message capture does not repeat checks or change short-circuit behavior.
Nested refusals keep an already supplied `user_message`. Technical exceptions
retain their normal behavior. Direct `Transition.change_state()` calls can
leave `user_message=None`; process configuration applies to Process calls.


## Checking eligibility while another action runs

`process.get_available_actions(..., include_locked=True)` and
`process.get_available_transitions(..., include_locked=True)` check the object's
current state, conditions and permissions even when another action holds its
state lock. This lets an application distinguish a temporary wait from a business
rule that forbids an action. The default continues to hide actions that need a
held lock.

Inside `DatabaseTransition`, the lock is visible only within the current call.
Other connections can list those actions, but calls still wait for the PostgreSQL row lock or refuse after its timeout.

These queries do not acquire or release locks, save a request, or guarantee that
an action can run immediately. Calling the action still acquires its normal
locks, checks the latest persisted state and rejects conflicting unfinished
work. Applications must handle a later refusal without damaging existing data.
