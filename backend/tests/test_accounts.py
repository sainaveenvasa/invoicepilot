"""Registration, login, logout, email verification and password reset (FR-001, threat model T2-T4)."""

import re
import time
import uuid
from datetime import datetime, timedelta
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.auth.tokens import PasswordResetTokenGenerator, default_token_generator
from django.core import mail, signing
from django.test import Client
from django.urls import reverse
from django.utils.http import urlsafe_base64_encode

from apps.accounts import services
from apps.accounts.models import User
from apps.accounts.tokens import make_email_verification_token, read_email_verification_token

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db

NEW_PASSWORD = "a-different-long-passphrase"


def signup(client, email="dana@example.com", password=PASSWORD, confirm=None, full_name="Dana Reyes"):
    return client.post(
        reverse("accounts:signup"),
        {"full_name": full_name, "email": email, "password1": password, "password2": confirm or password},
    )


def login(client, email="dana@example.com", password=PASSWORD, **extra):
    return client.post(reverse("accounts:login"), {"username": email, "password": password, **extra})


def link_in(message, path_prefix):
    match = re.search(rf"(\S+{re.escape(path_prefix)}\S+)", message.body)
    assert match, message.body
    return match.group(1)


def verification_token(message):
    return link_in(message, "/verify-email/").rstrip("/").rsplit("/", 1)[-1]


# --- User model -------------------------------------------------------------


def test_user_has_uuid_primary_key_and_normalized_email():
    user = User.objects.create_user(email="  Dana.Reyes@Example.COM ", password=PASSWORD)
    assert isinstance(user.pk, uuid.UUID)
    assert user.pk.version == 7
    assert user.email == "dana.reyes@example.com"
    assert User.objects.get_by_natural_key("DANA.REYES@example.com") == user


def test_password_is_stored_only_as_a_hash():
    user = User.objects.create_user(email="dana@example.com", password=PASSWORD)
    assert PASSWORD not in user.password
    assert user.check_password(PASSWORD)


def test_superuser_is_staff_and_superuser():
    admin = User.objects.create_superuser(email="ops@example.com", password=PASSWORD)
    assert admin.is_staff and admin.is_superuser


# --- Registration -----------------------------------------------------------


def test_signup_creates_an_unverified_user_and_emails_a_link_built_from_site_url(client):
    response = signup(client, email="Dana@Example.com")

    assert response.status_code == 302
    assert response["Location"] == reverse("accounts:signup_done")
    user = User.objects.get(email="dana@example.com")
    assert user.full_name == "Dana Reyes"
    assert not user.is_email_verified
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["dana@example.com"]
    assert link_in(mail.outbox[0], "/verify-email/").startswith(settings.SITE_URL + "/verify-email/")
    assert "testserver" not in mail.outbox[0].body


def test_signup_does_not_log_the_user_in(client):
    signup(client)
    assert "_auth_user_id" not in client.session


def test_signup_with_an_existing_email_reveals_nothing(client, make_user):
    existing = make_user(email="dana@example.com")
    fresh = signup(Client(), email="someone.new@example.com")
    mail.outbox.clear()

    response = signup(client, email="DANA@example.com", password=NEW_PASSWORD)

    assert response.status_code == fresh.status_code
    assert response["Location"] == fresh["Location"]
    assert User.objects.filter(email="dana@example.com").count() == 1
    existing.refresh_from_db()
    assert existing.check_password(PASSWORD)  # the attempt changed nothing
    # The real owner hears about it; no verification link is issued.
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["dana@example.com"]
    assert "/verify-email/" not in mail.outbox[0].body


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("short-pw", "too short"),
        ("123456789012345", "entirely numeric"),
        ("password12345", "too common"),
        ("dana@example.com", "too similar"),
    ],
)
def test_signup_rejects_weak_passwords(client, password, message):
    response = signup(client, password=password)
    assert response.status_code == 200
    assert message in response.content.decode()
    assert not User.objects.exists()


def test_signup_rejects_mismatched_passwords(client):
    response = signup(client, confirm=NEW_PASSWORD)
    assert "don&#x27;t match" in response.content.decode()
    assert not User.objects.exists()


def test_signup_is_throttled_per_ip(client, settings):
    settings.SIGNUP_THROTTLE_PER_IP = (2, 3600)
    signup(client, email="one@example.com")
    signup(client, email="two@example.com")
    response = signup(client, email="three@example.com")
    assert response.status_code == 200
    assert "Too many attempts" in response.content.decode()
    assert not User.objects.filter(email="three@example.com").exists()


