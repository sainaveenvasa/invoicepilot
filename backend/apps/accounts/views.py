from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views.decorators.http import require_http_methods, require_POST

from apps.core import throttling

from . import services
from .forms import THROTTLED_MESSAGE, LoginForm, SignupForm


@require_http_methods(["GET", "POST"])
def signup(request):
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        ip, rate = throttling.client_ip(request), settings.SIGNUP_THROTTLE_PER_IP
        if throttling.is_limited("signup-ip", ip, rate):
            form.add_error(None, THROTTLED_MESSAGE)
        else:
            throttling.hit("signup-ip", ip, rate)
            services.register_user(
                email=form.cleaned_data["email"],
                password=form.cleaned_data["password1"],
                full_name=form.cleaned_data["full_name"],
            )
            # Same destination whether or not the email already had an account.
            return redirect("accounts:signup_done")
    return render(request, "accounts/signup.html", {"form": form})


def signup_done(request):
    return render(request, "accounts/signup_done.html")


@require_http_methods(["GET", "POST"])
def verify_email(request, token):
    """GET only shows a confirm button; the POST verifies. Mail scanners that
    prefetch links therefore can't verify an address on the user's behalf."""
    if request.method == "GET":
        return render(request, "accounts/verify_email.html", {"token": token})
    user = services.verify_email(token=token)
    return render(
        request, "accounts/verify_email_done.html", {"verified": user is not None}, status=200 if user else 400
    )


@login_required
@require_POST
def resend_verification(request):
    rate = settings.VERIFICATION_RESEND_THROTTLE_PER_USER
    if throttling.is_limited("verify-resend", str(request.user.pk), rate):
        messages.error(request, THROTTLED_MESSAGE)
    else:
        throttling.hit("verify-resend", str(request.user.pk), rate)
        services.resend_verification_email(user=request.user)
        messages.success(request, "We sent a new verification link to your email.")
    return redirect(settings.LOGIN_REDIRECT_URL)


class LoginView(auth_views.LoginView):
    # Django rotates the session key on login and validates `next` against
    # ALLOWED_HOSTS (url_has_allowed_host_and_scheme).
    form_class = LoginForm
    template_name = "accounts/login.html"
    redirect_authenticated_user = True


class LogoutView(auth_views.LogoutView):
    """POST only (Django's default), so a link or image can't sign a user out."""


class PasswordResetView(auth_views.PasswordResetView):
    """Unknown emails get the same response and no email (PasswordResetForm
    only mails active accounts with a usable password)."""

    template_name = "accounts/password_reset_form.html"
    email_template_name = "accounts/emails/password_reset.txt"
    subject_template_name = "accounts/emails/password_reset_subject.txt"
    success_url = reverse_lazy("accounts:password_reset_done")

    def form_valid(self, form):
        ip, rate = throttling.client_ip(self.request), settings.PASSWORD_RESET_THROTTLE_PER_IP
        if throttling.is_limited("reset-ip", ip, rate):
            form.add_error(None, THROTTLED_MESSAGE)
            return self.form_invalid(form)
        throttling.hit("reset-ip", ip, rate)
        # The email template links from SITE_URL, not the request's host.
        self.extra_email_context = {"site_url": settings.SITE_URL}
        return super().form_valid(form)


class PasswordResetDoneView(auth_views.PasswordResetDoneView):
    template_name = "accounts/password_reset_done.html"


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    # Django moves the token from the URL into the session and redirects at
    # once, so it doesn't leak through the Referer header. Changing the
    # password invalidates every other session (session auth hash).
    template_name = "accounts/password_reset_confirm.html"
    success_url = reverse_lazy("accounts:password_reset_complete")


class PasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    template_name = "accounts/password_reset_complete.html"
