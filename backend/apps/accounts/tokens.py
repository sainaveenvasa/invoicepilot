"""Email-verification tokens, built on Django's signing framework.

The token signs the user's ID *and current email* with a purpose-specific
salt and a timestamp, so it expires (EMAIL_VERIFICATION_MAX_AGE), dies when
the email changes, and can't be swapped with another signed value such as a
password-reset token (threat model T2, T4). Password reset uses Django's own
PasswordResetTokenGenerator.
"""

from django.conf import settings
from django.core import signing

_SALT = "invoicepilot.accounts.email-verification"


def make_email_verification_token(user) -> str:
    return signing.dumps({"user": str(user.pk), "email": user.email}, salt=_SALT, compress=True)


def read_email_verification_token(token: str) -> tuple[str, str] | None:
    """Return (user_id, email) for a valid, unexpired token, else None."""
    try:
        payload = signing.loads(token, salt=_SALT, max_age=settings.EMAIL_VERIFICATION_MAX_AGE)
    except signing.BadSignature:  # includes SignatureExpired
        return None
    if not isinstance(payload, dict) or not {"user", "email"} <= payload.keys():
        return None
    return payload["user"], payload["email"]
