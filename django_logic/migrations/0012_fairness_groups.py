from django.db import migrations, models


def create_existing_groups(apps, schema_editor):
    messages = apps.get_model('django_logic_background', 'TransitionMessage')
    groups = apps.get_model('django_logic_background', 'FairnessGroup')
    alias = schema_editor.connection.alias
    keys = messages.objects.using(alias).filter(is_completed=False).order_by(
    ).values_list('fairness_key', flat=True).distinct()
    batch = []
    for key in keys.iterator(chunk_size=1000):
        batch.append(groups(key=key))
        if len(batch) == 1000:
            groups.objects.using(alias).bulk_create(batch, ignore_conflicts=True)
            batch = []
    if batch:
        groups.objects.using(alias).bulk_create(batch, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [('django_logic_background', '0011_operation_contract')]

    operations = [
        migrations.CreateModel(
            name='FairnessGroup',
            fields=[
                ('key', models.CharField(blank=True, max_length=255, primary_key=True, serialize=False)),
                ('last_served_at', models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.AddIndex(
            model_name='transitionmessage',
            index=models.Index(fields=['fairness_key', 'created'], condition=models.Q(is_completed=False), name='dl_bg_fair_ready_idx'),
        ),
        migrations.RunPython(create_existing_groups, migrations.RunPython.noop),
    ]
