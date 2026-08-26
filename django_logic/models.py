"""The app's models module.

``TransitionMessage`` is defined in ``django_logic.background.models`` and
declares ``app_label = 'django_logic_background'`` — the label is the
address of the live table, its migration records and its content types,
so it never changes. Importing it here registers it when the app loads.
"""
from django_logic.background.models import TransitionMessage  # noqa: F401
