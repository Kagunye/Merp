"""Shared form helpers."""

from django import forms

# A native date control only accepts ISO text. A Django date widget built without
# an explicit format renders the localized DATE_FORMAT instead -- "Oct. 17, 2026"
# -- which the browser silently throws away, leaving every prefilled and every
# edited date looking blank while the form itself holds a valid value.
ISO_DATE = "%Y-%m-%d"
ISO_DATETIME = "%Y-%m-%dT%H:%M"


def date_input(attrs=None):
    return forms.DateInput(attrs={"type": "date", **(attrs or {})}, format=ISO_DATE)


def datetime_input(attrs=None):
    return forms.DateTimeInput(
        attrs={"type": "datetime-local", **(attrs or {})}, format=ISO_DATETIME
    )


def is_new(form):
    """True while the bound instance has never been saved.

    Every model here uses a UUID primary key with a default, so an unsaved
    instance already carries a truthy pk -- `instance.pk` cannot be used to
    tell a create from an edit.
    """
    return form.instance.pk is None or form.instance._state.adding


def lock_currency(form, company, field_name="currency"):
    """Pin a transaction document to the currency its own company books in.

    There is no exchange-rate layer in this system: a document carrying a
    different code would post its raw figure straight into the base-currency
    ledger (a 1,000 USD invoice would settle 1,000 KES of accounts receivable).
    The field stays visible so the user can see what is being billed, but it
    can no longer be changed. Documents that already carry another currency
    keep it so that editing one does not silently rewrite its history.
    """
    if company is None or field_name not in form.fields:
        return
    field = form.fields[field_name]
    base = getattr(company, "currency", "") or "KES"
    saved = getattr(form.instance, field_name, "") if not is_new(form) else ""
    field.initial = saved or base
    field.disabled = True
    field.help_text = f"Fixed to the {company.name} book currency."
