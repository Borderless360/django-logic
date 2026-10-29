from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('django_logic_background', '0010_proxy_model_label')]

    operations = [
        migrations.AddField('transitionmessage', 'request_key', models.CharField(max_length=255, null=True, blank=True, unique=True)),
        migrations.AddField('transitionmessage', 'request_fingerprint', models.CharField(max_length=64, blank=True, default='', db_default='')),
        migrations.AddField('transitionmessage', 'fairness_key', models.CharField(max_length=255, blank=True, default='', db_default='', db_index=True)),
        migrations.AddField('transitionmessage', 'eligible_at', models.DateTimeField(null=True, blank=True, db_index=True)),
        migrations.AddField('transitionmessage', 'deferral_reason', models.TextField(blank=True, default='', db_default='')),
        migrations.AddField('transitionmessage', 'completion_kind', models.CharField(max_length=16, blank=True, default='', db_default='')),
        migrations.AddField('transitionmessage', 'required_next', models.CharField(max_length=100, blank=True, default='', db_default='')),
    ]
