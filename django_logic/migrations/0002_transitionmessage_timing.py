from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('django_logic_background', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='transitionmessage',
            name='started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='transitionmessage',
            name='completed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='transitionmessage',
            name='duration_ms',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='transitionmessage',
            index=models.Index(
                fields=['is_completed', 'started_at'],
                name='dl_bg_started_idx',
            ),
        ),
    ]
