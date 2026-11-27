import django.db.models.deletion
from django.db import migrations, models


def check_unfinished_results(apps, schema_editor):
    results = apps.get_model('django_logic_background', 'JobStateResult')
    if results.objects.using(schema_editor.connection.alias).filter(job__is_completed=False).exists():
        raise RuntimeError('Finish jobs with a saved state decision before downgrading Django Logic.')


class Migration(migrations.Migration):
    dependencies = [('django_logic_background', '0023_remove_state_lock_ownership')]

    operations = [
        migrations.CreateModel(
            name='JobStateResult',
            fields=[
                ('job', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name='+',
                    serialize=False, to='django_logic_background.transitionmessage',
                )),
                ('value', models.TextField()),
            ],
        ),
        migrations.RunPython(migrations.RunPython.noop, check_unfinished_results),
    ]
