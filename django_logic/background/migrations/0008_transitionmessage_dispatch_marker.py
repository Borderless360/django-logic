from django.db import migrations, models


class Migration(migrations.Migration):
    """Nullable/defaulted columns — instant on PostgreSQL. Of the three,
    only ``ended_in_failure`` is still live: migration 0009 removed
    ``last_dispatched_at`` and ``dispatch_count`` with the broker-era
    dispatch marker. The file name predates that removal; renaming an
    applied migration would ghost it in every consumer's
    ``django_migrations``, so the rename waits for the 1.0 squash.

    No new index: every read of ``ended_in_failure`` goes through the
    uncompleted set (small by design, already indexed) or the cleanup
    sweep's full scan."""

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
