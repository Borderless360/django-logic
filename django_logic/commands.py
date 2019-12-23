class BaseCommand(object):
    """
    Command descriptor
    """
    def __init__(self, commands=None):
        self._commands = commands or []
        self.name = None

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, instance, owner):
        self.transition = instance
        return self

    def __set__(self, instance, commands):
        instance.__dict__[self.name] = commands

    @property
    def commands(self):
        return self.transition.__dict__[self.name] if self.name is not None else self._commands

    def execute(self, *args, **kwargs):
        raise NotImplementedError


class Conditions(BaseCommand):
    def execute(self, instance: any, **kwargs):
        return all(command(instance, **kwargs) for command in self.commands)


class Permissions(BaseCommand):
    def execute(self, instance: any, user: any, **kwargs):
        return all(command(instance,  user, **kwargs) for command in self.commands)


class SideEffects(BaseCommand):
    def execute(self, instance: any, field_name: str, **kwargs):
        try:
            for command in self.commands:
                command(instance, **kwargs)
        except Exception:
            # TODO: handle exception
            self.transition.fail_transition(instance, field_name)
        else:
            self.transition.complete_transition(instance, field_name, **kwargs)


class Callbacks(BaseCommand):
    def execute(self, instance: any, field_name: str, **kwargs):
        try:
            for command in self.commands:
                command(instance, **kwargs)
        except Exception:
            # TODO: handle exception
            pass
