from datetime import date

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q, Sum
from django.db.models.functions import Length, Lower
from django.db.models.lookups import LessThanOrEqual
from django.utils import timezone

from apps.core.models import TenantModel, tenant_key
from apps.core.money import MAX_MINOR
from apps.core.validators import CURRENCY_CODE_REGEX, validate_currency

DESCRIPTION_MAX_LENGTH = 5000


class Invoice(TenantModel):
    """An amount a customer owes (FR-005). Money is in integer minor units of
    ``currency`` (core.money): 125050 is 1,250.50 USD, 120000 is 120,000 JPY.

    Write only through invoices.services, which checks permissions, write
    access, the plan limit and ``version``.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        PARTIALLY_PAID = "partially_paid", "Partially paid"
        PAID = "paid", "Paid"
        DISPUTED = "disputed", "Disputed"
        VOID = "void", "Void"

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual entry"
        CSV_IMPORT = "csv_import", "CSV import"
        API = "api", "API"

    # Invoices that still carry an unpaid balance.
    UNSETTLED_STATUSES = [Status.OPEN, Status.PARTIALLY_PAID, Status.DISPUTED]

    customer = models.ForeignKey("customers.Customer", on_delete=models.RESTRICT, related_name="invoices")
    invoice_number = models.CharField(max_length=64)
    currency = models.CharField(max_length=3, validators=[validate_currency])
    total_amount_minor = models.BigIntegerField(validators=[MinValueValidator(1), MaxValueValidator(MAX_MINOR)])
    # Denormalized from the total and PaymentRecord rows (BR-003) so that the
    # dashboard, aging buckets and reminder scans never aggregate payments.
    # Maintained only by recalculate_balance() under a row lock.
    balance_due_minor = models.BigIntegerField()
    issue_date = models.DateField()
    due_date = models.DateField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    description = models.TextField(blank=True, validators=[MaxLengthValidator(DESCRIPTION_MAX_LENGTH)])
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
    # Optimistic lock (database design §11.6): user edits increment it.
    # Balance recalculation doesn't, as it is serialized by the row lock.
    version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            tenant_key("invoices_invoice_tenant_key"),
            models.UniqueConstraint(F("organization"), Lower("invoice_number"), name="invoices_invoice_number_uniq"),
            models.CheckConstraint(condition=Q(total_amount_minor__gt=0), name="invoices_invoice_total_positive"),
            models.CheckConstraint(condition=Q(total_amount_minor__lte=MAX_MINOR), name="invoices_invoice_total_max"),
            models.CheckConstraint(
                condition=Q(balance_due_minor__gte=0, balance_due_minor__lte=F("total_amount_minor")),
                name="invoices_invoice_balance_in_range",
            ),
            models.CheckConstraint(condition=Q(due_date__gte=F("issue_date")), name="invoices_invoice_due_after_issue"),
            models.CheckConstraint(
                condition=Q(currency__regex=CURRENCY_CODE_REGEX), name="invoices_invoice_currency_format"
            ),
            models.CheckConstraint(condition=~Q(invoice_number=""), name="invoices_invoice_number_present"),
            # Status must agree with the balance (FR-006: a zero balance is paid).
            models.CheckConstraint(
                condition=Q(status="open", balance_due_minor=F("total_amount_minor"))
                | Q(status="partially_paid", balance_due_minor__gt=0, balance_due_minor__lt=F("total_amount_minor"))
                | Q(status="paid", balance_due_minor=0, paid_at__isnull=False)
                | Q(status="disputed", balance_due_minor__gt=0, disputed_at__isnull=False)
                | Q(status="void", voided_at__isnull=False),
                name="invoices_invoice_status_consistent",
            ),
            # With the CHECK above, paid_at is set exactly when paid, voided_at exactly when void.
            models.CheckConstraint(
                condition=Q(status="paid") | Q(paid_at__isnull=True), name="invoices_invoice_paid_at"
            ),
            models.CheckConstraint(
                condition=Q(status="void") | Q(voided_at__isnull=True), name="invoices_invoice_voided_at"
            ),
            models.CheckConstraint(
                condition=Q(source__in=["manual", "csv_import", "api"]), name="invoices_invoice_source_valid"
            ),
            models.CheckConstraint(
                condition=LessThanOrEqual(Length("description"), DESCRIPTION_MAX_LENGTH),
                name="invoices_invoice_description_length",
            ),
            models.CheckConstraint(condition=Q(version__gte=1), name="invoices_invoice_version_positive"),
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

    def clean(self):
        super().clean()
        if self.issue_date and self.due_date and self.due_date < self.issue_date:
            raise ValidationError({"due_date": "The due date can't be before the issue date."})

    @property
    def amount_paid_minor(self) -> int:
        return self.total_amount_minor - self.balance_due_minor

    def is_overdue(self, as_of: date) -> bool:
        """BR-004. `as_of` is today's date in the workspace timezone."""
        return self.status != self.Status.VOID and self.balance_due_minor > 0 and self.due_date < as_of

    def days_overdue(self, as_of: date) -> int:
        return (as_of - self.due_date).days if self.is_overdue(as_of) else 0

    def derive_status(self) -> str:
        if self.voided_at:
            return self.Status.VOID
        if self.balance_due_minor == 0:
            return self.Status.PAID
        if self.disputed_at:
            return self.Status.DISPUTED
        if self.balance_due_minor == self.total_amount_minor:
            return self.Status.OPEN
        return self.Status.PARTIALLY_PAID

    def net_paid_minor(self) -> int:
        """Payments and credits minus refunds, from non-voided payment records:
        the least the total can be (an edit below it would make the balance negative)."""
        sums = self.payment_records.filter(voided_at__isnull=True).aggregate(
            reductions=Sum("amount_minor", filter=Q(kind__in=["payment", "credit"]), default=0),
            refunds=Sum("amount_minor", filter=Q(kind="refund"), default=0),
        )
        return sums["reductions"] - sums["refunds"]

    def recalculate_balance(self) -> None:
        """Re-derive balance_due_minor and status from the payment records (BR-003).

        Callers must hold SELECT ... FOR UPDATE on this invoice so concurrent
        payments cannot both read the old balance.
        """
        balance = self.total_amount_minor - self.net_paid_minor()
        if not 0 <= balance <= self.total_amount_minor:
            raise ValidationError("Recorded payments would put the balance outside 0 and the invoice total.")
        self.balance_due_minor = balance
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
    # Minor units of the invoice's currency. Always positive; the effect on
    # the balance comes from `kind`.
    amount_minor = models.BigIntegerField()
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
            models.CheckConstraint(condition=Q(amount_minor__gt=0), name="invoices_payment_amount_positive"),
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
