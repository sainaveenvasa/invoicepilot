from django.db import migrations

from apps.core.migration_operations import AddTenantForeignKey


class Migration(migrations.Migration):
    dependencies = [
        ("invoices", "0001_initial"),
        ("customers", "0001_initial"),
    ]

    operations = [
        AddTenantForeignKey("invoice", "customer"),
        AddTenantForeignKey("paymentrecord", "invoice"),
    ]
