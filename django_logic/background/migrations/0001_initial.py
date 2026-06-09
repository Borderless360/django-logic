import django.utils.timezone
import model_utils.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies: list = []

    operations = [
        migrations.CreateModel(
            name='TransitionMessage',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True, serialize=False, verbose_name='ID',
                )),
                ('created', model_utils.fields.AutoCreatedField(
                    default=django.utils.timezone.now, editable=False, verbose_name='created',
                )),
                ('modified', model_utils.fields.AutoLastModifiedField(
                    default=django.utils.timezone.now, editable=False, verbose_name='modified',
                )),
                ('is_completed', models.BooleanField(default=False)),
                ('errors_count', models.PositiveIntegerField(default=0)),
                ('last_error_dt', models.DateTimeField(blank=True, null=True)),
                ('last_error_message', models.TextField(blank=True)),
                ('app_label', models.CharField(max_length=100)),
                ('model_name', models.CharField(max_length=100)),
                ('instance_id', models.PositiveIntegerField()),
                ('process_name', models.CharField(max_length=100)),
                ('transition_name', models.CharField(max_length=100)),
                ('queue_name', models.CharField(max_length=100)),
                ('kwargs', models.JSONField(blank=True, default=dict)),
            ],
            options={
                'indexes': [
                    models.Index(
                        fields=['is_completed', 'created'],
                        name='dl_bg_incomplete_idx',
                    ),
                    models.Index(
                        fields=['app_label', 'model_name', 'instance_id'],
                        name='dl_bg_instance_idx',
                    ),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        condition=models.Q(('is_completed', False)),
                        fields=('app_label', 'model_name', 'instance_id'),
                        name='dl_bg_only_one_uncompleted_per_instance',
                    ),
                ],
            },
        ),
    ]
