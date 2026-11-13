"""Require current saved-job identity before removing obsolete columns."""
from importlib import import_module

from django.apps import apps as installed_apps
from django.db import migrations


def _check_identity(job):
    from django_logic.process import Process

    path = job.kwargs.get('process_class') if isinstance(job.kwargs, dict) else None
    if not isinstance(path, str) or not path or not job.owning_process_class or not job.field_name:
        raise ValueError('process_class, owning_process_class and field_name are required')
    module_name, class_name = path.rsplit('.', 1)
    root = getattr(import_module(module_name), class_name)
    if not isinstance(root, type) or not issubclass(root, Process):
        raise ValueError('process_class does not name a Process class')
    if f'{root.__module__}.{root.__name__}' != path:
        raise ValueError('process_class names an alias')
    if root.process_name != job.process_name:
        raise ValueError('process_name no longer matches process_class')
    model_label = job.proxy_model_label or f'{job.app_label}.{job.model_name}'
    model = installed_apps.get_model(model_label)
    concrete = model._meta.concrete_model._meta
    if (job.app_label, job.model_name) != (concrete.app_label, concrete.model_name):
        raise ValueError('the saved model key must name the concrete model')
    model._meta.get_field(job.field_name)
    action = job.parent_transition_name if job.effect_name else job.transition_name
    pending, seen = [root], set()
    while pending:
        owner = pending.pop()
        if owner in seen:
            continue
        seen.add(owner)
        pending.extend(owner.nested_processes)
        if f'{owner.__module__}.{owner.__name__}' != job.owning_process_class:
            continue
        for transition in owner.transitions:
            if transition.action_name != action or not transition.is_background:
                continue
            names = [job.effect_name] if job.effect_name else job.background_effect_names
            declared = {effect.name for effect in transition.background_effects}
            if any(name not in declared for name in names):
                raise ValueError('a recorded background effect is no longer declared')
            return
    raise ValueError('the recorded owner no longer declares this background action')


def _lock_table(schema_editor, table):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute("SET LOCAL lock_timeout = '5s'")
        schema_editor.execute(f'LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE')


def remove_columns(apps, schema_editor):
    jobs = apps.get_model('django_logic_background', 'TransitionMessage')
    table = schema_editor.quote_name(jobs._meta.db_table)
    _lock_table(schema_editor, table)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f'SELECT id FROM {table} WHERE NOT is_completed AND required_next <> %s LIMIT 1', [''],
        )
        required_next = cursor.fetchone()
    if required_next:
        raise RuntimeError(
            f'Django Logic 5.0 upgrade refused: unfinished job #{required_next[0]} has required_next. '
            'Finish or explicitly resolve this job using 4.3.2 before upgrading. No jobs were changed.'
        )
    for job in jobs.objects.using(schema_editor.connection.alias).filter(is_completed=False).iterator(chunk_size=1000):
        try:
            _check_identity(job)
        except Exception as error:
            raise RuntimeError(
                f'Django Logic 5.0 upgrade refused: unfinished job #{job.pk}: {error}. '
                'Finish or explicitly resolve this job using 4.3.2 before upgrading. No jobs were changed.'
            ) from error
    for column in ('request_fingerprint', 'required_next'):
        schema_editor.execute(f'ALTER TABLE {table} DROP COLUMN {schema_editor.quote_name(column)}')


def restore_columns(apps, schema_editor):
    jobs = apps.get_model('django_logic_background', 'TransitionMessage')
    table = schema_editor.quote_name(jobs._meta.db_table)
    _lock_table(schema_editor, table)
    for column, length in (('request_fingerprint', 64), ('required_next', 100)):
        schema_editor.execute(
            f'ALTER TABLE {table} ADD COLUMN {schema_editor.quote_name(column)} '
            f"varchar({length}) NOT NULL DEFAULT ''",
        )


class Migration(migrations.Migration):
    dependencies = [('django_logic_background', '0016_remove_unused_operation_fields')]
    operations = [migrations.RunPython(remove_columns, restore_columns)]
