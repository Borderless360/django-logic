from django.apps import AppConfig


class DjangoLogicConfig(AppConfig):
    """The one installed app: ``INSTALLED_APPS = ['django_logic']``.

    The label stays ``django_logic_background``: the label is the address
    of the live ``TransitionMessage`` table, of its rows in
    ``django_migrations`` and of its content types, so an install that
    upgrades from the two-entry era keeps its data without a migration.

    A consumer that never declares a background transition needs nothing
    beyond this entry: the pull-mode database and cache rules are system
    checks that fire only when a background transition is bound.
    """
    name = 'django_logic'
    label = 'django_logic_background'
    verbose_name = 'Django Logic'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self) -> None:
        from django_logic import checks  # noqa: F401 — registers system checks
        from django_logic.background.apps import validate_on_ready

        validate_on_ready()
