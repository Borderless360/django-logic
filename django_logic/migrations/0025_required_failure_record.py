from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('django_logic_background', '0024_job_state_result')]

    operations = [
        migrations.AddField(
            model_name='transitionmessage', name='failure_pending',
            field=models.BooleanField(default=False, db_default=False),
        ),
        migrations.AddField(
            model_name='transitionmessage', name='failure_types',
            field=models.JSONField(default=list, blank=True, db_default=[]),
        ),
    ]
