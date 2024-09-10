from demo.conditions import is_user, is_staff, is_planned, is_lock_available
from demo.models import Lock, LOCK_STATES
from django_logic import Process, Transition, Action, ProcessManager


class UserLockerProcess(Process):
    permissions = [is_user]
    transitions = [
        Action(
            action_name='refresh',
            sources=[LOCK_STATES.open, LOCK_STATES.locked]
        ),
        Transition(
            action_name='lock',
            sources=[LOCK_STATES.open],
            target=LOCK_STATES.locked
        ),
        Transition(
            action_name='unlock',
            sources=[LOCK_STATES.locked],
            target=LOCK_STATES.open
        )
    ]


class StaffLockerProcess(Process):
    permissions = [is_staff]
    all_states = [x for x, y in LOCK_STATES]

    transitions = [
        Transition(
            action_name='lock',
            sources=[LOCK_STATES.open, LOCK_STATES.maintenance],
            target=LOCK_STATES.locked
        ),
        Transition(
            action_name='unlock',
            sources=[LOCK_STATES.locked, LOCK_STATES.maintenance],
            target=LOCK_STATES.open
        ),
        Transition(
            action_name='maintain',
            sources=all_states,
            target=LOCK_STATES.maintenance,
            conditions=[is_planned]
        )
    ]


class LockerProcess(Process):
    states = LOCK_STATES

    conditions = [is_lock_available]

    nested_processes = [
        StaffLockerProcess,
        UserLockerProcess,
    ]


ProcessManager.bind_model_process(Lock, LockerProcess, 'status')
