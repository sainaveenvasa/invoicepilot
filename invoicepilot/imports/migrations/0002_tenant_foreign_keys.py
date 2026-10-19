from django.db import migrations

from invoicepilot.core.migration_operations import AddTenantForeignKey


class Migration(migrations.Migration):
    dependencies = [
        ("imports", "0001_initial"),
        ("invoices", "0002_tenant_foreign_keys"),
    ]

    operations = [
        AddTenantForeignKey("importrow", "batch"),
        AddTenantForeignKey("importrow", "invoice"),
    ]
