"""Reading a number somebody typed into a box.

Python's Decimal accepts a good deal more than a bookkeeper means by a number:
"NaN", "sNaN", "Infinity" and "1e999999" all parse, and the first thing the
code does with them afterwards — compare, round or save — raises an error the
screen cannot show. Every quantity and price that reaches stock or the ledger
from a form field the browser does not police comes through here instead, so
the person at the keyboard is told what was wrong and nothing is half-written.

The limits mirror the columns the figure lands in: a quantity is held to four
places with eight digits before the point, money to two places with thirteen.
A figure the column cannot hold is refused here rather than by the database.
"""
import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError

# (digits before the point, places after it) for the columns these land in.
QUANTITY = (8, 4)      # DecimalField(max_digits=12, decimal_places=4)
UNIT_COST = (11, 4)    # DecimalField(max_digits=15, decimal_places=4)
MONEY = (13, 2)        # DecimalField(max_digits=15, decimal_places=2)

# What a typed number may look like: an optional sign, plain ASCII digits and
# at most one decimal point. Python's Decimal is far more generous — it reads
# "1_0" as ten (underscores are digit separators in Python source) and "１００"
# as a hundred (any Unicode digit) — and neither is something a bookkeeper
# meant to type into a money or quantity box.
PLAIN_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")


def typed_number(raw, label, shape=MONEY, *, allow_zero=False, allow_negative=False,
                 blank=None):
    """The Decimal a person meant, or a ValidationError that says what is wrong.

    ``blank`` is returned for an empty box; pass something else (or leave it
    None and check) depending on whether an empty box means "nothing" or is
    itself a mistake. Thousands separators are refused rather than guessed at:
    "1,250" is twelve hundred and fifty to one person and one and a quarter to
    another, and a stock count is the wrong place to find out which.
    """
    if raw is None:
        return blank
    text = str(raw).strip()
    if text == "":
        return blank
    if "," in text:
        raise ValidationError(
            f"{label} '{text}' has a comma in it. Type the number without "
            "thousands separators, for example 1250 or 12.5.")
    if "e" in text.lower() and any(ch.isdigit() for ch in text):
        # 1e2 is a hundred to a programmer and a typing slip to everyone else;
        # a money or quantity box takes the figure written out.
        raise ValidationError(
            f"{label} '{text}' is written as a power of ten. Type the number out in "
            "full, for example 100 or 12.5.")
    if not PLAIN_NUMBER.fullmatch(text):
        raise ValidationError(
            f"{label} '{text}' is not a number. Type it with the digits 0-9 and at most "
            "one decimal point, for example 1250 or 12.5.")
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{label} '{text}' is not a number.")
    if not value.is_finite():
        raise ValidationError(f"{label} '{text}' is not a number.")
    whole, places = shape
    # adjusted() is the power of ten of the leading digit, so it measures the
    # size without rounding — rounding 1e999999 is itself an error.
    if value and value.adjusted() >= whole:
        raise ValidationError(
            f"{label} {text} is too large: at most {whole} digits before the decimal point.")
    if value != value.quantize(Decimal(1).scaleb(-places)):
        raise ValidationError(
            f"{label} {text} has more than {places} decimal places.")
    if value < 0 and not allow_negative:
        raise ValidationError(f"{label} cannot be negative.")
    if value == 0 and not allow_zero:
        raise ValidationError(f"{label} must be more than zero.")
    return value


def fits(value, shape=MONEY):
    """Whether a worked-out figure (a quantity times a price) fits its column."""
    whole, _places = shape
    return not value or value.adjusted() < whole


def money(value):
    """A sum of money as a message prints it: 5,800.00, never 5800.000000000000.

    Totals worked out from four-place quantities and prices carry far more
    places than money has, and a flash message that prints them all tells the
    bookkeeper nothing except that the arithmetic was not rounded. The ledger
    holds cents, so the screen says cents.
    """
    try:
        return f"{Decimal(str(value or 0)).quantize(Decimal('0.01')):,.2f}"
    except (InvalidOperation, ValueError):
        return str(value)


def qty(value):
    """A counted quantity as a message prints it: 5, 2.5, never 5.0000."""
    try:
        text = format(Decimal(str(value or 0)).normalize(), "f")
    except (InvalidOperation, ValueError):
        return str(value)
    return text


def pick(queryset, raw):
    """The row a posted id names inside ``queryset``, or None.

    A primary key from a form is text the browser sent; "zzz" or an empty box
    is not an id, and asking the database for it raises instead of finding
    nothing. Callers treat None as "that is not one of yours".
    """
    if raw in (None, ""):
        return None
    try:
        return queryset.filter(pk=raw).first()
    except (ValueError, TypeError, ValidationError):
        return None


class PlainNumbersMixin:
    """Hold a form's number boxes to the rule typed_number() keeps.

    Django's DecimalField hands what was typed straight to Decimal, so "1_0"
    is saved as ten and "１００" (full-width digits) as a hundred. A form that
    mixes this in refuses any number box whose text is not plain ASCII digits
    with an optional sign and at most one decimal point, in the same words
    typed_number() uses, before the figure goes anywhere.
    """

    def _clean_fields(self):
        from django import forms

        super()._clean_fields()
        for name, field in self.fields.items():
            if field.disabled or not isinstance(field, (forms.DecimalField, forms.FloatField)):
                continue
            raw = self[name].data
            text = "" if raw is None else str(raw).strip()
            if text and not PLAIN_NUMBER.fullmatch(text) and name not in self._errors:
                self.add_error(
                    name,
                    f"'{text}' is not a number. Type it with the digits 0-9 and at most "
                    "one decimal point, for example 1250 or 12.5.")
