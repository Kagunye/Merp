"""Reading a foreign balance at today's rate instead of the day it was raised.

A dollar receivable booked in March is worth 130 shillings in March, and the
ledger says so forever — that is what the booked rate is for. But a balance
sheet drawn on the last day of the month is a statement about that day, and if
the dollar now buys 135 the receivable is worth 135. Nothing has been earned or
lost in cash yet, so the difference is *unrealised*: it is posted to its own
pair of accounts, leaving the receivable and payable control accounts — and the
invoice book they are reconciled against — exactly as they were.

The book carries one revaluation at a time. Posting a new one first reverses
the one it replaces, so a period end that is redrawn cannot pile up, and a rate
that comes back round to where it started leaves the balance sheet as though
nothing had happened.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.accounting.currency import book_currency, to_base
from apps.accounting.models import ExchangeRate, JournalEntry, period_for

# Where an unrealised difference is parked, and where it is reported.
REVALUATION = "EXCHANGE_REVALUATION"
RECEIVABLE_ACCOUNT = "1350"
PAYABLE_ACCOUNT = "2150"
GAIN_ACCOUNT = "4320"
LOSS_ACCOUNT = "7310"

Z = Decimal("0")


def live_revaluation(company):
    """The revaluation the book is currently sitting on, if any."""
    return (
        JournalEntry.objects.filter(
            company=company, entry_type="REVALUATION",
            source_document_type=REVALUATION, status="POSTED",
            reversed_by__isnull=True, reversal_of__isnull=True,
        ).order_by("-posting_date").first()
    )


def _documents(company, model, date_field, book, as_of):
    """Open, posted documents of this model that are kept in a foreign money.

    Only documents raised on or before the day being reported count: a
    receivable that did not exist on the period end cannot be revalued at it.
    """
    return list(
        model.objects.filter(company=company, journal_entry__isnull=False)
        .exclude(status__in=["DRAFT", "CANCELLED"])
        .exclude(currency=book)
        .filter(outstanding_amount__gt=Z, **{f"{date_field}__lte": as_of})
        .select_related("journal_entry")
    )


def positions(company, as_of):
    """Every foreign balance still open at `as_of`, read at the day's rate.

    Returns one row per currency, holding the receivable side and the payable
    side, and a list of currencies that cannot be valued because no rate has
    been filed. Amounts are Decimal: foreign, carried (booked at the document's
    own rate), revalued, and the difference between the last two.
    """
    from apps.procurement.models import Bill
    from apps.sales.models import Invoice

    book = book_currency(company)
    rows = {}

    def accumulate(documents, key):
        for document in documents:
            code = document.currency.upper()
            row = rows.setdefault(code, _blank_row(code))
            row[key["foreign"]] += Decimal(str(document.outstanding_amount))
            row[key["carried"]] += Decimal(str(document.outstanding_base))
            row["documents"] += 1

    accumulate(_documents(company, Invoice, "invoice_date", book, as_of),
               {"foreign": "ar_foreign", "carried": "ar_carried"})
    accumulate(_documents(company, Bill, "bill_date", book, as_of),
               {"foreign": "ap_foreign", "carried": "ap_carried"})

    missing, valued = [], []
    for code, row in sorted(rows.items()):
        try:
            rate = ExchangeRate.rate_for(company, code, as_of)
        except ValidationError:
            missing.append(code)
            continue
        row["rate"] = rate
        row["ar_revalued"] = to_base(row["ar_foreign"], rate)
        row["ap_revalued"] = to_base(row["ap_foreign"], rate)
        row["ar_difference"] = row["ar_revalued"] - row["ar_carried"]
        row["ap_difference"] = row["ap_revalued"] - row["ap_carried"]
        row["difference"] = row["ar_difference"] - row["ap_difference"]
        valued.append(row)
    return {"as_of": as_of, "book": book, "rows": valued, "missing": missing}


def _blank_row(code):
    return {
        "code": code, "documents": 0, "rate": None,
        "ar_foreign": Z, "ar_carried": Z, "ar_revalued": Z, "ar_difference": Z,
        "ap_foreign": Z, "ap_carried": Z, "ap_revalued": Z, "ap_difference": Z,
        "difference": Z,
    }


def summary(position):
    """The two numbers a revaluation entry is made of, in the book's money."""
    return (
        sum((row["ar_difference"] for row in position["rows"]), Z),
        sum((row["ap_difference"] for row in position["rows"]), Z),
    )


