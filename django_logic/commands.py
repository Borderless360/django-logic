from django_logic.constants import LogType
from django_logic.logger import get_logger
from django_logic.state import State


class BaseCommand(object):
    """
    Implements pattern Command
    """
    def __init__(self, commands=None, transition=None):
        self._commands = commands or []
        self._transition = transition
        self.logger = get_logger(module_name=__name__)

    @property
    def commands(self):
        return self._commands

    def execute(self, *args, **kwargs):
        raise NotImplementedError


class Conditions(BaseCommand):
    def execute(self, state: State, **kwargs):
        """
        It checks every condition for the provided instance by executing every command
        :param state: State object
        :return: True or False
        """
        return all(command(state.instance, **kwargs) for command in self._commands)


class Permissions(BaseCommand):
    def execute(self, state: State, user: any, **kwargs):
        """
        It checks the permissions for the provided user and instance by executing evey command
        If user is None then permissions passed
        :param state: State object
        :param user: any or None
        :return: True or False
        """
        return user is None or all(command(state.instance,  user, **kwargs) for command in self._commands)


class SideEffects(BaseCommand):
    def execute(self, state: State, **kwargs):
        """Side-effects execution"""
        self.logger.info(f"{state.instance_key} side effects of '{self._transition.action_name}' started",
                         log_type=LogType.TRANSITION_DEBUG,
                         log_data=state.get_log_data())
        try:
            for command in self._commands:
                command(state.instance, **kwargs)
        except Exception as error:
            self.logger.info(f"{state.instance_key} side effects of '{self._transition.action_name}' failed "
                             f"with {error}",
                             log_type=LogType.TRANSITION_DEBUG,
                             log_data=state.get_log_data())
            self.logger.error(error, log_type=LogType.TRANSITION_ERROR, log_data=state.get_log_data())
            self._transition.fail_transition(state, error, **kwargs)
        else:
            self.logger.info(f"{state.instance_key} side-effects of '{self._transition.action_name}' succeeded",
                             log_type=LogType.TRANSITION_DEBUG,
                             log_data=state.get_log_data())
            self._transition.complete_transition(state, **kwargs)


class Callbacks(BaseCommand):
    def execute(self, state: State, **kwargs):
        """
        Callback execution method.
        It runs commands one by one, if any of them raises an exception
        it will stop execution and send a message to logger.
        Please note, it doesn't run failure callbacks in case of exception.
        """
        try:
            for command in self.commands:
                command(state.instance, **kwargs)
        except Exception as error:
            self.logger.info(f"{state.instance_key} callbacks of '{self._transition.action_name}` failed with {error}",
                             log_type=LogType.TRANSITION_DEBUG,
                             log_data=state.get_log_data())
            self.logger.error(error, log_type=LogType.TRANSITION_ERROR, log_data=state.get_log_data())


class NextTransition(object):
    """
    Runs next transition if it is specified
    """
    _next_transition: str

    def __init__(self, next_transition: str = None):
        self._next_transition = next_transition

    def execute(self, state: State, **kwargs):
        if not self._next_transition:
            return

        process = getattr(state.instance, state.process_name)
        transitions = list(process.get_available_transitions(action_name=self._next_transition,
                                                             user=kwargs.get('user', None)))
        if not transitions:
            return None

        transition = transitions[0]
        transition.change_state(state, **kwargs)
