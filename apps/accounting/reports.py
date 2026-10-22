"""Ledger figures shared by the trial balance and balance sheet pages."""
from decimal import Decimal

from django.db.models import Sum

from .models import Account, JournalEntryLine

PROFIT_AND_LOSS_TYPES = ("REVENUE", "COST_OF_GOODS", "EXPENSE")


def _posted_lines(company, date_to=None, date_from=None):
    qs = JournalEntryLine.objects.filter(journal_entry__company=company, journal_entry__status="POSTED")
    if date_to:
        qs = qs.filter(journal_entry__posting_date__lte=date_to)
    if date_from:
        qs = qs.filter(journal_entry__posting_date__gte=date_from)
    return qs


def trial_balance(company, date_to=None, date_from=None):
    """Each account's net balance in its debit or credit column.

    Without `date_from` this is the true trial balance as at `date_to`;
    with it, the net movement over the period.
    """
    totals = {
        row["account"]: (row["dr"] or Decimal("0"), row["cr"] or Decimal("0"))
        for row in _posted_lines(company, date_to, date_from)
        .values("account").annotate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
    }
    rows, total_dr, total_cr = [], Decimal("0"), Decimal("0")
    for acc in Account.objects.filter(company=company, pk__in=totals.keys()).order_by("code"):
        dr, cr = totals[acc.pk]
        net = dr - cr
        if net == 0:
            continue
        debit, credit = (net, Decimal("0")) if net > 0 else (Decimal("0"), -net)
        rows.append({"account": acc, "debit": debit, "credit": credit})
        total_dr += debit
        total_cr += credit
    return {
        "rows": rows,
        "total_debit": total_dr,
        "total_credit": total_cr,
        "is_balanced": abs(total_dr - total_cr) < Decimal("0.01"),
    }


def unclosed_profit(company, date_to=None):
    """Income less costs and expenses not yet closed to retained earnings."""
    agg = _posted_lines(company, date_to).filter(
        account__account_type__in=PROFIT_AND_LOSS_TYPES
    ).aggregate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
    return (agg["cr"] or Decimal("0")) - (agg["dr"] or Decimal("0"))
