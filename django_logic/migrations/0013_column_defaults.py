from django.db import migrations

# Columns that 0011 adds. A database that applied the published 3.0.0 copy of
# 0011 has them without a database default, because Django dropped the default
# after adding each column. A producer on an earlier version then failed on
# NOT NULL. 0011 now keeps the defaults; this migration restores them where
# 0011 already ran.
COLUMNS = ['request_fingerprint', 'fairness_key', 'deferral_reason', 'completion_kind', 'required_next']


def restore_database_defaults(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != 'postgresql':
        return
    model = apps.get_model('django_logic_background', 'TransitionMessage')
    table = schema_editor.quote_name(model._meta.db_table)
    for column in COLUMNS:
        schema_editor.execute(f"ALTER TABLE {table} ALTER COLUMN {schema_editor.quote_name(column)} SET DEFAULT ''")


class Migration(migrations.Migration):
    dependencies = [('django_logic_background', '0012_fairness_groups')]

    operations = [
        migrations.RunPython(restore_database_defaults, migrations.RunPython.noop),
    ]
