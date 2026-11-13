"""Migration operations that tolerate a schema that is already there.

Two sales migrations were renumbered when branches were merged (0006/0007 became
0007/0008). A database that had applied them under the old names already holds
their columns and tables, so Django, seeing new names, tried to add them again
and stopped ("column ... already exists") - and every migration queued behind
them never ran. Wrapping an operation in IfMissing keeps its effect on Django's
model state but skips the SQL when the column or table is already present.
"""
from django.db import migrations
from django.db.migrations.operations.base import Operation


class IfMissing(Operation):
    reversible = True

    def __init__(self, operation):
        self.operation = operation

    def deconstruct(self):
        return (self.__class__.__qualname__, [self.operation], {})

    def state_forwards(self, app_label, state):
        self.operation.state_forwards(app_label, state)

    def describe(self):
        return f"{self.operation.describe()} (if missing)"

    def _present(self, app_label, schema_editor, to_state):
        op = self.operation
        conn = schema_editor.connection
        with conn.cursor() as cursor:
            tables = conn.introspection.table_names(cursor)
            if isinstance(op, migrations.CreateModel):
                model = to_state.apps.get_model(app_label, op.name)
                return model._meta.db_table in tables
            if isinstance(op, migrations.AddField):
                model = to_state.apps.get_model(app_label, op.model_name)
                table = model._meta.db_table
                if table not in tables:
                    return False
                column = model._meta.get_field(op.name).column
                return column in {c.name for c in conn.introspection.get_table_description(cursor, table)}
        return False

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if self._present(app_label, schema_editor, to_state):
            return
        self.operation.database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        self.operation.database_backwards(app_label, schema_editor, from_state, to_state)
