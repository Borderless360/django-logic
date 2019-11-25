class BaseCommand:
    def __init__(self, commands=None):
        self.commands = commands or []

    def execute(self):
        raise NotImplementedError


class CeleryCommand(BaseCommand):
    def execute(self):
        # TODO: https://github.com/Borderless360/django-logic/issues/1
        pass


class Command(BaseCommand):
    def execute(self):
        for command in self.commands:
            command()


class Conditions(BaseCommand):
    # TODO: support hints

    def execute(self):
        return all(command() for command in self.commands)

