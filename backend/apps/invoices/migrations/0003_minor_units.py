"""Money moves from NUMERIC(14,2) to BIGINT minor units (database design §7, decision P1).

Existing rows are converted exactly: 1250.50 USD becomes 125050 and 120000
JPY stays 120000. The migration refuses to run if a JPY amount has a
fractional part, rather than rounding money. It is reversible.

Also adds Invoice.version (§11.6), the "api" source, and new CHECKs.
"""

import django.core.validators
import django.db.models.functions.text
import django.db.models.lookups
from django.db import migrations, models

# Frozen here, not imported: a migration must keep meaning what it meant when
# it was written. Every other supported currency has two decimals.
ZERO_DECIMAL_CURRENCIES = ("JPY",)
_ZERO = ", ".join(f"'{code}'" for code in ZERO_DECIMAL_CURRENCIES)


def _scale(currency_column: str) -> str:
    return f"power(10::numeric, CASE WHEN {currency_column} IN ({_ZERO}) THEN 0 ELSE 2 END)"


FORWARD = f"""
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM invoices_invoice
        WHERE currency IN ({_ZERO})
          AND (total_amount <> trunc(total_amount) OR balance_due <> trunc(balance_due))
    ) OR EXISTS (
        SELECT 1 FROM invoices_paymentrecord p JOIN invoices_invoice i ON i.id = p.invoice_id
        WHERE i.currency IN ({_ZERO}) AND p.amount <> trunc(p.amount)
    ) THEN
        RAISE EXCEPTION 'Fractional amounts on zero-decimal invoices; correct them before converting to minor units';
    END IF;
END $$;

UPDATE invoices_invoice
   SET total_amount_minor = total_amount * {_scale("currency")},
       balance_due_minor = balance_due * {_scale("currency")};

UPDATE invoices_paymentrecord p
   SET amount_minor = p.amount * {_scale("i.currency")}
  FROM invoices_invoice i
 WHERE i.id = p.invoice_id;
"""

BACKWARD = f"""
UPDATE invoices_invoice
   SET total_amount = total_amount_minor / {_scale("currency")},
       balance_due = balance_due_minor / {_scale("currency")};

UPDATE invoices_paymentrecord p
   SET amount = p.amount_minor / {_scale("i.currency")}
  FROM invoices_invoice i
 WHERE i.id = p.invoice_id;
"""

MAX_MINOR = 99_999_999_999_999


class Migration(migrations.Migration):
    dependencies = [
        ("invoices", "0002_tenant_foreign_keys"),
    ]

    operations = [
        # 1. CHECKs that name the old columns go first.
        migrations.RemoveConstraint(model_name="invoice", name="invoices_invoice_total_positive"),
        migrations.RemoveConstraint(model_name="invoice", name="invoices_invoice_balance_in_range"),
        migrations.RemoveConstraint(model_name="invoice", name="invoices_invoice_status_consistent"),
        migrations.RemoveConstraint(model_name="paymentrecord", name="invoices_payment_amount_positive"),
        # 2. New columns, nullable until they are filled.
        migrations.AddField(model_name="invoice", name="total_amount_minor", field=models.BigIntegerField(null=True)),
        migrations.AddField(model_name="invoice", name="balance_due_minor", field=models.BigIntegerField(null=True)),
        migrations.AddField(model_name="paymentrecord", name="amount_minor", field=models.BigIntegerField(null=True)),
        # 3. Old columns become nullable, so reversing can re-add them before refilling.
        migrations.AlterField(
            model_name="invoice",
            name="total_amount",
            field=models.DecimalField(decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AlterField(
            model_name="invoice",
            name="balance_due",
            field=models.DecimalField(decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AlterField(
            model_name="paymentrecord",
            name="amount",
            field=models.DecimalField(decimal_places=2, max_digits=14, null=True),
        ),
        # 4. Convert.
        migrations.RunSQL(FORWARD, BACKWARD),
        # 5. Drop the old columns and tighten the new ones.
        migrations.RemoveField(model_name="invoice", name="total_amount"),
        migrations.RemoveField(model_name="invoice", name="balance_due"),
        migrations.RemoveField(model_name="paymentrecord", name="amount"),
        migrations.AlterField(
            model_name="invoice",
            name="total_amount_minor",
            field=models.BigIntegerField(
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(MAX_MINOR),
                ]
            ),
        ),
        migrations.AlterField(model_name="invoice", name="balance_due_minor", field=models.BigIntegerField()),
        migrations.AlterField(model_name="paymentrecord", name="amount_minor", field=models.BigIntegerField()),
        # 6. Optimistic locking, the API source, the description limit.
        migrations.AddField(model_name="invoice", name="version", field=models.PositiveIntegerField(default=1)),
        migrations.AlterField(
            model_name="invoice",
            name="source",
            field=models.CharField(
                choices=[("manual", "Manual entry"), ("csv_import", "CSV import"), ("api", "API")],
                default="manual",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="invoice",
            name="description",
            field=models.TextField(blank=True, validators=[django.core.validators.MaxLengthValidator(5000)]),
        ),
        # 7. CHECKs on the new columns, plus the new rules.
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=models.Q(total_amount_minor__gt=0), name="invoices_invoice_total_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=models.Q(total_amount_minor__lte=MAX_MINOR), name="invoices_invoice_total_max"
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=models.Q(balance_due_minor__gte=0, balance_due_minor__lte=models.F("total_amount_minor")),
                name="invoices_invoice_balance_in_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=models.Q(status="open", balance_due_minor=models.F("total_amount_minor"))
                | models.Q(
                    status="partially_paid",
                    balance_due_minor__gt=0,
                    balance_due_minor__lt=models.F("total_amount_minor"),
                )
                | models.Q(status="paid", balance_due_minor=0, paid_at__isnull=False)
                | models.Q(status="disputed", balance_due_minor__gt=0, disputed_at__isnull=False)
                | models.Q(status="void", voided_at__isnull=False),
                name="invoices_invoice_status_consistent",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=models.Q(status="paid") | models.Q(paid_at__isnull=True), name="invoices_invoice_paid_at"
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=models.Q(status="void") | models.Q(voided_at__isnull=True), name="invoices_invoice_voided_at"
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=models.Q(source__in=["manual", "csv_import", "api"]), name="invoices_invoice_source_valid"
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=django.db.models.lookups.LessThanOrEqual(
                    django.db.models.functions.text.Length("description"), 5000
                ),
                name="invoices_invoice_description_length",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=models.Q(version__gte=1), name="invoices_invoice_version_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="paymentrecord",
            constraint=models.CheckConstraint(
                condition=models.Q(amount_minor__gt=0), name="invoices_payment_amount_positive"
            ),
        ),
    ]
