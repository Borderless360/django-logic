from django.db import migrations


class Migration(migrations.Migration):
    """Pull workers claim rows from the database, so nothing is published
    and there is no publish to mark or count."""

    dependencies = [
        ('django_logic_background', '0008_transitionmessage_dispatch_marker'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='transitionmessage',
            name='last_dispatched_at',
        ),
        migrations.RemoveField(
            model_name='transitionmessage',
            name='dispatch_count',
        ),
    ]
