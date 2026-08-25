"""One migration for a fresh install, replacing 0001 through 0010.

A fresh install applies only this file. An install that has applied any
of the ten originals keeps using them; Django runs the unapplied ones
and records this file as applied. The originals stay in the package
until every install has applied this file. The data rewrite in 0010 is
not repeated here: a fresh install has no rows to rewrite.
"""
import django.utils.timezone
import model_utils.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    replaces = [('django_logic_background', '0001_initial'), ('django_logic_background', '0002_transitionmessage_timing'), ('django_logic_background', '0003_transitionmessage_failure_side_effect_error'), ('django_logic_background', '0004_transitionmessage_timeout_seconds'), ('django_logic_background', '0005_transitionmessage_instance_id_text'), ('django_logic_background', '0006_per_process_constraint_and_field_name'), ('django_logic_background', '0007_transitionmessage_owning_process_class'), ('django_logic_background', '0008_transitionmessage_dispatch_marker'), ('django_logic_background', '0009_remove_dispatch_marker'), ('django_logic_background', '0010_proxy_model_label')]

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='TransitionMessage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('is_completed', models.BooleanField(default=False)),
                ('errors_count', models.PositiveIntegerField(default=0)),
                ('last_error_dt', models.DateTimeField(blank=True, null=True)),
                ('last_error_message', models.TextField(blank=True)),
                ('app_label', models.CharField(max_length=100)),
                ('model_name', models.CharField(max_length=100)),
                ('instance_id', models.CharField(max_length=255)),
                ('process_name', models.CharField(max_length=100)),
                ('transition_name', models.CharField(max_length=100)),
                ('queue_name', models.CharField(max_length=100)),
                ('kwargs', models.JSONField(blank=True, default=dict)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('duration_ms', models.PositiveIntegerField(blank=True, null=True)),
                ('failure_side_effect_error', models.TextField(blank=True)),
                ('timeout_seconds', models.PositiveIntegerField(blank=True, null=True)),
                ('field_name', models.CharField(blank=True, default='', max_length=100)),
                ('owning_process_class', models.TextField(blank=True, default='')),
                ('ended_in_failure', models.BooleanField(default=False)),
                ('proxy_model_label', models.CharField(blank=True, default='', max_length=201)),
            ],
            options={
                'indexes': [models.Index(fields=['is_completed', 'created'], name='dl_bg_incomplete_idx'), models.Index(fields=['app_label', 'model_name', 'instance_id'], name='dl_bg_instance_idx'), models.Index(fields=['is_completed', 'started_at'], name='dl_bg_started_idx')],
                'constraints': [models.UniqueConstraint(condition=models.Q(('is_completed', False)), fields=('app_label', 'model_name', 'instance_id', 'process_name'), name='dl_bg_one_uncompleted_per_process')],
            },
        ),
    ]
