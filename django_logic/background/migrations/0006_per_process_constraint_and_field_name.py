from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('django_logic_background', '0005_transitionmessage_instance_id_text'),
    ]

    operations = [
        # Record the state field the process is bound to, so phase 2 can
        # reconstruct the process from the stored process_class without
        # guessing the field name. Blank on pre-0.4 rows.
        migrations.AddField(
            model_name='transitionmessage',
            name='field_name',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        # Scope the in-flight concurrency guard per process: two processes
        # bound to different state fields of the same model are independent
        # state machines and may both have background work in flight.
        migrations.RemoveConstraint(
            model_name='transitionmessage',
            name='dl_bg_only_one_uncompleted_per_instance',
        ),
        migrations.AddConstraint(
            model_name='transitionmessage',
            constraint=models.UniqueConstraint(
                condition=models.Q(('is_completed', False)),
                fields=('app_label', 'model_name', 'instance_id', 'process_name'),
                name='dl_bg_one_uncompleted_per_process',
            ),
        ),
    ]
