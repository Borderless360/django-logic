class BaseCommand:
    def __init__(self, commands=None):
        self.commands = commands or []

    def execute(self, *args, **kwargs):
        raise NotImplementedError


class CeleryCommand(BaseCommand):
    def execute(self):
        # TODO: https://github.com/Borderless360/django-logic/issues/1
        pass


class Command(BaseCommand):
    def execute(self, instance: any):
        for command in self.commands:
            command(instance)


class Conditions(BaseCommand):
    def execute(self, instance: any):
        return all(command(instance) for command in self.commands)


class Permissions(BaseCommand):
    def execute(self, instance: any, user: any):
        return all(command(instance,  user) for command in self.commands)