"""Keep old producer columns while running processes move to the new version."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('django_logic_background', '0015_background_effects')]
    operations = [
        migrations.SeparateDatabaseAndState(state_operations=[
            migrations.RemoveField('transitionmessage', 'request_fingerprint'),
            migrations.RemoveField('transitionmessage', 'required_next'),
        ]),
    ]
