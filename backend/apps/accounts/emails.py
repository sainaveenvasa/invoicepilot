"""Account emails. Links are built from settings.SITE_URL, never from the
request's Host header, so a forged Host can't poison them (threat model T4)."""

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse

from .tokens import make_email_verification_token


def _send(template: str, to: str, context: dict) -> None:
    context = {"site_url": settings.SITE_URL, **context}
    subject = render_to_string(f"accounts/emails/{template}_subject.txt", context).strip()
    body = render_to_string(f"accounts/emails/{template}.txt", context)
    send_mail(subject, body, None, [to])


def send_verification_email(user) -> None:
    path = reverse("accounts:verify_email", kwargs={"token": make_email_verification_token(user)})
    _send("verify_email", user.email, {"user": user, "verify_url": settings.SITE_URL + path})


def send_signup_notice(user) -> None:
    """Sent instead of a second account when someone signs up with an email
    that already has one, so the sign-up form can't reveal which emails exist."""
    _send(
        "signup_notice",
        user.email,
        {
            "user": user,
            "login_url": settings.SITE_URL + reverse("accounts:login"),
            "reset_url": settings.SITE_URL + reverse("accounts:password_reset"),
        },
    )
