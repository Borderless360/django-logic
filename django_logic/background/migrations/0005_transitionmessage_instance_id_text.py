from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('django_logic_background', '0004_transitionmessage_timeout_seconds'),
    ]

    operations = [
        # Widen instance_id from PositiveIntegerField to CharField so the
        # background path supports BigAutoField PKs beyond 2**31-1, UUID
        # primary keys, and CharField primary keys — everything the
        # synchronous core (which uses instance.pk) already supports.
        # Postgres casts existing integer values to text in place.
        migrations.AlterField(
            model_name='transitionmessage',
            name='instance_id',
            field=models.CharField(max_length=255),
        ),
    ]
