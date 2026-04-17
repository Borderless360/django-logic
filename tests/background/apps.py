from django.apps import AppConfig


class BackgroundTestsConfig(AppConfig):
    name = 'tests.background'
    label = 'bg_tests'
    default_auto_field = 'django.db.models.BigAutoField'
