"""What the receivables control account is made of, read from the documents.

Accounts Receivable (1300) holds two kinds of balance. Invoices owed by
customers sit on the debit side. Money customers paid that has not yet been
matched to an invoice — an over-payment, a deposit, a receipt nobody has
allocated — sits on the credit side, because the receipt credited AR for the
whole amount it took in. A report that adds up only the open invoices is
therefore not the receivable: it leaves the money on account out, and its
total disagrees with the control account by exactly that much.

Every customer-side report (Aged Debtors, the sales ageing, the receivables
home, the dashboard and the customer statement) reads both halves from here,
so they agree with each other and with the ledger.
"""
from decimal import Decimal

from apps.accounting.models import to_base

OPEN = ["SENT", "PARTIAL", "OVERDUE"]


def unapplied_amount(payment):
    """How much of a receipt, in its own money, no invoice has taken yet."""
    allocated = sum((a.allocated_amount for a in payment.allocations.all()), Decimal("0"))
    return max(payment.amount - allocated, Decimal("0"))


def unapplied_base(payment, unapplied=None):
    """That money in the book's currency, at the rate the receipt came in at.

    The receipt credited AR with the unmatched part at its own rate, and
    applying credit later releases it at the same rate (see
    gl_service.apply_customer_credit), so this is exactly what AR still holds
    for it.
    """
    if unapplied is None:
        unapplied = unapplied_amount(payment)
    return to_base(unapplied, payment.rate) if unapplied else Decimal("0")


def unapplied_receipts(company, customer=None, currency=None):
    """Live receipts with money still on account, as dicts.

    Each row: payment, customer, currency, amount (its own money) and base
    (the book's). Reversed receipts gave the money back and are not counted;
    receipts never posted never reached AR.
    """
    from .models import CustomerPayment
    qs = CustomerPayment.objects.filter(
        company=company, status="RECEIVED", journal_entry__isnull=False,
    ).select_related("customer").prefetch_related("allocations")
    if customer is not None:
        qs = qs.filter(customer=customer)
    if currency:
        qs = qs.filter(currency=currency)
    rows = []
    for payment in qs.order_by("payment_date", "reference"):
        left = unapplied_amount(payment)
        if left > 0:
            rows.append({
                "payment": payment, "customer": payment.customer,
                "currency": payment.currency, "amount": left,
                "base": unapplied_base(payment, left),
            })
    return rows


def open_invoice_total(company, customer=None):
    """Open invoices in the book's money, as every report adds them."""
    from .models import Invoice
    qs = Invoice.objects.filter(company=company, status__in=OPEN,
                                outstanding_amount__gt=Decimal("0"))
    if customer is not None:
        qs = qs.filter(customer=customer)
    return sum((inv.outstanding_base for inv in qs), Decimal("0"))


def unapplied_total(company, customer=None):
    return sum((row["base"] for row in unapplied_receipts(company, customer)), Decimal("0"))


def net_receivable(company, customer=None):
    """Open invoices less money on account: what AR 1300 says is owed."""
    return open_invoice_total(company, customer) - unapplied_total(company, customer)
