from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('django_logic_background', '0006_per_process_constraint_and_field_name'),
    ]

    operations = [
        # Record the (possibly nested) Process class that DECLARES the
        # transition, so phase-2 restore can pick the exact background
        # transition when an action_name is shared across
        # condition-disambiguated nested processes. Blank on rows created
        # before this field existed (and whenever the transition lives on the
        # bound process itself) — phase 2 then falls back to first-match by
        # transition_name, which the old validator guaranteed was unique.
        migrations.AddField(
            model_name='transitionmessage',
            name='owning_process_class',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
