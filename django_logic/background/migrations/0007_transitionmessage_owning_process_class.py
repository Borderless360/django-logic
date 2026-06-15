from django.db import migrations, models


class Migration(migrations.Migration):
    """Add the phase-2 owner discriminator (issue #98).

    Records the (possibly nested) Process class that DECLARES the transition, so
    phase-2 restore can pick the exact background transition when an action_name
    is shared across condition-disambiguated nested processes. Blank on rows
    created before this field existed — phase 2 then resolves by transition_name
    only when that name is unambiguous across the tree (see runner._find_transition).

    Deploy note (PostgreSQL): this AddField does NOT rewrite the table (a
    constant default on PG 11+ is metadata-only), but ADD COLUMN still takes a
    brief ACCESS EXCLUSIVE lock on ``transitionmessage`` — the engine's hottest
    table (phase 1 inserts into it; phase 2 holds rows under
    ``select_for_update(nowait=True)``). If a worker is holding a row lock in a
    long-open transaction, the ALTER queues at the head of the lock queue and
    briefly blocks other access. django-logic transactions are short by design,
    so this is normally a sub-second blip; to be safe, run ``migrate`` with a
    short ``lock_timeout`` (e.g. ``SET lock_timeout = '2s'``) and retry, ideally
    during a low-throughput window. See README "Deployment" / CHANGELOG.
    """

    dependencies = [
        ('django_logic_background', '0006_per_process_constraint_and_field_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='transitionmessage',
            name='owning_process_class',
            field=models.TextField(blank=True, default=''),
        ),
    ]
