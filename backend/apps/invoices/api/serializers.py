import re
from decimal import Decimal

from rest_framework import serializers

from apps.core import money
from apps.core.api.serializers import StrictFieldsMixin
from apps.core.validators import SUPPORTED_CURRENCIES
from apps.customers.models import Customer
from apps.invoices import selectors
from apps.invoices.models import DESCRIPTION_MAX_LENGTH, Invoice

CURRENCY_CHOICES = sorted(SUPPORTED_CURRENCIES)


class AmountField(serializers.Field):
    """A decimal string such as ``"1250.50"``, never a JSON number, so no
    client float ever becomes money (API contracts §1.1). The currency's
    decimal places and the range are checked by the service."""

    default_error_messages = {"invalid": 'Enter the amount as a decimal string, for example "1250.50".'}
    PATTERN = re.compile(r"-?\d{1,15}(\.\d{1,6})?")

    def to_internal_value(self, data):
        if not isinstance(data, str) or not self.PATTERN.fullmatch(data):
            self.fail("invalid")
        return Decimal(data)


class CustomerSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = ["id", "name", "email"]
        read_only_fields = fields


class InvoiceSerializer(serializers.ModelSerializer):
    """The invoice resource. Needs ``today`` (the workspace's date) in the context."""

    customer = CustomerSummarySerializer(read_only=True)
    total_amount = serializers.SerializerMethodField()
    balance_due = serializers.SerializerMethodField()
    amount_paid = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()
    days_overdue = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            "id",
            "customer",
            "invoice_number",
            "currency",
            "total_amount",
            "balance_due",
            "amount_paid",
            "issue_date",
            "due_date",
            "status",
            "is_overdue",
            "days_overdue",
            "description",
            "source",
            "reminders_paused",
            "pause_reason",
            "disputed_at",
            "dispute_note",
            "paid_at",
            "voided_at",
            "version",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_total_amount(self, invoice) -> str:
        return money.format_minor(invoice.total_amount_minor, invoice.currency)

    def get_balance_due(self, invoice) -> str:
        return money.format_minor(invoice.balance_due_minor, invoice.currency)

    def get_amount_paid(self, invoice) -> str:
        return money.format_minor(invoice.amount_paid_minor, invoice.currency)

    def get_is_overdue(self, invoice) -> bool:
        return invoice.is_overdue(self.context["today"])

    def get_days_overdue(self, invoice) -> int:
        return invoice.days_overdue(self.context["today"])


UPDATE_FIELDS = ("invoice_number", "total_amount", "issue_date", "due_date", "description")


class InvoiceUpdateSerializer(StrictFieldsMixin, serializers.Serializer):
    """``PATCH /invoices/{id}``. The customer and currency are immutable, and
    balance and status belong to the services, so sending them is an error."""

    invoice_number = serializers.CharField(max_length=64)
    total_amount = AmountField()
    issue_date = serializers.DateField()
    due_date = serializers.DateField()
    description = serializers.CharField(max_length=DESCRIPTION_MAX_LENGTH, allow_blank=True)

    read_only_names = tuple(sorted({*InvoiceSerializer.Meta.fields, "customer_id"} - set(UPDATE_FIELDS)))


class InvoiceCreateSerializer(InvoiceUpdateSerializer):
    customer_id = serializers.UUIDField()
    currency = serializers.ChoiceField(choices=CURRENCY_CHOICES, required=False)
    description = serializers.CharField(max_length=DESCRIPTION_MAX_LENGTH, allow_blank=True, required=False)

    read_only_names = tuple(sorted(set(InvoiceSerializer.Meta.fields) - {*UPDATE_FIELDS, "currency"}))


class InvoiceFilterSerializer(serializers.Serializer):
    """Query parameters of ``GET /invoices``. Pass a plain dict with ``status``
    as a list: from a QueryDict, DRF reads a missing boolean as false."""

    status = serializers.ListField(child=serializers.ChoiceField(choices=Invoice.Status.choices), default=list)
    customer_id = serializers.UUIDField(required=False, default=None)
    overdue = serializers.BooleanField(required=False, allow_null=True, default=None)
    due_from = serializers.DateField(required=False, default=None)
    due_to = serializers.DateField(required=False, default=None)
    currency = serializers.ChoiceField(choices=CURRENCY_CHOICES, required=False, default=None)
    q = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    ordering = serializers.ChoiceField(choices=list(selectors.ORDERINGS), required=False, default="-issue_date")

    def validate(self, attrs):
        if attrs["due_from"] and attrs["due_to"] and attrs["due_from"] > attrs["due_to"]:
            raise serializers.ValidationError({"due_to": ["Must be on or after due_from."]})
        return attrs
