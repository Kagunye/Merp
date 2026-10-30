"""Small validators shared by the company, user and people forms."""
import datetime
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


# The earliest date any business record here can carry. Nothing this system
# tracks (a hire, an interview, a project, an opening) happened before it; a
# date earlier than this is a typo in the year -- 0202 for 2020 -- and left in,
# it skews every report that sorts or ages by date.
EARLIEST_BUSINESS_DATE = datetime.date(1950, 1, 1)

# The youngest a person on the books can be.
MINIMUM_AGE_YEARS = 14


def earliest_date_error(value, what="This date"):
    """A message if `value` (a date or datetime) is before 1950, else None."""
    if value is None:
        return None
    day = value.date() if isinstance(value, datetime.datetime) else value
    if day < EARLIEST_BUSINESS_DATE:
        return (f"{what} is before {EARLIEST_BUSINESS_DATE.year}, which can't be right. "
                "Check the year.")
    return None


def birth_date_error(dob):
    """A message if `dob` makes the person younger than MINIMUM_AGE_YEARS, else None.

    A date of birth in the future, or one that makes an employee a toddler,
    is always a slip of the year field; left in, it flows into age-based
    reports and statutory deductions.
    """
    if dob is None:
        return None
    today = datetime.date.today()
    try:
        latest = today.replace(year=today.year - MINIMUM_AGE_YEARS)
    except ValueError:  # 29 February
        latest = today.replace(year=today.year - MINIMUM_AGE_YEARS, day=28)
    if dob > latest:
        return (f"A date of birth must make the person at least {MINIMUM_AGE_YEARS} "
                "years old. Check the year.")
    if dob.year < 1900:
        return "This date of birth is before 1900. Check the year."
    return None
