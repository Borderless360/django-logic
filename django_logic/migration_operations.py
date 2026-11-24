"""Build PostgreSQL indexes while application writes continue."""
from django.db.migrations.operations.models import AddIndex


class AddIndexConcurrentlyOnPostgreSQL(AddIndex):
    atomic = False

    def _database_operation(self, schema_editor):
        operation_class = AddIndex
        if schema_editor.connection.vendor == 'postgresql':
            # SQLite installations do not need a PostgreSQL driver.
            from django.contrib.postgres.operations import AddIndexConcurrently
            operation_class = AddIndexConcurrently
        return operation_class(model_name=self.model_name, index=self.index)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        self._database_operation(schema_editor).database_forwards(
            app_label, schema_editor, from_state, to_state,
        )

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        self._database_operation(schema_editor).database_backwards(
            app_label, schema_editor, from_state, to_state,
        )
