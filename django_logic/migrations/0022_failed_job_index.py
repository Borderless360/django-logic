from django.db import migrations, models

from django_logic.migration_operations import AddIndexConcurrentlyOnPostgreSQL


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('django_logic_background', '0021_completed_job_cleanup_index'),
    ]

    operations = [
        AddIndexConcurrentlyOnPostgreSQL(
            model_name='transitionmessage',
            index=models.Index(
                fields=['app_label', 'model_name', 'instance_id', 'process_name', '-completed_at', '-id'],
                condition=models.Q(is_completed=True, ended_in_failure=True),
                name='dl_bg_failure_idx',
            ),
        ),
    ]
