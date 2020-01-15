class BaseCommand(object):
    """
    Command
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
    def execute(self, instance: any, **kwargs):
        """
        It checks every condition for the provided instance by executing every command
        :param instance: any
        :return: True or False
        """
        return all(command(instance, **kwargs) for command in self._commands)


class Permissions(BaseCommand):
    def execute(self, instance: any, user: any, **kwargs):
        """
        It checks the permissions for the provided user and instance by executing evey command
        If user is None then permissions passed
        :param instance: any
        :param user: any or None
        :return: True or False
        """
        return user is None or all(command(instance,  user, **kwargs) for command in self._commands)


class SideEffects(BaseCommand):
    def execute(self, instance: any, field_name: str, **kwargs):
        try:
            for command in self._commands:
                command(instance, **kwargs)
        except Exception:
            self._transition.fail_transition(instance, field_name, **kwargs)
        else:
            self._transition.complete_transition(instance, field_name, **kwargs)


class Callbacks(BaseCommand):
    def execute(self, instance: any, field_name: str, **kwargs):
        try:
            for command in self.commands:
                command(instance, **kwargs)
        except Exception:
            # TODO: handle exception
            pass