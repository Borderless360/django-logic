import logging

from django_logic.state import State


class BaseCommand(object):
    """
    Implements pattern Command
    """
    def __init__(self, commands=None, transition=None):
        self._commands = commands or []
        self._transition = transition

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
        logging.info(f"{state.instance_key} side-effects of '{self._transition.action_name}' action started")
        try:
            for command in self._commands:
                command(state.instance, **kwargs)
        except Exception as error:
            logging.error(f"{state.instance_key} side-effects of "
                          f"'{self._transition.action_name}' action failed with {error}")
            self._transition.fail_transition(state, error, **kwargs)
        else:
            logging.info(f"{state.instance_key} side-effects of "
                         f"'{self._transition.action_name}' action succeed")
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
            logging.error(f"{state.instance_key} callbacks of "
                          f"'{self._transition.action_name}` action failed with {error}")
