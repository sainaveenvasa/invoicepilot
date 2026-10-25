from collections.abc import Mapping

from rest_framework import serializers


class StrictFieldsMixin:
    """Reject unknown and read-only fields instead of silently ignoring them
    (API contracts §1.1), so a typo or a mass-assignment attempt is visible."""

    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            writable = {name for name, field in self.fields.items() if not field.read_only}
            errors = {
                key: ["This field is read-only." if key in self.fields else "Unknown field."]
                for key in sorted(set(data) - writable)
            }
            if errors:
                raise serializers.ValidationError(errors)
        return super().to_internal_value(data)
