class BaseCommand(object):
    """
    Command descriptor
    """
    def __set__(self, instance, value):
        self._commands = value

    @property
    def commands(self):
        return self._commands

    def execute(self, *args, **kwargs):
        raise NotImplementedError


class Conditions(BaseCommand):
    def __init__(self, commands=None):
        self._commands = commands or []

    def execute(self, instance: any):
        return all(command(instance) for command in self.commands)


class Permissions(BaseCommand):
    def __init__(self, commands=None):
        self._commands = commands or []

    def execute(self, instance: any, user: any):
        return all(command(instance,  user) for command in self.commands)


class TransitionCommandDescriptor(object):
    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, transition, owner):
        self.transition = transition
        return self

    def __set__(self, transition, commands):
        transition.__dict__[self.name] = commands

    @property
    def commands(self):
        return self.transition.__dict__[self.name]


class SideEffects(TransitionCommandDescriptor, BaseCommand):
    def execute(self, instance: any, field_name):
        try:
            for command in self.commands:
                command(instance)
        except Exception:
            # TODO: handle exception
            self.transition.fail_transition(instance, field_name)
        else:
            self.transition.complete_transition(instance, field_name)


class Callbacks(TransitionCommandDescriptor, BaseCommand):
    def execute(self, instance, field_name):
        try:
            for command in self.commands:
                command(instance)
        except Exception:
            # TODO: handle exception
            pass
