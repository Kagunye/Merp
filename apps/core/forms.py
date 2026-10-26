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


class CurrencyChoiceField(forms.ChoiceField):
    """A currency select that falls back to the book's own code.

    The widget always shows a choice, so a real browser submits one; a request
    that never mentions a currency still has to land somewhere honest, and that
    is the currency the ledger is kept in.
    """

    def __init__(self, *args, fallback="", **kwargs):
        kwargs.setdefault("required", False)
        self.fallback = fallback
        super().__init__(*args, **kwargs)

    def clean(self, value):
        return super().clean(value) or self.fallback


def pick_currency(form, company, field_name="currency", rated_only=True):
    """Offer the currencies this book can actually value a document in.

    The ledger is kept in the company's own currency, so a foreign document is
    translated at the day's rate before it is posted. A currency with no filed
    rate cannot be translated, so it is not offered for transaction documents --
    it would only fail on posting. Partners (a customer may well be billed in
    dollars as a matter of record) pass *rated_only=False* and see the full
    register instead.

    A saved document always keeps the code it was raised in, even if the rate
    has since been withdrawn: rewriting history would misstate the ledger.
    """
    if company is None or field_name not in form.fields:
        return
    from apps.accounting.currency import book_currency, currency_choices

    old = form.fields[field_name]
    if company and not form.instance.company_id:
        # The document's clean() values it against its own book, so the company
        # has to be on the instance before validation runs, not only before save.
        form.instance.company = company
    if rated_only:
        choices = currency_choices(company)
    else:
        from apps.accounting.currency import registered_currencies
        choices = [(c.code, f"{c.code} — {c.name}")
                   for c in registered_currencies(company)]
    # Only a saved document's own code counts as history. A new one arrives with
    # the model's default, which must not outvote the book it is being raised in.
    saved = "" if is_new(form) else (getattr(form.instance, field_name, "") or "")
    if saved and saved not in dict(choices):
        choices.append((saved, f"{saved} — as booked"))
    book = book_currency(company)
    form.fields[field_name] = CurrencyChoiceField(
        choices=choices,
        label=old.label,
        fallback=saved or book,
        initial=saved or book,
        widget=forms.Select(attrs=dict(old.widget.attrs)),
        help_text=f"Converted into {book} at the rate filed for the document date.",
    )


def company_users(company):
    """People who can be picked in a form: active members of `company` only."""
    from apps.accounts.models import User
    if company is None:
        return User.objects.none()
    return User.objects.filter(
        memberships__company=company, memberships__is_active=True, is_active=True,
    ).distinct().order_by("first_name", "last_name")
