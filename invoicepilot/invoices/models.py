from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q, Sum
from django.db.models.functions import Lower
from django.utils import timezone

from invoicepilot.core.models import TenantModel, tenant_key
from invoicepilot.core.validators import CURRENCY_CODE_REGEX, validate_currency

MONEY = {"max_digits": 14, "decimal_places": 2}


class Invoice(TenantModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        PARTIALLY_PAID = "partially_paid", "Partially paid"
        PAID = "paid", "Paid"
        DISPUTED = "disputed", "Disputed"
        VOID = "void", "Void"

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual entry"
        CSV_IMPORT = "csv_import", "CSV import"

    # Invoices that still carry an unpaid balance.
    UNSETTLED_STATUSES = [Status.OPEN, Status.PARTIALLY_PAID, Status.DISPUTED]

    customer = models.ForeignKey("customers.Customer", on_delete=models.RESTRICT, related_name="invoices")
    invoice_number = models.CharField(max_length=64)
    currency = models.CharField(max_length=3, validators=[validate_currency])
    total_amount = models.DecimalField(**MONEY)
    # Denormalized from total_amount and PaymentRecord rows (BR-003) so that the
    # dashboard, aging buckets and reminder scans never aggregate payments.
    # Maintained only by recalculate_balance() under a row lock.
    balance_due = models.DecimalField(**MONEY)
    issue_date = models.DateField()
    due_date = models.DateField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    description = models.TextField(blank=True)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL)

    reminders_paused = models.BooleanField(default=False)
    paused_at = models.DateTimeField(null=True, blank=True)
    pause_reason = models.CharField(max_length=255, blank=True)
    disputed_at = models.DateTimeField(null=True, blank=True)
    dispute_note = models.TextField(blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            tenant_key("invoices_invoice_tenant_key"),
            models.UniqueConstraint(
                F("organization"), Lower("invoice_number"), name="invoices_invoice_number_uniq"
            ),
            models.CheckConstraint(condition=Q(total_amount__gt=0), name="invoices_invoice_total_positive"),
            models.CheckConstraint(
                condition=Q(balance_due__gte=0, balance_due__lte=F("total_amount")),
                name="invoices_invoice_balance_in_range",
            ),
            models.CheckConstraint(condition=Q(due_date__gte=F("issue_date")), name="invoices_invoice_due_after_issue"),
            models.CheckConstraint(
                condition=Q(currency__regex=CURRENCY_CODE_REGEX), name="invoices_invoice_currency_format"
            ),
            models.CheckConstraint(condition=~Q(invoice_number=""), name="invoices_invoice_number_present"),
            # Status must agree with the balance (FR-006: a zero balance is paid).
            models.CheckConstraint(
                condition=Q(status="open", balance_due=F("total_amount"))
                | Q(status="partially_paid", balance_due__gt=0, balance_due__lt=F("total_amount"))
                | Q(status="paid", balance_due=0, paid_at__isnull=False)
                | Q(status="disputed", balance_due__gt=0, disputed_at__isnull=False)
                | Q(status="void", voided_at__isnull=False),
                name="invoices_invoice_status_consistent",
            ),
            models.CheckConstraint(
                condition=Q(reminders_paused=False) | Q(paused_at__isnull=False),
                name="invoices_invoice_pause_consistent",
            ),
        ]
        indexes = [
            # Dashboard totals, aging buckets and overdue lists (FR-012).
            models.Index(
                fields=["organization", "due_date"],
                condition=Q(status__in=["open", "partially_paid", "disputed"]),
                name="invoices_unsettled_due_idx",
            ),
            # Default invoice list ordering and pagination.
            models.Index(fields=["organization", "-issue_date"], name="invoices_org_issue_idx"),
            # Monthly plan-limit counts (BR-012).
            models.Index(fields=["organization", "created_at"], name="invoices_org_created_idx"),
            # Cross-tenant scan used by the reminder scheduler.
            models.Index(
                fields=["due_date"],
                condition=Q(status__in=["open", "partially_paid"], reminders_paused=False),
                name="invoices_reminder_scan_idx",
            ),
        ]

    def __str__(self):
        return self.invoice_number

    def is_overdue(self, as_of: date) -> bool:
        """BR-004. `as_of` is today's date in the workspace timezone."""
        return self.status != self.Status.VOID and self.balance_due > 0 and self.due_date < as_of

    def derive_status(self) -> str:
        if self.voided_at:
            return self.Status.VOID
        if self.balance_due == 0:
            return self.Status.PAID
        if self.disputed_at:
            return self.Status.DISPUTED
        if self.balance_due == self.total_amount:
            return self.Status.OPEN
        return self.Status.PARTIALLY_PAID

    def recalculate_balance(self) -> None:
        """Re-derive balance_due and status from non-voided payment records (BR-003).

        Callers must hold SELECT ... FOR UPDATE on this invoice so concurrent
        payments cannot both read the old balance.
        """
        sums = self.payment_records.filter(voided_at__isnull=True).aggregate(
            reductions=Sum("amount", filter=Q(kind__in=["payment", "credit"]), default=Decimal("0")),
            refunds=Sum("amount", filter=Q(kind="refund"), default=Decimal("0")),
        )
        balance = self.total_amount - sums["reductions"] + sums["refunds"]
        if not Decimal("0") <= balance <= self.total_amount:
            raise ValidationError("Recorded payments would put the balance outside 0 and the invoice total.")
        self.balance_due = balance
        self.status = self.derive_status()
        if self.status == self.Status.PAID:
            self.paid_at = self.paid_at or timezone.now()
        else:
            self.paid_at = None


class PaymentRecord(TenantModel):
    """A user-asserted payment, credit or refund. InvoicePilot never processes
    invoice payments, so these are records, not settlements (BR-015)."""

    class Kind(models.TextChoices):
        PAYMENT = "payment", "Payment received"
        CREDIT = "credit", "Credit / adjustment"
        REFUND = "refund", "Refund"

    class Method(models.TextChoices):
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        CARD = "card", "Card"
        CASH = "cash", "Cash"
        CHEQUE = "cheque", "Cheque"
        ONLINE = "online", "Online payment service"
        OTHER = "other", "Other"

    invoice = models.ForeignKey(Invoice, on_delete=models.RESTRICT, related_name="payment_records")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.PAYMENT)
    # Always positive; the effect on the balance comes from `kind`.
    amount = models.DecimalField(**MONEY)
    received_on = models.DateField()
    method = models.CharField(max_length=20, choices=Method.choices, blank=True)
    reference = models.CharField(max_length=128, blank=True)
    note = models.TextField(blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Mistakes are voided rather than deleted so the audit trail stays intact.
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    void_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="invoices_payment_amount_positive"),
            models.CheckConstraint(
                condition=Q(kind__in=["payment", "credit", "refund"]), name="invoices_payment_kind_valid"
            ),
            models.CheckConstraint(
                condition=Q(voided_by__isnull=True) | Q(voided_at__isnull=False),
                name="invoices_payment_void_consistent",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "received_on"], name="invoices_payment_org_recv_idx"),
        ]
