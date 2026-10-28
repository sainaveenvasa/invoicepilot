from django import forms

from .models import NOTES_MAX_LENGTH


class CustomerForm(forms.Form):
    name = forms.CharField(max_length=200)
    email = forms.EmailField(max_length=254)
    company_name = forms.CharField(max_length=200, required=False)
    notes = forms.CharField(max_length=NOTES_MAX_LENGTH, required=False, widget=forms.Textarea(attrs={"rows": 4}))


class CustomerEditForm(CustomerForm):
    # The version the user loaded; the service refuses the save if it's stale.
    version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)

    def changes(self) -> dict:
        return {name: value for name, value in self.cleaned_data.items() if name != "version"}


class CustomerSearchForm(forms.Form):
    STATUS_CHOICES = [("active", "Active"), ("inactive", "Deactivated"), ("all", "All")]
    IS_ACTIVE = {"active": True, "inactive": False, "all": None}

    q = forms.CharField(max_length=200, required=False, label="Search")
    status = forms.ChoiceField(choices=STATUS_CHOICES, required=False)

    def search(self) -> tuple[str, str]:
        """(q, status). An invalid query falls back to all active customers."""
        data = self.cleaned_data if self.is_valid() else {}
        return data.get("q", ""), data.get("status") or "active"
