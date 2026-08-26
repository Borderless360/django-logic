from django.db import migrations, models


def normalize_uncompleted_proxy_rows(apps_registry, schema_editor):
    """Rewrite uncompleted rows keyed on a proxy's name to the concrete key.

    The key columns now name the concrete model, and the recording proxy
    class moves to ``proxy_model_label``. Rows written before this release
    carry the proxy's own name in ``model_name``, so the uncompleted-row
    guard and the ``in_flight`` probes would stop seeing them.

    Only the live app registry knows which recorded names are proxies —
    the consumer's proxy classes are not in this migration's historical
    state — so the lookup goes through it. A name the live registry cannot
    resolve is left as it is; restore handles it the same either way.
    """
    from django.apps import apps as live_apps

    TransitionMessage = apps_registry.get_model(
        'django_logic_background', 'TransitionMessage')
    for row in TransitionMessage.objects.filter(is_completed=False).iterator():
        try:
            model = live_apps.get_model(row.app_label, row.model_name)
        except LookupError:
            continue
        if not model._meta.proxy:
            continue
        concrete = model._meta.concrete_model._meta
        clash = TransitionMessage.objects.filter(
            app_label=concrete.app_label,
            model_name=concrete.model_name,
            instance_id=row.instance_id,
            process_name=row.process_name,
            is_completed=False,
        ).exclude(pk=row.pk).exists()
        if clash:
            # Two uncompleted rows for one physical row — the defect this
            # release fixes. Rewriting both would violate the unique
            # constraint. The extra row keeps its proxy key; the worker
            # still claims and completes it, it just stays outside the
            # guard until it completes.
            continue
        # .update(), not .save(): the auto-set 'modified' field must not
        # move, or the retry-window classification would read every old
        # row as freshly active.
        TransitionMessage.objects.filter(pk=row.pk).update(
            proxy_model_label=f'{row.app_label}.{row.model_name}',
            app_label=concrete.app_label,
            model_name=concrete.model_name,
        )


def restore_proxy_keys(apps_registry, schema_editor):
    """Put the recording class back into the key columns, so code from
    before this release reads its rows again after an unapply. Runs
    before the column drops. The same clash rule as the forward pass:
    an uncompleted row whose old key another uncompleted row now holds
    keeps its concrete key."""
    TransitionMessage = apps_registry.get_model(
        'django_logic_background', 'TransitionMessage')
    labeled = TransitionMessage.objects.exclude(proxy_model_label='')
    for row in labeled.iterator():
        app_label, _, model_name = row.proxy_model_label.partition('.')
        if not row.is_completed:
            clash = TransitionMessage.objects.filter(
                app_label=app_label,
                model_name=model_name,
                instance_id=row.instance_id,
                process_name=row.process_name,
                is_completed=False,
            ).exclude(pk=row.pk).exists()
            if clash:
                continue
        TransitionMessage.objects.filter(pk=row.pk).update(
            app_label=app_label,
            model_name=model_name,
            proxy_model_label='',
        )


class Migration(migrations.Migration):

    dependencies = [
        ('django_logic_background', '0009_remove_dispatch_marker'),
    ]

    operations = [
        # The key columns (app_label, model_name, instance_id) now name the
        # concrete model, so a proxy and the model it proxies collide in
        # the uncompleted-row constraint. This column records the proxy
        # class that recorded the row, so the worker restores that class.
        migrations.AddField(
            model_name='transitionmessage',
            name='proxy_model_label',
            field=models.CharField(blank=True, default='', max_length=201),
        ),
        # Elidable: a fresh install has no rows to rewrite.
        migrations.RunPython(
            normalize_uncompleted_proxy_rows,
            restore_proxy_keys,
            elidable=True,
        ),
    ]
