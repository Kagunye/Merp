"""The part of a control account no document explains.

Accounts Receivable and Accounts Payable are control accounts: each is meant
to equal the open documents behind it (invoices less money on account for
receivables, open bills for payables). But not everything on them came from
a document. The opening balances a company brings in on its first day are
typed as one journal on 1300 and 2100, with no invoice or bill underneath;
so is any hand-made journal an accountant posts to a control account. A
report that adds up only the documents then says the company is owed nothing
while the balance sheet says 75,000.

Every screen that shows the receivable or payable total (the dashboard tiles,
Aged Debtors, Aged Creditors) reads the gap from here and shows it as its own
line, "Opening balance / unallocated", so its total equals the control
account and the reader can see how much of it is not yet tied to a document.
The gap is not aged: nothing says when it fell due.
"""
from decimal import Decimal

from django.db.models import Sum

from .models import JournalEntryLine

OPENING_LINE_LABEL = "Opening balance / unallocated"

# The control account types, and the side each one's balance is read from.
_SIDES = {"receivable": ("RECEIVABLE", 1), "payable": ("PAYABLE", -1)}


def control_balance(company, side):
    """What the receivable (debit) or payable (credit) control accounts hold.

    Every posted line on an account of that type counts, future-dated ones
    included, because the document side it is compared with counts every open
    document whatever its date.
    """
    account_type, sign = _SIDES[side]
    totals = JournalEntryLine.objects.filter(
        journal_entry__company=company, journal_entry__status="POSTED",
        account__account_type=account_type,
    ).aggregate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
    net = (totals["dr"] or Decimal("0")) - (totals["cr"] or Decimal("0"))
    return net * sign


def unallocated(company, side, documents_total):
    """The control balance less what the documents account for.

    Positive when the ledger holds more than the documents: opening balances
    owed by (or to) the company, a journal posted straight to the control
    account. `documents_total` is in the book's money, as the reports add it.
    """
    return control_balance(company, side) - Decimal(str(documents_total or 0))
