from rest_framework import serializers

from apps.core.api.serializers import StrictFieldsMixin
from apps.organizations.models import Membership, Organization


class WorkspaceSerializer(StrictFieldsMixin, serializers.ModelSerializer):
    read_only = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = ["id", "name", "timezone", "default_currency", "read_only", "read_only_since", "created_at"]
        read_only_fields = ["id", "read_only_since", "created_at"]

    def get_read_only(self, organization) -> bool:
        return organization.read_only_since is not None

    def validate_name(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("This field may not be blank.")
        return value


class MembershipSummarySerializer(serializers.ModelSerializer):
    organization_id = serializers.UUIDField(read_only=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True)

    class Meta:
        model = Membership
        fields = ["organization_id", "organization_name", "role"]
        read_only_fields = fields
