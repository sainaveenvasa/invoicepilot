from django.db.migrations.operations.base import Operation


class AddTenantForeignKey(Operation):
    """Add FOREIGN KEY (organization_id, <field>_id) -> parent (organization_id, id).

    Django's own single-column FK only proves the parent row exists; this
    composite key also proves it belongs to the same workspace (BR-017). The
    parent must declare core.models.tenant_key(). A nullable FK column is
    skipped by PostgreSQL's MATCH SIMPLE semantics, so optional links work.

    The constraint is DEFERRABLE INITIALLY DEFERRED to match Django's own FKs,
    so cascade deletes and fixture loading behave the same way.
    """

    reversible = True
    reduces_to_sql = True

    def __init__(self, model_name, field_name):
        self.model_name = model_name
        self.field_name = field_name

    def deconstruct(self):
        return (self.__class__.__name__, [], {"model_name": self.model_name, "field_name": self.field_name})

    def state_forwards(self, app_label, state):
        pass

    def _parts(self, app_label, state):
        model = state.apps.get_model(app_label, self.model_name)
        field = model._meta.get_field(self.field_name)
        table = model._meta.db_table
        return model, field, table, f"{table}_{self.field_name}_tenant_fk"

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        _, field, table, name = self._parts(app_label, to_state)
        qn = schema_editor.quote_name
        schema_editor.execute(
            f"ALTER TABLE {qn(table)} ADD CONSTRAINT {qn(name)} "
            f"FOREIGN KEY ({qn('organization_id')}, {qn(field.column)}) "
            f"REFERENCES {qn(field.related_model._meta.db_table)} ({qn('organization_id')}, {qn('id')}) "
            "DEFERRABLE INITIALLY DEFERRED"
        )

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        _, _, table, name = self._parts(app_label, from_state)
        qn = schema_editor.quote_name
        schema_editor.execute(f"ALTER TABLE {qn(table)} DROP CONSTRAINT {qn(name)}")

    def describe(self):
        return f"Add tenant-scoped composite FK on {self.model_name}.{self.field_name}"

    @property
    def migration_name_fragment(self):
        return f"{self.model_name}_{self.field_name}_tenant_fk"
