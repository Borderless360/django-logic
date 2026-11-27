from django.apps import AppConfig


class DjangoLogicConfig(AppConfig):
    """The one installed app: ``INSTALLED_APPS = ['django_logic']``.

    The label ``django_logic_background`` identifies the job table,
    migration records, and content types.

    """
    name = 'django_logic'
    label = 'django_logic_background'
    verbose_name = 'Django Logic'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self) -> None:
        # Imported for its side effect: the module registers the system checks.
        from django_logic import checks
        from django_logic.conf import validate_on_ready

        validate_on_ready()
