"""Test-suite helpers.

``dl_settings`` exists because ~26 test modules used to hand-copy the whole
six-key ``DJANGO_LOGIC`` dict just to change one key, so adding a setting meant
editing all of them.
"""


def dl_settings(**overrides):
    """The active ``DJANGO_LOGIC`` settings with ``overrides`` applied.

    Use with ``@override_settings(DJANGO_LOGIC=dl_settings(KEY=value))``.
    Passing no overrides yields the settings module's own values, i.e. a no-op
    override — so don't.
    """
    from django.conf import settings

    return {**settings.DJANGO_LOGIC, **overrides}
