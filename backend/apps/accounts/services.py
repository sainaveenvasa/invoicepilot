from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, transaction
from django.utils import timezone

from .emails import send_signup_notice, send_verification_email
from .models import User, normalize_email
from .tokens import read_email_verification_token


def register_user(*, email: str, password: str, full_name: str = "") -> User | None:
    """Create an account and email a verification link.

    If the email already has an account, nothing is created and its owner is
    told instead. The caller shows the same "check your inbox" page either
    way, so sign-up never reveals whether an account exists. Returns the new
    user, or None when the email was taken.
    """
    email = normalize_email(email)
    user = None
    try:
        with transaction.atomic():
            if not User.objects.filter(email=email).exists():
                user = User.objects.create_user(email=email, password=password, full_name=full_name.strip())
    except IntegrityError:  # a concurrent sign-up won the race for this email
        user = None

    # Emails are sent after the transaction commits: no network calls inside one.
    if user is None:
        make_password(password)  # same hashing cost as a real sign-up, so timing doesn't tell
        send_signup_notice(User.objects.get(email=email))
        return None
    send_verification_email(user)
    return user


def verify_email(*, token: str) -> User | None:
    """Mark the user's email verified. Idempotent; None if the token is
    invalid, expired, or for an email the account no longer has."""
    claims = read_email_verification_token(token)
    if claims is None:
        return None
    user_id, email = claims
    user = User.objects.filter(pk=user_id, is_active=True).first()
    if user is None or user.email != email:
        return None
    if user.email_verified_at is None:
        user.email_verified_at = timezone.now()
        user.save(update_fields=["email_verified_at"])
    return user


def resend_verification_email(*, user: User) -> None:
    if not user.is_email_verified:
        send_verification_email(user)
