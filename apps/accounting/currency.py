"""Turning a foreign document into book-currency money.

The ledger holds one currency: the company's own. A document can be written in
another, but it must be translated before it reaches the journal, and the
translation has to be provable years later — which is why the rate used is
stamped on the document rather than looked up from a live feed each time.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError

from apps.accounting.models import Currency, ExchangeRate, to_base

# Where a document's foreign total has its matching book value stored.
BASE_FIELDS = {"total_amount": "total_amount_base", "amount": "amount_base"}


def book_currency(company):
    return (getattr(company, "currency", "") or "KES").upper()


def registered_currencies(company):
    """Every currency the book knows, with its own code always in the list.

    The company's currency is the ledger itself, so it is offered even if the
    shared register has never heard of it — a book could not function without
    its own code.
    """
    book = book_currency(company)
    currencies = list(Currency.objects.filter(is_active=True).order_by("code"))
    if not any(c.code == book for c in currencies):
        currencies.insert(0, Currency(code=book, name=f"{book} book currency"))
    return currencies


def translatable_currencies(company):
    """Codes the company may raise a document in.

    Its own currency always, plus any currency with at least one filed rate:
    a currency without a rate is not a choice, it is a dead end waiting to
    happen on the posting screen.
    """
    book = book_currency(company)
    codes = set(
        ExchangeRate.objects.filter(company=company)
        .values_list("currency__code", flat=True)
    )
    codes.add(book)
    return [c for c in registered_currencies(company) if c.code in codes]


def currency_choices(company):
    """Selectable document currencies, the book's own listed first."""
    book = book_currency(company)
    currencies = translatable_currencies(company)
    ordered = [c for c in currencies if c.code == book] + \
              [c for c in currencies if c.code != book]
    return [(c.code, f"{c.code} — {c.name}") for c in ordered]


def document_rate(instance, on_date=None):
    """The rate this document's amounts must be multiplied by.

    A booked rate always wins: it is what the document was worth that day. With
    nothing booked, the book's own currency is worth one of itself, and any
    other code has to have been filed by the date the document falls due in the
    ledger — otherwise the amount would be a guess.
    """
    code = (getattr(instance, "currency", "") or "").upper()
    booked = getattr(instance, "exchange_rate", None)
    if booked:
        return Decimal(str(booked))
    company = instance.company
    if not code or code == book_currency(company):
        return Decimal("1")
    if on_date is None:
        raise ValidationError(
            f"Cannot value a {code} document without knowing its date.")
    return ExchangeRate.rate_for(company, code, on_date)


def book_value(document, amount_field="total_amount", base_field="total_amount_base"):
    """What a document's money is worth in the book, in Decimal.

    A booked figure is used when the document carries one. Documents raised
    before the book stored base amounts read as zero, so those are converted at
    the document's own date rather than reported as nothing.
    """
    stored = getattr(document, base_field, None)
    if stored:
        return Decimal(str(stored))
    foreign = Decimal(str(getattr(document, amount_field, 0) or 0))
    if not foreign:
        return Decimal("0")
    for date_field in ("invoice_date", "bill_date", "payment_date"):
        on_date = getattr(document, date_field, None)
        if on_date:
            return to_base(foreign, document_rate(document, on_date))
    return to_base(foreign, document_rate(document, None))


def stamp_document(instance, on_date):
    """Book the day's rate and the book-currency value onto the document.

    Called from each document's clean(), so an invoice that cannot be valued is
    refused at the form instead of quietly posting a wrong number.
    """
    if "exchange_rate" not in [f.name for f in instance._meta.fields]:
        return
    if getattr(instance, "company_id", None) is None:
        # A form can validate a document before the view has attached a company
        # to it. There is no book to convert into yet, so nothing is stamped —
        # the rate is resolved again at posting time, which is where it matters.
        return
    rate = document_rate(instance, on_date)
    instance.exchange_rate = rate
    for field, base_field in BASE_FIELDS.items():
        if hasattr(instance, base_field):
            setattr(instance, base_field,
                    to_base(getattr(instance, field) or Decimal("0"), rate))


def spread(foreign_amounts, rate, total_base):
    """Convert a list of amounts so that the parts still add up to the whole.

    Rounding each line on its own can leave a cent or two between the sum of
    the lines and the document total; the difference is put on the largest line
    rather than left to unbalance the journal.
    """
    bases = [to_base(amount, rate) for amount in foreign_amounts]
    residue = Decimal(str(total_base)) - sum(bases, Decimal("0"))
    if residue and bases:
        heaviest = max(range(len(foreign_amounts)),
                       key=lambda i: abs(foreign_amounts[i]))
        bases[heaviest] += residue
    return bases
