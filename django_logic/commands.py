class BaseCommand:
    def __init__(self, commands=None):
        self.commands = commands or []

    def execute(self):
        raise NotImplementedError


class CeleryCommand(BaseCommand):
    def execute(self):
        # TODO: wrap all commands into celery task with asck late = True
        # TODO: execute them in a queue
        pass


class Command(BaseCommand):
    def execute(self):
        for command in self.commands:
            command()


class Conditions(BaseCommand):
    # TODO: support hints

    def execute(self):
        return all(command() for command in self.commands)

