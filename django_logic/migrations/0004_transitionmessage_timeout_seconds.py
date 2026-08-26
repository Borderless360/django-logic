from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('django_logic_background', '0003_transitionmessage_failure_side_effect_error'),
    ]

    operations = [
        migrations.AddField(
            model_name='transitionmessage',
            name='timeout_seconds',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
