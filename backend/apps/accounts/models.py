from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from apps.core.ids import uuid7


def normalize_email(email: str) -> str:
    """Whole-address lowercase: uniqueness is case-insensitive (FR-001)."""
    return email.strip().lower()


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=normalize_email(email), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password, **extra_fields):
        extra_fields.update(is_staff=True, is_superuser=True)
        return self._create_user(email, password, **extra_fields)

    def get_by_natural_key(self, username):
        return self.get(email=normalize_email(username))


class User(AbstractBaseUser, PermissionsMixin):
    """Login identity. Workspace access is granted only through Membership.

    Email verification and password-reset tokens are signed, time-limited and
    invalidated by state changes (Django's token generators), so no token table
    is stored.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    # unique=True plus the lowercase CHECK below gives case-insensitive uniqueness.
    email = models.EmailField(max_length=254, unique=True)
    full_name = models.CharField(max_length=150, blank=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(email=Lower("email")), name="accounts_user_email_lowercase"),
        ]

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)

    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None

    def __str__(self):
        return self.email
