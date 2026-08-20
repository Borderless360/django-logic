from django.db import migrations, models


class Migration(migrations.Migration):
    """Three nullable/defaulted columns — instant on PostgreSQL. No new
    index: every read of these columns goes through the uncompleted set
    (small by design, already indexed) or the cleanup sweep's full scan."""

    dependencies = [
        ('django_logic_background', '0007_transitionmessage_owning_process_class'),
    ]

    operations = [
        migrations.AddField(
            model_name='transitionmessage',
            name='last_dispatched_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='transitionmessage',
            name='dispatch_count',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='transitionmessage',
            name='ended_in_failure',
            field=models.BooleanField(default=False),
        ),
    ]
