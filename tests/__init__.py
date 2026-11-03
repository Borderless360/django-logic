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


def unbind_model_process(model, process_class=None, state_field=None):
    """Remove test bindings without deleting application-owned accessors."""
    from django_logic.process import ProcessManager, _ProcessAccessor

    removed = [binding for binding in ProcessManager.bindings
               if binding.model is model
               and (process_class is None or binding.process_class is process_class)
               and (state_field is None or binding.state_field == state_field)]
    ProcessManager.bindings = [binding for binding in ProcessManager.bindings if binding not in removed]
    remaining = {binding.process_class.process_name for binding in ProcessManager.bindings if binding.model is model}
    for binding in removed:
        name = binding.process_class.process_name
        if name not in remaining and isinstance(vars(model).get(name), _ProcessAccessor):
            delattr(model, name)
