import zoneinfo

from django.core.exceptions import ValidationError

# ISO 4217 codes supported in the MVP, mapped to their minor-unit exponent
# (core.money). Currencies with three minor units (e.g. KWD, BHD) are
# excluded until they are needed (database design §7, open question 1).
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


def validate_timezone(value: str) -> None:
    if value not in zoneinfo.available_timezones():
        raise ValidationError("%(value)s is not a valid IANA timezone.", params={"value": value})
