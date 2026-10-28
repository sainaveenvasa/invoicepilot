from rest_framework import serializers

from apps.core.api.serializers import StrictFieldsMixin
from apps.customers import selectors
from apps.customers.models import Customer


class CustomerSerializer(StrictFieldsMixin, serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = [
            "id",
            "name",
            "email",
            "company_name",
            "notes",
            "is_active",
            "deactivated_at",
            "version",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "is_active", "deactivated_at", "version", "created_at", "updated_at"]


class CustomerFilterSerializer(serializers.Serializer):
    """Query parameters of ``GET /customers``. Pass a plain dict: from a
    QueryDict, DRF reads a missing boolean as false."""

    q = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    is_active = serializers.BooleanField(required=False, allow_null=True, default=None)
    ordering = serializers.ChoiceField(choices=selectors.ORDERINGS, required=False, default="name")
