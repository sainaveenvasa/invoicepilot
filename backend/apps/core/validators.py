import zoneinfo
from decimal import Decimal

from django.core.exceptions import ValidationError

# ISO 4217 codes supported in the MVP, mapped to their minor-unit exponent.
# Amounts are stored as NUMERIC(14, 2), so currencies with three minor units
# (e.g. KWD, BHD) are deliberately excluded until the money type is revisited.
SUPPORTED_CURRENCIES = {
    "AUD": 2,
    "BRL": 2,
    "CAD": 2,
    "CHF": 2,
    "CZK": 2,
    "DKK": 2,
    "EUR": 2,
    "GBP": 2,
    "HKD": 2,
    "INR": 2,
    "JPY": 0,
    "MXN": 2,
    "NOK": 2,
    "NZD": 2,
    "PLN": 2,
    "SEK": 2,
    "SGD": 2,
    "USD": 2,
    "ZAR": 2,
}

CURRENCY_CODE_REGEX = r"^[A-Z]{3}$"


def validate_currency(value: str) -> None:
    if value not in SUPPORTED_CURRENCIES:
        raise ValidationError("%(value)s is not a supported currency.", params={"value": value})


def validate_amount_for_currency(amount: Decimal, currency: str) -> None:
    """A JPY invoice cannot carry fractional yen, even though the column allows cents."""
    exponent = SUPPORTED_CURRENCIES.get(currency)
    if exponent is not None and amount != amount.quantize(Decimal(1).scaleb(-exponent)):
        raise ValidationError(
            "%(currency)s amounts allow at most %(places)s decimal places.",
            params={"currency": currency, "places": exponent},
        )


def validate_timezone(value: str) -> None:
    if value not in zoneinfo.available_timezones():
        raise ValidationError("%(value)s is not a valid IANA timezone.", params={"value": value})