def test_signup_requires_a_csrf_token():
    response = signup(Client(enforce_csrf_checks=True))
    assert response.status_code == 403
    assert not User.objects.exists()


# --- Email verification -------------------------------------------------------


def test_opening_the_link_does_not_verify_but_confirming_does(client):
    signup(client)
    url = reverse("accounts:verify_email", kwargs={"token": verification_token(mail.outbox[0])})
    user = User.objects.get(email="dana@example.com")

    assert client.get(url).status_code == 200
    user.refresh_from_db()
    assert not user.is_email_verified  # a mail scanner prefetching the link verifies nothing

    response = client.post(url)
    assert response.status_code == 200
    user.refresh_from_db()
    assert user.is_email_verified


def test_verification_is_idempotent(make_user):
    user = make_user(verified=False)
    token = make_email_verification_token(user)
    first = services.verify_email(token=token).email_verified_at
    assert services.verify_email(token=token).email_verified_at == first


def test_signed_tokens_reject_tampering_and_purpose_swap(client, make_user):
    user = make_user(verified=False)
    token = make_email_verification_token(user)
    claims = {"user": str(user.pk), "email": user.email}

    assert read_email_verification_token(token[:-2] + ("aa" if not token.endswith("aa") else "bb")) is None
    assert read_email_verification_token(signing.dumps(claims)) is None  # no purpose salt
    assert read_email_verification_token(signing.dumps(claims, salt="invoicepilot.unsubscribe")) is None
    assert read_email_verification_token(default_token_generator.make_token(user)) is None

    response = client.post(reverse("accounts:verify_email", kwargs={"token": signing.dumps(claims)}))
    assert response.status_code == 400
    user.refresh_from_db()
    assert not user.is_email_verified


def test_verification_token_expires_after_48_hours(make_user):
    user = make_user(verified=False)
    issued = time.time() - (48 * 3600 + 60)
    with mock.patch("django.core.signing.time.time", return_value=issued):
        token = make_email_verification_token(user)
    assert services.verify_email(token=token) is None


def test_verification_token_void_after_email_change(make_user):
    user = make_user(verified=False)
    token = make_email_verification_token(user)
    user.email = "dana.new@example.com"
    user.save()
    assert services.verify_email(token=token) is None
    user.refresh_from_db()
    assert not user.is_email_verified


def test_inactive_users_cannot_verify(make_user):
    user = make_user(verified=False, is_active=False)
    assert services.verify_email(token=make_email_verification_token(user)) is None


def test_resend_verification_email(make_user, client_for, settings):
    settings.VERIFICATION_RESEND_THROTTLE_PER_USER = (1, 3600)
    client = client_for(make_user(verified=False))

    client.post(reverse("accounts:resend_verification"))
    client.post(reverse("accounts:resend_verification"))  # throttled

    assert len(mail.outbox) == 1
    assert "/verify-email/" in mail.outbox[0].body


def test_resend_is_a_no_op_once_verified(make_user, client_for):
    client_for(make_user(verified=True)).post(reverse("accounts:resend_verification"))
    assert mail.outbox == []


# --- Login and logout ---------------------------------------------------------


def test_login_with_any_email_case(client, make_user):
    user = make_user()
    response = login(client, email="DANA@Example.com")
    assert response.status_code == 302
    assert response["Location"] == reverse(settings.LOGIN_REDIRECT_URL)
    assert client.session["_auth_user_id"] == str(user.pk)


def test_login_does_not_reveal_whether_an_account_exists(client, make_user):
    make_user()
    wrong_password = login(client, password="not-the-password")
    unknown_email = login(Client(), email="nobody@example.com")
    assert wrong_password.status_code == unknown_email.status_code == 200
    assert wrong_password.context["form"].errors == unknown_email.context["form"].errors
    assert "_auth_user_id" not in client.session


def test_inactive_user_gets_the_generic_error(client, make_user):
    make_user(is_active=False)
    response = login(client)
    assert response.context["form"].errors == {"__all__": ["Enter a correct email and password."]}


def test_login_throttled_after_failed_attempts(client, make_user):
    make_user()
    for _ in range(5):
        login(client, password="not-the-password")

    response = login(client)  # correct password, but the account is locked for now

    assert response.status_code == 200
    assert "Too many attempts" in response.content.decode()
    assert "_auth_user_id" not in client.session


