from django.db import migrations, models

from django_logic.migration_operations import AddIndexConcurrentlyOnPostgreSQL


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('django_logic_background', '0020_state_lock_ownership'),
    ]

    operations = [
        AddIndexConcurrentlyOnPostgreSQL(
            model_name='transitionmessage',
            index=models.Index(
                fields=['modified', 'id'],
                condition=models.Q(is_completed=True, ended_in_failure=False),
                name='dl_bg_cleanup_age_idx',
            ),
        ),
    ]
