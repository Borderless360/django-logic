from model_utils import Choices

from demo.conditions import is_lock_available
from django_logic import Process, Transition

LOCK_STATES = Choices(
    ('maintenance', 'Under maintenance'),
    ('locked', 'Locked'),
    ('open', 'Open'),
)


class UserLockerProcess(Process):
    def is_user(self, user):
        return not user.is_staff

    permissions = [is_user]
    transitions = [
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
    def is_staff(self, user):
        return user.is_staff

    permissions = [is_staff]
    all_states = [s for s in LOCK_STATES]
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
            target=LOCK_STATES.maintenance
        )
    ]


class LockerProcess(Process):
    states = LOCK_STATES
    conditions = [is_lock_available]

    nested_processes = [
        UserLockerProcess,
        StaffLockerProcess,
    ]
