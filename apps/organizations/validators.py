"""Small validators shared by the company and user forms."""
from functools import lru_cache

from django.core.exceptions import ValidationError


@lru_cache(maxsize=1)
def _zones():
    from zoneinfo import available_timezones
    return frozenset(available_timezones())


def validate_timezone(value):
    """Refuse a timezone name the server cannot load.

    The field is free text, and a typo ("Africa/Nairobbi", "EAT", "GMT+3")
    is accepted by the database but blows up the first time anything tries
    to show a date in that zone. Only IANA names the server knows pass.
    """
    if value and value not in _zones():
        raise ValidationError(
            f"'{value}' is not a timezone this system knows. Use an IANA name "
            "such as Africa/Nairobi, Africa/Kampala or Europe/London.")
    return value