def test_login_throttle_counts_unknown_emails_too(client):
    for _ in range(5):
        login(client, email="nobody@example.com", password="guess")
    response = login(client, email="nobody@example.com", password="guess")
    assert "Too many attempts" in response.content.decode()


def test_login_throttled_per_ip_across_accounts(client, make_user, settings):
    settings.LOGIN_THROTTLE_PER_IP = (3, 600)
    make_user()
    for n in range(3):
        login(client, email=f"guess{n}@example.com", password="guess")
    response = login(client)
    assert "Too many attempts" in response.content.decode()


def test_successful_login_resets_the_account_counter(client, make_user):
    make_user()
    for _ in range(4):
        login(client, password="not-the-password")
    login(client)
    client.post(reverse("accounts:logout"))
    for _ in range(4):
        login(client, password="not-the-password")
    assert login(client).status_code == 302


def test_session_rotated_on_login(client, make_user):
    make_user()
    before = client.session.session_key
    login(client)
    assert client.cookies[settings.SESSION_COOKIE_NAME].value != before


def test_login_redirect_rejects_external_next(client, make_user):
    make_user()
    response = login(client, next="https://evil.example/phish")
    assert response["Location"] == reverse(settings.LOGIN_REDIRECT_URL)


def test_login_redirect_follows_internal_next(client, make_user):
    make_user()
    response = login(client, next=reverse("app:workspace_list"))
    assert response["Location"] == reverse("app:workspace_list")


def test_logout_requires_post(make_user, client_for):
    client = client_for(make_user())
    assert client.get(reverse("accounts:logout")).status_code == 405
    assert "_auth_user_id" in client.session

    client.post(reverse("accounts:logout"))
    assert "_auth_user_id" not in client.session


# --- Password reset -----------------------------------------------------------


def request_reset(client, email="dana@example.com", **extra):
    return client.post(reverse("accounts:password_reset"), {"email": email}, **extra)


def test_password_reset_link_uses_configured_site_url(client, make_user):
    make_user()
    response = request_reset(client)
    assert response["Location"] == reverse("accounts:password_reset_done")
    assert len(mail.outbox) == 1
    assert link_in(mail.outbox[0], "/reset/").startswith(settings.SITE_URL + "/reset/")
    assert "testserver" not in mail.outbox[0].body


def test_password_reset_rejects_forged_host_header(client, make_user):
    make_user()
    response = request_reset(client, HTTP_HOST="evil.example")
    assert response.status_code == 400
    assert mail.outbox == []


def test_password_reset_does_not_reveal_accounts(client, make_user):
    make_user()
    known = request_reset(client)
    unknown = request_reset(Client(), email="nobody@example.com")
    assert known.status_code == unknown.status_code == 302
    assert known["Location"] == unknown["Location"]
    assert len(mail.outbox) == 1  # only the real account got an email


def test_password_reset_changes_password_and_invalidates_other_sessions(client, make_user, client_for):
    user = make_user()
    other_device = client_for(user)
    # Signed in: with no workspace yet, the picker sends them to create one.
    assert other_device.get(reverse("app:workspace_list"))["Location"] == reverse("app:workspace_create")

    request_reset(client)
    response = client.get(link_in(mail.outbox[0], "/reset/").removeprefix(settings.SITE_URL))
    assert response.status_code == 302  # token moved into the session, out of the URL
    response = client.post(response["Location"], {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
    assert response["Location"] == reverse("accounts:password_reset_complete")

    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
    response = other_device.get(reverse("app:workspace_list"))
    assert response["Location"].startswith(reverse("accounts:login"))


def test_reset_token_expires_after_one_hour(client, make_user):
    user = make_user()
    uidb64 = urlsafe_base64_encode(str(user.pk).encode())
    token = default_token_generator.make_token(user)
    url = reverse("accounts:password_reset_confirm", kwargs={"uidb64": uidb64, "token": token})
    later = datetime.now() + timedelta(seconds=3601)

    with mock.patch.object(PasswordResetTokenGenerator, "_now", return_value=later):
        response = client.get(url, follow=True)

    assert response.context["validlink"] is False


def test_password_reset_is_throttled_per_ip(client, make_user, settings):
    settings.PASSWORD_RESET_THROTTLE_PER_IP = (2, 3600)
    make_user()
    request_reset(client)
    request_reset(client)
    response = request_reset(client)
    assert response.status_code == 200
    assert "Too many attempts" in response.content.decode()
    assert len(mail.outbox) == 2
