"""Money as integers in the currency's minor unit (database design §7).

1250.50 USD is stored as 125050 and 120000 JPY as 120000. Amounts cross the
boundary as ``Decimal`` (forms, CSV) or decimal strings (the API) and are
converted here, exactly: a value with more decimals than its currency allows
is rejected, never rounded.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError

from .validators import SUPPORTED_CURRENCIES

# 999,999,999,999.99 in a two-decimal currency, the range NUMERIC(14,2) held.
# Far below 2**53, so every amount is exact in JSON clients as well.
MAX_MINOR = 99_999_999_999_999


def to_minor(amount: Decimal, currency: str) -> int:
    """``Decimal("1250.50"), "USD"`` → ``125050``. The amount must be positive."""
    if not isinstance(amount, Decimal) or not amount.is_finite():
        raise ValidationError("Enter a valid amount.", code="invalid")
    places = SUPPORTED_CURRENCIES[currency]
    scaled = amount.scaleb(places)
    if scaled != scaled.to_integral_value():
        raise ValidationError(
            "%(currency)s amounts allow at most %(places)s decimal places.",
            code="decimal_places",
            params={"currency": currency, "places": places},
        )
    minor = int(scaled)
    if minor <= 0:
        raise ValidationError("The amount must be greater than zero.", code="min_value")
    if minor > MAX_MINOR:
        raise ValidationError("The amount is too large.", code="max_value")
    return minor


def from_minor(minor: int, currency: str) -> Decimal:
    """``125050, "USD"`` → ``Decimal("1250.50")``; ``120000, "JPY"`` → ``Decimal("120000")``."""
    return Decimal(minor).scaleb(-SUPPORTED_CURRENCIES[currency])


def format_minor(minor: int, currency: str) -> str:
    """The API's decimal string (API contracts §1.1), with the currency's exact number of decimals."""
    return f"{from_minor(minor, currency):f}"
