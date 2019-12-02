class BaseTransitionCommand(object):
    """
    Command descriptor
    """
    def __init__(self, commands=None, transition=None, *args, **kwargs):
        self.commands = commands or []
        self.transition = transition

    def __get__(self, transition, owner):
        if self.transition is None:
            self.transition = transition
        return self

    def __set__(self, transition, commands):
        if self.transition is None:
            self.transition = transition
        self.commands = commands

    def __delete__(self, instance):
        del self.commands

    def execute(self, *args, **kwargs):
        raise NotImplementedError


class SideEffects(BaseTransitionCommand):
    def execute(self, instance: any, field_name):
        try:
            for command in self.commands:
                command(instance)
        except Exception:
            self.transition.fail_transition(instance, field_name)
        else:
            self.transition.complete_transition(instance, field_name)


class Callbacks(BaseTransitionCommand):
    def execute(self, instance, field_name):
        try:
            for command in self.commands:
                command(instance)
        except Exception:
            # TODO: logger
            pass


class Conditions(BaseTransitionCommand):
    def execute(self, instance: any):
        return all(command(instance) for command in self.commands)


class Permissions(BaseTransitionCommand):
    def execute(self, instance: any, user: any):
        return all(command(instance,  user) for command in self.commands)
