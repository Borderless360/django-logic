from .process import Process, ProcessManager
from .transition import Transition

#: The public surface of the top-level package. Both sibling public packages
#: (``django_logic.background``, ``django_logic.testing``) define one; without
#: it here ``from django_logic import *`` leaked the submodules that happened
#: to be imported, so the star-import namespace varied with INSTALLED_APPS.
#: State, exceptions, background and testing are imported from their own
#: modules by design — see the README. The command classes live in
#: ``django_logic.commands``: consumers declare lists of functions, and a
#: bundle subclass imports its base from there.
__all__ = [
    'Process',
    'ProcessManager',
    'Transition',
]


def __getattr__(name):
    if name == 'Action':
        raise ImportError(
            "Action was removed in 1.0.0. Declare "
            "Transition(action_name=..., sources=[...]) with no target — "
            "it writes no state on success, and unlike Action it takes "
            "the state lock, is refused while a background transition is "
            "uncompleted (TransitionTemporarilyUnavailable), and runs "
            "next_transition. A side-effect that must not obey that "
            "contract belongs in a plain method, not in the process."
        )
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
