from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('django_logic_background', '0002_transitionmessage_timing'),
    ]

    operations = [
        migrations.AddField(
            model_name='transitionmessage',
            name='failure_side_effect_error',
            field=models.TextField(blank=True),
        ),
    ]
