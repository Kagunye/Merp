"""Business guardrails shared by the module forms and views.

Each helper either returns nothing or raises / records a ValidationError
with a message that tells the person what to do instead.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError

# How far from today a business date may sit before we call it a typo.
MAX_YEARS_BACK = 10
MAX_DAYS_AHEAD = 366


def positive(value, label="Amount"):
    """`value` must be greater than zero."""
    if value is not None and value <= 0:
        raise ValidationError(f"{label} must be greater than zero.")
    return value


def not_negative(value, label="Amount"):
    if value is not None and value < 0:
        raise ValidationError(f"{label} can't be negative.")
    return value


def percent(value, label="Discount"):
    if value is not None and not (0 <= value <= 100):
        raise ValidationError(f"{label} must be between 0% and 100%.")
    return value


def plausible_date(value, label="Date", *, ahead_days=MAX_DAYS_AHEAD, years_back=MAX_YEARS_BACK):
    """Catch year typos (2099, 1990) without blocking normal back-dating."""
    if value is None:
        return value
    today = datetime.date.today()
    if value > today + datetime.timedelta(days=ahead_days):
        raise ValidationError(f"{label} {value:%d %b %Y} is too far in the future — check the year.")
    if value < today.replace(year=today.year - years_back):
        raise ValidationError(f"{label} {value:%d %b %Y} is more than {years_back} years ago — check the year.")
    return value


def not_in_future(value, label="Date"):
    if value is not None and value > datetime.date.today():
        raise ValidationError(f"{label} can't be in the future.")
    return value


def order(form, start, end, message=None):
    """Form-level: `end` field must not be before `start` field."""
    a, b = form.cleaned_data.get(start), form.cleaned_data.get(end)
    if a and b and b < a:
        form.add_error(end, message or f"{form.fields[end].label} can't be before {form.fields[start].label.lower()}.")


def unique(form, model, company, field="reference", label=None, **extra):
    """Form-level: `field` must be unique inside `company` (case-insensitive)."""
    value = form.cleaned_data.get(field)
    if not value or company is None:
        return
    qs = model._default_manager.filter(company=company, **{f"{field}__iexact": value}, **extra)
    if not form.instance._state.adding:
        qs = qs.exclude(pk=form.instance.pk)
    if qs.exists():
        form.add_error(field, f"{label or form.fields[field].label} “{value}” is already used in this company.")


def decimal_field(value, label="Amount"):
    """Reject NaN / Infinity, which DecimalField lets through in some paths."""
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValidationError(f"{label} must be a number.")
    return value


def parse_amount(raw, label="Amount", *, allow_zero=False):
    """Parse a free-text POST amount for function views. Raises ValidationError."""
    try:
        value = Decimal(str(raw).replace(",", "").strip())
    except Exception:
        raise ValidationError(f"{label} must be a number.")
    if not value.is_finite():
        raise ValidationError(f"{label} must be a number.")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValidationError(f"{label} must be greater than zero.")
    return value


def own_record(user, *owners):
    """True when `user` is one of `owners` (a User, or an Employee linked to a user)."""
    for owner in owners:
        if owner is None:
            continue
        uid = getattr(owner, "user_id", None) if not hasattr(owner, "email") else owner.pk
        if uid and uid == user.pk:
            return True
    return False


def employee_of(user, company):
    """The Employee record for `user` in `company`, or None."""
    from apps.hr.models import Employee
    if company is None or not getattr(user, "is_authenticated", False):
        return None
    return Employee.objects.filter(company=company, user=user).first()


class GuardedFormMixin:
    """Adds NaN/Infinity rejection to every DecimalField on the form."""

    def clean(self):
        data = super().clean()
        for name, field in self.fields.items():
            if isinstance(field, forms.DecimalField) and isinstance(data.get(name), Decimal):
                if not data[name].is_finite():
                    self.add_error(name, "Enter a number.")
        return data