def _account(company, code, name, account_type, parent_code):
    """Find the revaluation account, making it if this book has none yet.

    Companies founded before this feature simply have no such code; refusing to
    revalue until somebody creates one by hand would punish exactly the people
    the feature is for, so the account is created on first use — the same way
    the exchange gain and loss accounts were retro-fitted.
    """
    from apps.accounting.models import Account

    account = Account.objects.filter(company=company, code=code).first()
    if account:
        return account
    parent = Account.objects.filter(company=company, code=parent_code).first()
    return Account.objects.create(
        company=company, code=code, name=name,
        account_type=account_type, parent=parent,
        description="Unrealised exchange difference, reversed at the next revaluation.",
    )


@transaction.atomic
def post_revaluation(company, as_of, user=None):
    """Value the open foreign balances at `as_of` and book the difference.

    Returns (entry, position). The entry is None when the balances need nothing:
    either there are no open foreign documents, or the rate has not moved since
    the book was last revalued.
    """
    from apps.accounting.gl_service import _cr, _dr, _new_je

    position = positions(company, as_of)
    if position["missing"]:
        raise ValidationError(
            "Cannot revalue " + ", ".join(position["missing"]) +
            f" on {as_of:%d %b %Y}: no exchange rate has been filed for that day.")

    receivable, payable = summary(position)
    if not receivable and not payable:
        previous = live_revaluation(company)
        if previous and previous.posting_date != as_of:
            # The balances net to nothing at this date but an older revaluation
            # is still sitting on the book — it has to come off, or the balance
            # sheet keeps a difference that no longer exists.
            previous.reverse(user=user, posting_date=as_of)
        return None, position

    previous = live_revaluation(company)
    if previous:
        if previous.posting_date > as_of:
            raise ValidationError(
                f"The book is already revalued to {previous.posting_date:%d %b %Y}, "
                f"which is after {as_of:%d %b %Y}.")
        previous.reverse(
            user=user,
            # A redraw of the same period end takes the old entry out with its
            # own date, so the period nets to exactly the new figure.
            posting_date=None if previous.posting_date == as_of else as_of,
        )

    period = period_for(company, as_of)
    entry = _new_je(
        company, "JE-FX-", "REVALUATION", as_of,
        f"Exchange revaluation at {as_of:%d %b %Y} — "
        f"{len(position['rows'])} currency(ies), "
        f"receivables {receivable:,.2f}, payables {payable:,.2f}",
    )
    entry.source_document_type = REVALUATION
    entry.source_document_id = str(period.pk) if period else ""
    entry.save(update_fields=[
        "source_document_type", "source_document_id", "updated_at"])

    gain = _account(company, GAIN_ACCOUNT, "Unrealised Exchange Gain",
                    "REVENUE", "4300")
    loss = _account(company, LOSS_ACCOUNT, "Unrealised Exchange Loss",
                    "EXPENSE", "7000")
    revalued_receivables = _account(
        company, RECEIVABLE_ACCOUNT, "FX Revaluation — Receivables",
        "ASSET", "1000")
    revalued_payables = _account(
        company, PAYABLE_ACCOUNT, "FX Revaluation — Payables",
        "LIABILITY", "2000")

    if receivable > 0:
        _dr(entry, revalued_receivables, receivable,
            f"Receivables worth more at {as_of:%d %b %Y}")
        _cr(entry, gain, receivable, "Unrealised exchange gain")
    elif receivable < 0:
        _cr(entry, revalued_receivables, -receivable,
            f"Receivables worth less at {as_of:%d %b %Y}")
        _dr(entry, loss, -receivable, "Unrealised exchange loss")

    if payable > 0:
        _dr(entry, loss, payable, "Unrealised exchange loss")
        _cr(entry, revalued_payables, payable,
            f"Payables worth more at {as_of:%d %b %Y}")
    elif payable < 0:
        _dr(entry, revalued_payables, -payable,
            f"Payables worth less at {as_of:%d %b %Y}")
        _cr(entry, gain, -payable, "Unrealised exchange gain")

    entry.post(user=user)
    return entry, position
