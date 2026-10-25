from django.db import migrations

from apps.core.migration_operations import AddTenantForeignKey


class Migration(migrations.Migration):
    dependencies = [
        ("reminders", "0001_initial"),
        ("invoices", "0002_tenant_foreign_keys"),
    ]

    operations = [
        AddTenantForeignKey("reminderstep", "policy"),
        AddTenantForeignKey("reminderstep", "template"),
        AddTenantForeignKey("reminderdelivery", "invoice"),
        AddTenantForeignKey("reminderdelivery", "step"),
        AddTenantForeignKey("reminderdelivery", "sender_identity"),
        AddTenantForeignKey("reminderdeliveryattempt", "delivery"),
    ]
