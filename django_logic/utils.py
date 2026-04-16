import importlib

from django.apps import apps
from django.contrib.auth import get_user_model


def restore_user_object(kwargs):
    """Restore user object from user_id in kwargs."""
    if user_id := kwargs.get('user_id'):
        kwargs['user'] = get_user_model().objects.get(id=user_id)


def get_process_instance(instance, process_name, process_class=None, field_name='status'):
    """Get process instance from model or process_class."""
    try:
        return getattr(instance, process_name)
    except AttributeError:
        if process_class:
            module_path, class_name = process_class.rsplit('.', 1)
            module = importlib.import_module(module_path)
            process_class_obj = getattr(module, class_name)
            return process_class_obj(field_name=field_name, instance=instance)
        raise AttributeError(
            f"'{instance.__class__.__name__}' object has no attribute '{process_name}' "
            f"and no process_class was provided"
        )


def get_process_and_state(app_label, model_name, instance_id, process_name,
                         process_class=None, field_name='status'):
    """Load instance and process from serialized kwargs; return (process, state)."""
    app = apps.get_app_config(app_label)
    model = app.get_model(model_name)
    instance = model.objects.get(pk=instance_id)
    process = get_process_instance(instance, process_name, process_class, field_name)
    return process, process.state


def restore_action(
    app_label, model_name, instance_id, field_name, 
    process_class, action_name, user
):
    """Restore action from serialized kwargs."""
    # Instance
    app = apps.get_app_config(app_label)
    model = app.get_model(model_name)
    instance = model.objects.get(pk=instance_id)
    # Process
    module_path, class_name = process_class.rsplit('.', 1)
    module = importlib.import_module(module_path)
    process_class_obj = getattr(module, class_name)
    process =process_class_obj(field_name=field_name, instance=instance)

    transition = process.get_transition_by_action_name(action_name=action_name, user=user, ignore_sources=True)
    return process, transition
