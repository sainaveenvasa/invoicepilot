from django.db import migrations

# Row-level UPDATE/DELETE on audit events is rejected. The retention job is the
# only sanctioned deleter: it runs `SET LOCAL invoicepilot.audit_purge = 'on'`
# inside its own transaction first. Phase 2.4 additionally revokes UPDATE and
# DELETE on this table from the application's database role.
FORWARD = """
CREATE FUNCTION audit_auditevent_reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('invoicepilot.audit_purge', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit_auditevent is append-only: % rejected', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER audit_auditevent_append_only
    BEFORE UPDATE OR DELETE ON audit_auditevent
    FOR EACH ROW EXECUTE FUNCTION audit_auditevent_reject_mutation();
"""

BACKWARD = """
DROP TRIGGER audit_auditevent_append_only ON audit_auditevent;
DROP FUNCTION audit_auditevent_reject_mutation();
"""


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial")]

    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
