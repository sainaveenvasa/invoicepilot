import zoneinfo

from django import forms

from apps.core.validators import SUPPORTED_CURRENCIES


class WorkspaceForm(forms.Form):
    name = forms.CharField(max_length=200)
    timezone = forms.ChoiceField(choices=[], initial="UTC")
    default_currency = forms.ChoiceField(choices=[(code, code) for code in sorted(SUPPORTED_CURRENCIES)], initial="USD")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["timezone"].choices = [(tz, tz) for tz in sorted(zoneinfo.available_timezones())]

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("Enter a name for the workspace.")
        return name
