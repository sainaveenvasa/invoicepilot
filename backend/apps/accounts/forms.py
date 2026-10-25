from django import forms
from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

from apps.core import throttling

from .models import User, normalize_email

THROTTLED_MESSAGE = "Too many attempts. Please wait a few minutes and try again."


class SignupForm(forms.Form):
    """A plain Form, not a ModelForm: a ModelForm's unique check would say
    "this email is taken" and reveal which accounts exist (threat model T4)."""

    full_name = forms.CharField(max_length=150, required=False, widget=forms.TextInput(attrs={"autocomplete": "name"}))
    email = forms.EmailField(max_length=254, widget=forms.EmailInput(attrs={"autocomplete": "email"}))
    password1 = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text=password_validation.password_validators_help_text_html(),
    )
    password2 = forms.CharField(
        label="Confirm password", strip=False, widget=forms.PasswordInput(attrs={"autocomplete": "new-password"})
    )

    def clean_email(self):
        return normalize_email(self.cleaned_data["email"])

    def clean(self):
        cleaned = super().clean()
        password1, password2 = cleaned.get("password1"), cleaned.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", "The two passwords don't match.")
        elif password1:
            candidate = User(email=cleaned.get("email", ""), full_name=cleaned.get("full_name", ""))
            try:
                password_validation.validate_password(password1, user=candidate)
            except ValidationError as error:
                self.add_error("password1", error)
        return cleaned


class LoginForm(AuthenticationForm):
    """Email + password login with per-account and per-IP throttles.

    Failures are counted for the submitted email whether or not an account
    exists, and "unknown email" and "wrong password" give the same message,
    so the form can't be used to discover accounts.
    """

    username = forms.EmailField(
        label="Email", max_length=254, widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"})
    )

    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": "Enter a correct email and password.",
        "throttled": THROTTLED_MESSAGE,
    }

    def clean(self):
        email = normalize_email(self.cleaned_data.get("username") or "")
        ip = throttling.client_ip(self.request) if self.request else ""
        account_rate, ip_rate = settings.LOGIN_THROTTLE_PER_ACCOUNT, settings.LOGIN_THROTTLE_PER_IP
        if throttling.is_limited("login-account", email, account_rate) or throttling.is_limited(
            "login-ip", ip, ip_rate
        ):
            raise ValidationError(self.error_messages["throttled"], code="throttled")
        try:
            cleaned = super().clean()
        except ValidationError:
            throttling.hit("login-account", email, account_rate)
            throttling.hit("login-ip", ip, ip_rate)
            raise
        throttling.reset("login-account", email)
        return cleaned
