from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('django_logic_background', '0022_failed_job_index')]

    operations = [
        migrations.DeleteModel(name='StateLock'),
        migrations.DeleteModel(name='JobProcessOwner'),
    ]
