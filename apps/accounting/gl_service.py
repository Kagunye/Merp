"""
GL Posting Service — creates and posts balanced JournalEntry records
for every transaction type in MERP.

Conventions
-----------
* All post_* functions must be called inside a view's own db transaction
  (or wrap themselves atomically where needed).
* _get_account_safe() never raises — callers handle None gracefully.
* Each post_* is idempotent: if the source document already links a
  journal_entry, the existing entry is returned without creating a duplicate.
* We fall back through a chain: product-specific account → default code
  → first account of that type.  This means GL posting works even when the
  chart of accounts is sparse.
"""
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.accounting.models import (
    Account, INCOME_STATEMENT_TYPES, JournalEntry, JournalEntryLine, to_base,
)
from apps.accounting.currency import book_currency, spread
from apps.core.models import SequenceCounter


# ─────────────────────────── low-level helpers ──────────────────────────────

def _get_account_safe(company, *codes: str) -> Optional[Account]:
    """
    Try each code in order; return the first active Account found, or None.
    """
    for code in codes:
        if not code:
            continue
        try:
            return Account.objects.get(company=company, code=code, is_active=True)
        except Account.DoesNotExist:
            pass
    return None


def _get_account_by_type(company, *types) -> Optional[Account]:
    """Return the first active Account matching any of the given account_types."""
    return (
        Account.objects
        .filter(company=company, account_type__in=types, is_active=True)
        .order_by("code")
        .first()
    )


def ensure_account(company, code, name, account_type, parent_code) -> Account:
    """The account a posting needs, made on first use if this book has none.

    A company founded before a feature exists has nowhere to put its figures,
    and refusing the posting punishes the bookkeeper for something the software
    changed. The account is created rather than the entry abandoned — the way
    the exchange and revaluation accounts were retro-fitted.
    """
    account = Account.objects.filter(company=company, code=code).first()
    if account:
        return account
    parent = (Account.objects.filter(company=company, code=parent_code).first()
              if parent_code else None)
    return Account.objects.create(
        company=company, code=code, name=name,
        account_type=account_type, parent=parent,
    )


def _new_je(company, ref_prefix: str, entry_type: str, date, description: str = "",
            source_currency: str = "", exchange_rate: Optional[Decimal] = None) -> JournalEntry:
    """Create a DRAFT JournalEntry with a sequenced reference."""
    ref = SequenceCounter.next_number(company, ref_prefix)
    return JournalEntry.objects.create(
        company=company,
        reference=ref,
        entry_type=entry_type,
        posting_date=date,
        status="DRAFT",
        notes=description,
        source_currency=(source_currency or "").upper(),
        exchange_rate=exchange_rate,
    )


def _dr(je: JournalEntry, account: Account, amount: Decimal, description: str = "") -> None:
    """Add a debit line (skips if amount <= 0 or account is None)."""
    if account and amount and amount > 0:
        JournalEntryLine.objects.create(
            journal_entry=je,
            account=account,
            debit_amount=amount,
            credit_amount=Decimal("0"),
            description=description,
        )


def _cr(je: JournalEntry, account: Account, amount: Decimal, description: str = "") -> None:
    """Add a credit line (skips if amount <= 0 or account is None)."""
    if account and amount and amount > 0:
        JournalEntryLine.objects.create(
            journal_entry=je,
            account=account,
            debit_amount=Decimal("0"),
            credit_amount=amount,
            description=description,
        )


# ────────────────────────── foreign currency ────────────────────────────────

def _fx_accounts(company):
    """Where a realised gain and a realised loss are booked."""
    gain = _get_account_safe(company, "4310") or _get_account_by_type(company, "REVENUE")
    loss = _get_account_safe(company, "7300") or _get_account_by_type(company, "EXPENSE")
    return gain, loss


def _book_exchange_difference(je: JournalEntry, company, difference: Decimal,
                              description: str) -> None:
    """Push a settlement difference onto the exchange accounts.

    A positive difference means the cash was worth more, in book currency, than
    the balance it cleared — that is a gain. Zero means the rate never moved
    and nothing is booked. Callers pass it from the money side, so a payment
    out of the door is simply the negative of a receipt.
    """
    if not difference:
        return
    gain, loss = _fx_accounts(company)
    if difference > 0:
        _cr(je, gain, difference, description)
    else:
        _dr(je, loss, -difference, description)


# ──────────────────────────── Invoice GL ────────────────────────────────────

def _invoice_targets(invoice, default_revenue, default_vat):
    """The revenue and tax lines an invoice of this shape is worth.

    Stated in the invoice's own currency; the caller converts the whole set at
    the invoice's booked rate. Shared with the credit note so a return can never
    book against different accounts from the sale it answers.
    """
    targets = []
    for line in invoice.lines.select_related(
        "product", "product__income_account", "tax_rate", "tax_rate__account"
    ):
        revenue_acct = (
            (line.product and line.product.income_account) or default_revenue
        )
        line_desc = line.description or (line.product.name if line.product else "Item")
        targets.append((revenue_acct, line.line_total, line_desc))

        if line.tax_amount and line.tax_amount > 0:
            tax_acct = (
                (line.tax_rate and line.tax_rate.account) or default_vat
            )
            targets.append((tax_acct, line.tax_amount, f"VAT: {invoice.reference}"))

    if not targets:
        # A header-only invoice still owes somebody its revenue.
        targets = [(default_revenue, invoice.total_amount,
                    f"Revenue: {invoice.reference}")]
    return targets

@transaction.atomic
def post_invoice(invoice, user=None) -> JournalEntry:
    """
    Post a customer invoice to the general ledger.

    Entries created
    ---------------
    DR  Accounts Receivable (1300)          invoice.total_amount
    CR  Sales Revenue per line              line.line_total  (product.income_account or 4100)
    CR  VAT Payable per taxed line          line.tax_amount  (tax_rate.account or 2200)

    Every figure is in the book currency. A foreign invoice is translated at the
    rate booked on it, and the rounding difference between the lines and the
    total lands on the largest line so the entry still balances.

    Parameters
    ----------
    invoice : sales.Invoice instance (with lines pre-saved)
    user    : accounts.User (posted_by)

    Returns the JournalEntry.
    Raises ValueError if essential accounts are missing.
    """
    if invoice.journal_entry_id:
        return invoice.journal_entry

    company = invoice.company

    ar_account = (
        _get_account_safe(company, "1300")
        or _get_account_by_type(company, "RECEIVABLE")
    )
    default_revenue = (
        _get_account_safe(company, "4100", "4200")
        or _get_account_by_type(company, "REVENUE")
    )
    default_vat = (
        _get_account_safe(company, "2200")
        or _get_account_by_type(company, "TAX")
    )

    if not ar_account:
        raise ValueError(
            "GL posting failed: Accounts Receivable account (code 1300) not found."
        )
    if not default_revenue:
        raise ValueError(
            "GL posting failed: Sales Revenue account (code 4100) not found."
        )

    rate = invoice.rate
    total_base = invoice.total_amount_base or to_base(invoice.total_amount, rate)

    je = _new_je(
        company, "JE-INV-", "INVOICE", invoice.invoice_date,
        f"Invoice {invoice.reference} — {invoice.customer.name}",
        source_currency=invoice.currency, exchange_rate=rate,
    )

    # Per-line revenue and VAT, all stated in the book's own currency.
    targets = _invoice_targets(invoice, default_revenue, default_vat)

    _dr(je, ar_account, total_base, f"AR: {invoice.reference}")
    bases = spread([t[1] for t in targets], rate, total_base)
    for (account, _foreign, desc), base in zip(targets, bases):
        _cr(je, account, base, desc)

    je.post(user=user, document="invoice")

    # Link back to the invoice and mark it as SENT
    from apps.sales.models import Invoice as _Invoice
    _Invoice.objects.filter(pk=invoice.pk).update(
        journal_entry=je,
        status="SENT",
    )
    invoice.journal_entry = je
    invoice.status = "SENT"
    return je


# ──────────────────────────── Credit notes ──────────────────────────────────

@transaction.atomic
def post_credit_note(note, user=None) -> JournalEntry:
    """Post a credit note: the invoice's money flowing back the other way.

    DR  Sales Revenue (per line)           what the invoice credited
    DR  VAT Payable (per taxed line)       the tax charged on it
    CR  Accounts Receivable (1300)         gross credited

    The original entry is left exactly as it was. A credit note is a second
    document with its own date and its own entry, and the two net to nothing in
    the account — which is how a mistake is corrected in a book that is never
    rewritten.
    """
    if note.journal_entry_id:
        return note.journal_entry

    company = note.company
    ar_account = (_get_account_safe(company, "1300")
                  or _get_account_by_type(company, "RECEIVABLE"))
    default_revenue = (_get_account_safe(company, "4100", "4200")
                       or _get_account_by_type(company, "REVENUE"))
    default_vat = (_get_account_safe(company, "2200")
                   or _get_account_by_type(company, "TAX"))
    if not (ar_account and default_revenue):
        raise ValueError("GL posting failed: receivables or revenue account missing.")

    rate = note.rate
    total_base = note.total_amount_base or to_base(note.total_amount, rate)

    je = _new_je(
        company, "JE-CN-", "CREDIT_NOTE", note.invoice_date,
        f"Credit note {note.reference} — {note.customer.name}",
        source_currency=note.currency, exchange_rate=rate,
    )
    targets = _invoice_targets(note, default_revenue, default_vat)
    bases = spread([t[1] for t in targets], rate, total_base)
    for (account, _foreign, desc), base in zip(targets, bases):
        _dr(je, account, base, desc)
    _cr(je, ar_account, total_base, f"AR credit: {note.reference}")

    je.post(user=user, document="credit note")

    from apps.sales.models import Invoice as _Invoice
    _Invoice.objects.filter(pk=note.pk).update(journal_entry=je, status="CREDIT_NOTE")
    note.journal_entry = je
    note.status = "CREDIT_NOTE"
    return je


def _debit_note_targets(note, inventory_acct, vat_input_acct):
    """The accounts a debit note credits, and the money those amounts are in.

    A return answers a bill, so it has to credit the accounts that bill charged
    — stock, the received-not-invoiced accrual, VAT input — in the proportions
    the bill itself set. Working them out from the note's own header would
    invent accounts the purchase never touched, and the pair of entries would
    leave a residue nobody can explain. The original's posted charge lines are
    therefore scaled to the note's share of the bill, and returned already in
    the book's money, hence a rate of one. With no posted bill to mirror, the
    note falls back to being read as a bill in reverse.
    """
    original = note.original_bill
    entry = getattr(original, "journal_entry", None) if original else None
    if entry is None or entry.status != "POSTED":
        return _bill_targets(note, inventory_acct, vat_input_acct), note.rate

    lines = list(entry.lines.filter(debit_amount__gt=Decimal("0"))
                 .select_related("account").order_by("account__code", "id"))
    note_base = note.total_amount_base or to_base(note.total_amount, note.rate)
    billed = sum((line.debit_amount for line in lines), Decimal("0"))
    if not lines or billed <= 0 or note_base <= 0:
        return _bill_targets(note, inventory_acct, vat_input_acct), note.rate

    share = note_base / billed
    targets = [(line.account,
                (line.debit_amount * share).quantize(Decimal("0.01")),
                f"Debit note {note.reference}: {line.description or line.account.name}")
               for line in lines]
    return targets, Decimal("1")


@transaction.atomic
def post_debit_note(note, user=None) -> JournalEntry:
    """Post a supplier debit note: goods back, or a price the supplier reduced.

    DR  Accounts Payable (2100)            gross debited
    CR  whatever the bill being answered charged

    A return has to credit the accounts the purchase was charged to — stock, or
    the goods-received accrual, or VAT input — otherwise the pair of entries
    leaves a residue nobody can explain. So the note is mirrored off the
    original bill's own debit lines, shrunk to the note's share of it.
    """
    if note.journal_entry_id:
        return note.journal_entry

    company = note.company
    ap_account = (_get_account_safe(company, "2100")
                  or _get_account_by_type(company, "PAYABLE"))
    inventory_acct = (_get_account_safe(company, "1400")
                      or _get_account_by_type(company, "ASSET"))
    vat_input_acct = _get_account_safe(company, "2210")
    if not (ap_account and inventory_acct):
        raise ValueError("GL posting failed: payables or inventory account missing.")

    rate = note.rate
    total_base = note.total_amount_base or to_base(note.total_amount, rate)

    je = _new_je(
        company, "JE-DN-", "DEBIT_NOTE", note.bill_date,
        f"Debit note {note.reference} — {note.supplier.name}",
        source_currency=note.currency, exchange_rate=rate,
    )
    targets, spread_rate = _debit_note_targets(note, inventory_acct, vat_input_acct)
    bases = spread([t[1] for t in targets], spread_rate, total_base)
    _dr(je, ap_account, total_base, f"AP debit: {note.reference}")
    for (account, _amount, desc), base in zip(targets, bases):
        _cr(je, account, base, desc)

    je.post(user=user, document="debit note")

    from apps.procurement.models import Bill as _Bill
    _Bill.objects.filter(pk=note.pk).update(journal_entry=je, status="DEBIT_NOTE")
    note.journal_entry = je
    note.status = "DEBIT_NOTE"
    return je


# ──────────────────────── Customer Payment GL ────────────────────────────────

@transaction.atomic
def post_customer_payment(payment, user=None, invoice_allocations=None) -> JournalEntry:
    """
    Post a customer payment to the general ledger and auto-allocate invoices.

    Entries created
    ---------------
    DR  Bank / Cash account                payment.amount  (bank_account.gl_account or 1200/1100)
    CR  Accounts Receivable (1300)         payment.amount

    The bank side is the receipt at today's rate; the receivable is cleared at
    the rate the invoice was raised at. The difference between the two is the
    realised exchange gain or loss and is booked to 4310 / 7300.

    Invoice allocation
    ------------------
    If *invoice_allocations* is a list of (Invoice, Decimal) pairs, those are
    used.  Otherwise the service allocates oldest-first against outstanding
    invoices for the same customer.

    Returns the JournalEntry.
    """
    if payment.journal_entry_id:
        return payment.journal_entry

    company = payment.company

    # Debit account: prefer the linked bank account's GL account
    if payment.bank_account_id and payment.bank_account.gl_account_id:
        debit_acct = payment.bank_account.gl_account
    else:
        # Notes handed over the counter belong to the drawer and mobile money to
        # the bank: putting cash receipts in the bank account makes the two
        # impossible to reconcile against each other at the end of the day.
        codes = (("1100", "1200") if (payment.payment_method or "").upper() == "CASH"
                 else ("1200", "1100"))
        debit_acct = (
            _get_account_safe(company, *codes)
            or _get_account_by_type(company, "BANK", "CASH")
        )

    ar_account = (
        _get_account_safe(company, "1300")
        or _get_account_by_type(company, "RECEIVABLE")
    )

    if not debit_acct:
        raise ValueError(
            "GL posting failed: Bank/Cash account not found for payment."
        )
    if not ar_account:
        raise ValueError(
            "GL posting failed: Accounts Receivable account (code 1300) not found."
        )

    from apps.sales.models import Invoice as _Invoice, PaymentAllocation

    want = (payment.currency or book_currency(company)).upper()
    rate = payment.rate
    bank_base = payment.amount_base or to_base(payment.amount, rate)

    # A settlement clears the receivable at the value the invoice was booked at;
    # the gap between that and the cash actually received is realised exchange.
    allocations = []
    if invoice_allocations is not None:
        for inv, alloc_amount in invoice_allocations:
            if alloc_amount and alloc_amount > 0:
                allocations.append(
                    (inv, alloc_amount, to_base(alloc_amount, inv.rate)))
    else:
        outstanding = (
            _Invoice.objects
            .filter(
                company=company,
                customer=payment.customer,
                status__in=["SENT", "PARTIAL", "OVERDUE"],
            )
            # A dollar receipt must not quietly settle a shilling invoice: the
            # amounts only mean anything when both sides count the same money.
            .filter(Q(currency__iexact=want) | Q(currency=""))
            .order_by("invoice_date")
            .select_for_update()
        )
        remaining = payment.amount
        for inv in outstanding:
            if remaining <= 0:
                break
            allocate = min(remaining, inv.outstanding_amount)
            if allocate > 0:
                allocations.append((inv, allocate, to_base(allocate, inv.rate)))
                remaining -= allocate

    ar_base = sum((base for _inv, _amt, base in allocations), Decimal("0"))
    left = payment.amount - sum((amt for _inv, amt, _b in allocations), Decimal("0"))
    if left > 0:
        # Money the receipt did not settle is not a gain made on the rate: it is
        # still the customer's money resting on their account, so it is credited
        # to receivables too. The exchange difference is then only the gap
        # between the rate the cash came in at and the rate the settled paper
        # was booked at, which is the part that is genuinely a foreign gain.
        ar_base += to_base(left, rate)

    je = _new_je(
        company, "JE-PAY-", "PAYMENT", payment.payment_date,
        f"Payment {payment.reference} — {payment.customer.name}",
        source_currency=payment.currency, exchange_rate=rate,
    )
    _dr(je, debit_acct, bank_base, f"Cash receipt: {payment.reference}")
    _cr(je, ar_account, ar_base, f"AR clear: {payment.reference}")
    _book_exchange_difference(
        je, company, bank_base - ar_base,
        f"Realised exchange on {payment.reference}",
    )
    je.post(user=user, document="payment")

    from apps.sales.models import CustomerPayment as _CP
    _CP.objects.filter(pk=payment.pk).update(journal_entry=je)
    payment.journal_entry = je

    for inv, allocated, allocated_base in allocations:
        PaymentAllocation.objects.create(
            payment=payment, invoice=inv, allocated_amount=allocated,
            base_amount=allocated_base,
        )
        inv.paid_amount = (inv.paid_amount or Decimal("0")) + allocated
        inv.save()

    return je


# ─────────────────────────── Point of sale ──────────────────────────────────

@transaction.atomic
def post_pos_sale(sale, user=None, refund=False) -> JournalEntry:
    """Book a counter sale, or the money handed back over the same counter.

    A sale                                   A refund
    DR  the till (1100) or the bank (1200)   DR  revenue per line
    CR  sales revenue per line               CR  VAT payable
    CR  VAT payable                          CR  the till or the bank

    Accounts Receivable is nowhere near this. A till sale is paid as it is rung
    up, so there is no balance to clear and nothing to age; the money is already
    in the drawer or on the bank statement. A refund runs the entry backwards as
    its own posted document rather than editing the sale, because the morning's
    takings and the afternoon's give-back are two events the cash-up has to be
    able to show separately.

    What the customer handed over and what the ticket asked for are different
    figures — the difference is change, which never reaches the ledger: the till
    is debited with the money it keeps, which is the bill.
    """
    if sale.journal_entry_id:
        return sale.journal_entry

    company = sale.session.company
    cash = sale.payment_method == "CASH"
    counter = (
        _get_account_safe(company, "1100") if cash
        else _get_account_safe(company, "1200", "1100")
        or _get_account_by_type(company, "BANK", "CASH")
    )
    default_revenue = (
        _get_account_safe(company, "4100", "4200")
        or _get_account_by_type(company, "REVENUE")
    )
    default_vat = _get_account_safe(company, "2200") or _get_account_by_type(company, "TAX")

    if not counter:
        raise ValueError(
            "GL posting failed: no till (1100) or bank (1200) account to take the money."
        )
    if not default_revenue:
        raise ValueError("GL posting failed: no sales revenue account (4100) on file.")

    je = _new_je(
        company, "JE-RFND-" if refund else "JE-POS-",
        "PAYMENT" if refund else "RECEIPT", sale.created_at.date(),
        f"{'Refund' if refund else 'Till sale'} {sale.reference} — "
        f"{sale.session.cashier.get_full_name() or sale.session.cashier.username}",
    )
    kind = "Refund" if refund else "Sale"
    for line in sale.lines.select_related(
        "product", "product__income_account", "tax_rate", "tax_rate__account"
    ):
        product = line.product
        revenue = (product.income_account if product else None) or default_revenue
        name = product.name if product else "Item"
        count = _qty_text(line.quantity)
        net = (line.line_total or Decimal("0")) - (line.tax_amount or Decimal("0"))
        if refund:
            if net > 0:
                _dr(je, revenue, net, f"{kind}: {name} × {count}")
            if line.tax_amount and line.tax_amount > 0:
                _dr(je, (line.tax_rate.account if line.tax_rate_id else None) or default_vat,
                    line.tax_amount, f"VAT back: {sale.reference}")
        else:
            if net > 0:
                _cr(je, revenue, net, f"{kind}: {name} × {count}")
            if line.tax_amount and line.tax_amount > 0:
                _cr(je, (line.tax_rate.account if line.tax_rate_id else None) or default_vat,
                    line.tax_amount, f"VAT: {sale.reference}")

    total = sale.total_amount or Decimal("0")
    if total <= 0:
        raise ValidationError("A ticket with nothing on it books nothing.")
    if refund:
        _cr(je, counter, total, f"Money back over the counter: {sale.reference}")
    else:
        _dr(je, counter, total, f"Counter receipt: {sale.reference}")
    je.post(user=user)

    from apps.pos.models import POSSale as _Sale
    _Sale.objects.filter(pk=sale.pk).update(journal_entry=je)
    sale.journal_entry = je
    return je


# ─────────────────────────── Bill GL ────────────────────────────────────────

def _bill_targets(bill, inventory_acct, vat_input_acct):
    """The accounts a bill charges, in the bill's own currency.

    Shared with the debit note so returned goods credit exactly what the
    purchase was debited — including whether VAT was recoverable or absorbed
    into the cost.
    """
    lines = getattr(bill, "lines", None)
    if lines is not None:
        try:
            bill_lines = list(lines.select_related(
                "product", "product__inventory_account", "tax_rate", "tax_rate__account"
            ))
        except Exception:
            bill_lines = []
    else:
        bill_lines = []

    targets = []
    if bill_lines:
        for line in bill_lines:
            inv_acct = (
                (line.product and line.product.inventory_account) or inventory_acct
            )
            line_desc = getattr(line, "description", "") or (
                line.product.name if line.product else "Purchase"
            )
            tax_amount = getattr(line, "tax_amount", Decimal("0")) or Decimal("0")

            if vat_input_acct and tax_amount > 0:
                # Recoverable VAT: cost + separate VAT Input debit
                targets.append((inv_acct, line.line_total, line_desc))
                targets.append(
                    (vat_input_acct, tax_amount, f"VAT Input: {bill.reference}"))
            else:
                # Non-recoverable or no VAT account: absorb tax into cost
                targets.append(
                    (inv_acct, line.line_total + tax_amount, line_desc))
    else:
        # Header-level fallback: the account the bill was entered against, or
        # stock when none was chosen.
        inventory_acct = getattr(bill, "charge_account", None) or inventory_acct
        if vat_input_acct and bill.tax_amount and bill.tax_amount > 0:
            # Recoverable VAT: separate input VAT debit
            targets.append((inventory_acct, bill.subtotal, f"Purchases: {bill.reference}"))
            targets.append(
                (vat_input_acct, bill.tax_amount, f"VAT Input: {bill.reference}"))
        else:
            # Non-recoverable VAT or no VAT Input account: include tax in cost
            targets.append(
                (inventory_acct, bill.total_amount, f"Purchases: {bill.reference}"))
    return targets


@transaction.atomic
def post_bill(bill, user=None) -> JournalEntry:
    """
    Post a supplier bill to the general ledger.

    Entries created
    ---------------
    DR  Stock Received Not Invoiced (2110) the goods value already received
    DR  Inventory account                  (1400) whatever was not received first
    DR  VAT Input (code 2210, if exists)   bill.tax_amount
    CR  Accounts Payable (2100)            bill.total_amount

    A bill for goods that are already in the warehouse does not add them to
    stock again — it releases the accrual the receipt put up. A bill for goods
    nobody received (or a service) charges stock or expense directly, as it
    always has.

    Returns the JournalEntry.
    """
    if bill.journal_entry_id:
        return bill.journal_entry

    company = bill.company

    ap_account = (
        _get_account_safe(company, "2100")
        or _get_account_by_type(company, "PAYABLE")
    )
    inventory_acct = (
        _get_account_safe(company, "1400")
        or _get_account_by_type(company, "ASSET")
    )
    # VAT Input — optional; skip if not configured
    vat_input_acct = _get_account_safe(company, "2210")

    if not ap_account:
        raise ValueError(
            "GL posting failed: Accounts Payable account (code 2100) not found."
        )
    if not inventory_acct:
        raise ValueError(
            "GL posting failed: Inventory/Asset account (code 1400) not found."
        )

    rate = bill.rate
    total_base = bill.total_amount_base or to_base(bill.total_amount, rate)

    je = _new_je(
        company, "JE-BILL-", "BILL", bill.bill_date,
        f"Bill {bill.reference} — {bill.supplier.name}",
        source_currency=bill.currency, exchange_rate=rate,
    )

    targets = _bill_targets(bill, inventory_acct, vat_input_acct)

    _cr(je, ap_account, total_base, f"AP: {bill.reference}")
    bases = spread([t[1] for t in targets], rate, total_base)
    # Goods that arrived before this bill were put into stock on the day they
    # came in, against the received-not-invoiced accrual. This bill is the
    # supplier asking to be paid for that same stock, so it clears the accrual
    # instead of charging the cost to stock a second time; only what the
    # accrual does not cover — a price above the order, or goods billed without
    # a receipt — is new cost, and it reaches stock as it always has.
    remaining = (accrual_remaining(bill.purchase_order)
                 if bill.purchase_order_id else Decimal("0"))
    clearing = ensure_account(company, *NOT_BILLED) if remaining > 0 else None
    cleared = Decimal("0")
    for (account, _foreign, desc), base in zip(targets, bases):
        move = Decimal("0")
        if clearing and account is not vat_input_acct and remaining > 0:
            move = min(remaining, base)
            remaining -= move
            cleared += move
        if base - move > 0:
            _dr(je, account, base - move, desc)
    if cleared > 0:
        _dr(je, clearing, cleared,
            f"Goods received and now billed: {bill.reference}")
    je.post(user=user, document="bill")

    from apps.procurement.models import Bill as _Bill
    _Bill.objects.filter(pk=bill.pk).update(
        journal_entry=je,
        status="RECEIVED",
        accrued_cleared=cleared,
    )
    bill.journal_entry = je
    bill.status = "RECEIVED"
    bill.accrued_cleared = cleared
    return je


# ──────────────────────── Stock Movement GL ──────────────────────────────────

# Which movements change what the book owns, and so have to be booked. A
# transfer only moves stock between the company's own shelves: nothing is
# bought, sold, used up or lost, so no entry is due.
STOCK_IN = ("RECEIPT", "OPENING", "ADJUSTMENT_PLUS", "PRODUCTION_IN", "RETURN_IN")
STOCK_OUT = ("DELIVERY", "ADJUSTMENT_MINUS", "PRODUCTION_OUT", "RETURN_OUT")

# The accounts stock is booked against when the obvious one will not do.
NOT_BILLED = ("2110", "Stock Received Not Invoiced", "LIABILITY", "2000")
WIP = ("1450", "Work In Progress", "ASSET", "1000")
STOCK_ADJUSTMENTS = ("5300", "Stock Adjustments & Shrinkage", "COST_OF_GOODS", "5000")
OPENING_EQUITY = ("3100", "Share Capital", "EQUITY", "3000")


def goods_accrued(purchase_order) -> Decimal:
    """What has been received into stock against this order, in book money.

    A receipt is costed in the book's own currency when it is created — a
    foreign order costs what the order's rate says it costs — so the accrual
    and the bill that later clears it are counted in the same money.
    """
    from apps.inventory.models import StockMovement

    return sum((movement.total_cost for movement in StockMovement.objects.filter(
        purchase_order=purchase_order, movement_type="RECEIPT")), Decimal("0"))


def accrual_remaining(purchase_order) -> Decimal:
    """Goods standing in the warehouse for this order that no bill has answered."""
    from apps.procurement.models import Bill

    # A debit note is the supplier paying us back, not a fresh claim on the
    # accrual, so only real bills are counted against it — and a cancelled bill
    # does not count either, because cancelling it reversed the clearing it had
    # done and the accrual is standing there again.
    cleared = sum((bill.accrued_cleared for bill in Bill.objects.filter(
        purchase_order=purchase_order, original_bill__isnull=True
    ).exclude(status="CANCELLED")), Decimal("0"))
    return goods_accrued(purchase_order) - cleared


def _stock_accounts(company, product):
    """(the account stock is held on, the account it is expensed through)."""
    inventory = (product.inventory_account
                 or _get_account_safe(company, "1400")
                 or ensure_account(company, "1400", "Inventory", "ASSET", "1000"))
    cogs = (product.cogs_account
            or _get_account_safe(company, "5000")
            or ensure_account(company, "5000", "Cost of Goods Sold",
                              "COST_OF_GOODS", None))
    return inventory, cogs


def _movement_value(movement) -> Decimal:
    """What a stock movement is worth in the book, to the cent.

    Quantity and unit cost are both held to four decimals, so their product has
    six — more precision than money carries. Rounding once here, and using that
    one figure for both sides of the entry, is what keeps a stock posting
    balanced instead of differing by a fraction of a cent.
    """
    return to_base((movement.quantity or Decimal("0"))
                   * (movement.unit_cost or Decimal("0")), Decimal("1"))


def _qty_text(quantity) -> str:
    """How a counted quantity reads on a journal line: 5, not 5.0000."""
    text = format(Decimal(str(quantity or 0)), "f").rstrip("0").rstrip(".")
    return text or "0"


@transaction.atomic
def post_stock_delivery(movement, user=None) -> Optional[JournalEntry]:
    """Book goods leaving the warehouse: stock down, and the cost where it went.

    DR the account the goods were used for   quantity × the cost on the shelf
    CR Inventory                             the same

    A sale or a consumption charges cost of sales; a count that came up short is
    written off as a stock adjustment, not sold; production draws materials
    into work in progress instead of pretending the shop floor sold something;
    and goods going back to a supplier release the accrual that received them.
    """
    product = movement.product
    company = product.company
    inventory, cogs = _stock_accounts(company, product)

    value = _movement_value(movement)
    if value <= 0:
        # Goods with no cost on them cost the book nothing — a free sample or a
        # receipt nobody priced. Nothing is booked, and the shelf still moves.
        return None

    kind = movement.movement_type
    if kind == "DELIVERY":
        debit, reason = cogs, "Cost of goods sold"
    elif kind == "PRODUCTION_OUT":
        debit, reason = ensure_account(company, *WIP), "Issued to production"
    elif kind == "ADJUSTMENT_MINUS":
        debit, reason = ensure_account(company, *STOCK_ADJUSTMENTS), "Stock written down"
    elif kind == "RETURN_OUT":
        debit, reason = ensure_account(company, *NOT_BILLED), "Returned to supplier"
    else:
        return None

    je = _new_je(
        company, "JE-COGS-", "STOCK_ADJUSTMENT", movement.movement_date,
        f"{reason}: {movement.reference or product.name}",
    )
    count = _qty_text(movement.quantity)
    _dr(je, debit, value, f"{reason}: {product.name} × {count}")
    _cr(je, inventory, value, f"Inventory out: {product.name} × {count}")
    je.post(user=user)
    return je


@transaction.atomic
def post_stock_receipt(movement, user=None) -> Optional[JournalEntry]:
    """Book goods coming in: stock up at its cost, against the source of it.

    DR Inventory                 quantity × the cost the goods arrived at
    CR the account the value came from

    A supplier's delivery is not a payment. The goods are owed for, but nobody
    has billed for them yet, so the credit is the received-not-invoiced accrual
    that the bill later clears — never Accounts Payable, which is the account
    the bill book alone is proved against, and double-counts the same goods the
    moment a bill arrives. Stock the owner introduced credits equity, a count
    that came up long is a stock adjustment, production output comes out of
    work in progress, and a customer's return comes back off cost of sales
    because that is where it was charged when it went out.
    """
    product = movement.product
    company = product.company
    inventory, cogs = _stock_accounts(company, product)

    value = _movement_value(movement)
    if value <= 0:
        return None

    kind = movement.movement_type
    if kind == "RECEIPT":
        credit, reason = ensure_account(company, *NOT_BILLED), "Goods received"
    elif kind == "OPENING":
        credit, reason = ensure_account(company, *OPENING_EQUITY), "Opening stock"
    elif kind == "ADJUSTMENT_PLUS":
        credit, reason = ensure_account(company, *STOCK_ADJUSTMENTS), "Stock found on count"
    elif kind == "PRODUCTION_IN":
        credit, reason = ensure_account(company, *WIP), "Production output"
    elif kind == "RETURN_IN":
        credit, reason = cogs, "Customer return"
    else:
        return None

    je = _new_je(
        company, "JE-RECV-", "STOCK_ADJUSTMENT", movement.movement_date,
        f"{reason}: {movement.reference or product.name}",
    )
    count = _qty_text(movement.quantity)
    _dr(je, inventory, value,
        f"Inventory in: {product.name} × {count}")
    _cr(je, credit, value, f"{reason}: {product.name} × {count}")
    je.post(user=user)
    return je


# ──────────────────────── Expense Claim GL ───────────────────────────────────

@transaction.atomic
def post_expense_claim(claim, bank_account=None, user=None) -> JournalEntry:
    """
    Post an approved expense claim payment to the GL.

    DR  Expense account per line   (ExpenseCategory.gl_account or 6000)
    CR  Bank / Cash                claim.total_amount  (bank_account.gl_account or 1100)

    Parameters
    ----------
    claim        : expenses.ExpenseClaim
    bank_account : banking.BankAccount  (the petty cash / bank account used to pay)
    user         : accounts.User

    Returns the JournalEntry.
    """
    company = claim.company

    if bank_account and getattr(bank_account, "gl_account_id", None):
        cash_acct = bank_account.gl_account
    else:
        cash_acct = (
            _get_account_safe(company, "1100", "1200")
            or _get_account_by_type(company, "CASH", "BANK")
        )

    default_expense = (
        _get_account_safe(company, "6000")
        or _get_account_by_type(company, "EXPENSE")
    )

    if not cash_acct:
        raise ValueError(
            "GL posting failed: Cash/Bank account not found for expense payment."
        )
    if not default_expense:
        raise ValueError(
            "GL posting failed: Expense account (code 6000) not found."
        )

    je = _new_je(
        company, "JE-EXP-", "MANUAL", claim.claim_date,
        f"Expense Claim {claim.reference}",
    )

    for line in claim.lines.select_related("category", "category__gl_account"):
        exp_acct = (
            (line.category and line.category.gl_account) or default_expense
        )
        _dr(je, exp_acct, line.amount, line.description)

    _cr(je, cash_acct, claim.total_amount, f"Paid: {claim.reference}")
    je.post(user=user, document="expense claim")

    # Link back to claim
    claim.journal_entry = je
    type(claim).objects.filter(pk=claim.pk).update(
        journal_entry=je,
        status="PAID",
        paid_at=timezone.now().date(),
    )
    return je


# ─────────────────────────── Payroll GL ──────────────────────────────────────

def post_payroll_run(run, user=None) -> JournalEntry:
    """Book a month's pay as one entry.

    DR  6100 Salaries & Wages        the pay itself, and any employer share whose
    DR  an allowance's own account   table has no expense account of its own
    CR  2300 / 2400 / 2500 / 2550    every withholding — both halves of a
                                     contribution go to the same payable
    CR  1200 Bank                    the net pay that actually leaves the account

    One entry for the whole run, not one per person: the deductions are remitted
    as single figures, and a run that balances proves the payslips against the
    ledger in one place.
    """
    from apps.payroll.models import PayslipLine

    company = run.company
    if run.journal_entry_id:
        return run.journal_entry
    if not run.payslips.exists():
        raise ValueError(
            f"'{run.reference}' has no payslips, so there is nothing in it to book. "
            "Calculate the run first.")

    salary_acct = (
        run.salary_account
        or _get_account_safe(company, "6100")
        or _get_account_by_type(company, "EXPENSE")
    )
    net_acct = (
        run.paid_into
        or _get_account_safe(company, "1200", "1100")
        or _get_account_by_type(company, "BANK", "CASH")
    )
    if not salary_acct:
        raise ValueError(
            "GL posting failed: this book has no salaries expense account (6100) "
            "to charge the month's pay to.")
    if not net_acct:
        raise ValueError(
            "GL posting failed: no bank or cash account to pay the net salary out of.")

    def charge(bucket, account, amount, label):
        if not amount:
            return
        if account is None:
            raise ValueError(
                f"GL posting failed: '{label}' has no account to book to — set one on "
                "the rule or component that produced it.")
        row = bucket.setdefault(account, [Decimal("0"), label])
        row[0] += amount

    def payable_for(line):
        if line.gl_account_id:
            return line.gl_account
        if line.rule_id:
            return line.rule.payable_account
        return _get_account_safe(company, "2600")

    debits, credits = {}, {}
    for line in PayslipLine.objects.filter(
            payslip__payroll_run=run).select_related(
        "gl_account", "rule", "rule__payable_account", "payslip"):
        if line.kind == "EARNING":
            charge(debits, line.gl_account or salary_acct, line.amount, line.description)
        elif line.kind == "DEDUCTION":
            charge(credits, payable_for(line), line.amount, line.description)
        else:
            expense = line.rule.expense_account if line.rule_id else None
            charge(debits, expense or salary_acct, line.amount,
                   f"{line.description} — employer cost")
            charge(credits, payable_for(line), line.amount, line.description)

    charge(credits, net_acct, run.total_net, f"Net pay {run.reference}")

    total_debit = sum((row[0] for row in debits.values()), Decimal("0"))
    total_credit = sum((row[0] for row in credits.values()), Decimal("0"))
    if total_debit != total_credit:
        raise ValueError(
            f"GL posting failed: '{run.reference}' charges {total_debit} but owes "
            f"{total_credit}. The payslip lines contradict the run's own totals — "
            "recalculate the run before posting it.")

    je = _new_je(
        company, "JE-PR-", "PAYROLL", run.payment_date,
        f"Payroll {run.reference} "
        f"({run.period_start:%d %b} – {run.period_end:%d %b %Y}, "
        f"{run.payslips.count()} payslip(s))",
    )
    for account, (amount, label) in sorted(debits.items(), key=lambda kv: kv[0].code):
        _dr(je, account, amount, label)
    for account, (amount, label) in sorted(credits.items(), key=lambda kv: kv[0].code):
        _cr(je, account, amount, label)

    je.post(user=user, document="payroll run")
    type(run).objects.filter(pk=run.pk).update(journal_entry=je, status="POSTED")
    run.journal_entry = je
    run.status = "POSTED"
    return je


# ────────────────────────── Period and year close ────────────────────────────
#
# Closing a period is a journal entry, not a flag on the calendar: the figures
# move out of the profit and loss accounts and into retained earnings where
# the balance sheet can read them, and reopening reverses the entry instead of
# editing history.

PERIOD_CLOSE = "ACCOUNTING_PERIOD"
YEAR_CLOSE = "FISCAL_YEAR"


def closing_entry_for(company, kind, source_id):
    """The closing entry a period or year is currently sitting on, if any.

    A reversed closing entry stays POSTED in the ledger (its reversal is a
    second entry that offsets it), so liveness is read from the reversal link.
    The reversal of a closing entry carries the same tags and type, so it has
    to be excluded by being someone's reversal rather than by status.
    """
    return (
        JournalEntry.objects.filter(
            company=company, entry_type="CLOSING",
            source_document_type=kind, source_document_id=str(source_id),
            status="POSTED", reversed_by__isnull=True, reversal_of__isnull=True,
        ).first()
    )


def period_activity(company, start, end):
    """Net balance of every income-statement account inside a window."""
    rows = (
        JournalEntryLine.objects.filter(
            account__company=company,
            account__account_type__in=INCOME_STATEMENT_TYPES,
            journal_entry__status="POSTED",
            journal_entry__posting_date__gte=start,
            journal_entry__posting_date__lte=end,
        )
        .exclude(journal_entry__entry_type="CLOSING")
        .values("account_id", "account__code", "account__name")
        .annotate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        .order_by("account__code")
    )
    activity = []
    for row in rows:
        net = (row["dr"] or Decimal("0")) - (row["cr"] or Decimal("0"))
        if net:
            activity.append({
                "account_id": row["account_id"],
                "code": row["account__code"],
                "name": row["account__name"],
                "net": net,
            })
    return activity


def profit_for(company, start, end):
    """Revenue less cost and expense posted in a window, in the profit's own sign."""
    return -sum((row["net"] for row in period_activity(company, start, end)), Decimal("0"))


@transaction.atomic
def close_period(company, period, user=None):
    """Empty the period's revenue and expense balances into current year profit.

    Returns the posted closing entry, or None when the period held no profit or
    loss activity at all (in which case there is genuinely nothing to carry).
    """
    live = closing_entry_for(company, PERIOD_CLOSE, period.pk)
    if live:
        if period.status != "CLOSED":
            period.status = "CLOSED"
            period.save(update_fields=["status", "updated_at"])
        return live

    activity = period_activity(company, period.start_date, period.end_date)
    if not activity:
        period.status = "CLOSED"
        period.save(update_fields=["status", "updated_at"])
        return None

    profit_account = (
        _get_account_safe(company, "3300") or _get_account_by_type(company, "EQUITY")
    )
    if not profit_account:
        raise ValueError(
            "Closing a period needs a Current Year Profit account (code 3300)."
        )

    je = _new_je(
        company, "JE-CLOSE-", "CLOSING", period.end_date,
        f"Close {period.name} into {profit_account.code} {profit_account.name}",
    )
    profit = Decimal("0")
    for row in activity:
        account = Account.objects.get(pk=row["account_id"])
        if row["net"] < 0:
            # A credit balance belongs to income: debit it away to nothing.
            _dr(je, account, -row["net"], f"Close {period.name}: {row['code']}")
        else:
            _cr(je, account, row["net"], f"Close {period.name}: {row['code']}")
        profit -= row["net"]

    if profit > 0:
        _cr(je, profit_account, profit, f"Profit for {period.name}")
    elif profit < 0:
        _dr(je, profit_account, -profit, f"Loss for {period.name}")

    je.source_document_type = PERIOD_CLOSE
    je.source_document_id = str(period.pk)
    je.save(update_fields=["source_document_type", "source_document_id", "updated_at"])
    je.post(user=user)

    period.status = "CLOSED"
    period.save(update_fields=["status", "updated_at"])
    return je


@transaction.atomic
def reopen_period(company, period, user=None):
    """Reverse the period's closing entry and hand the months their figures back."""
    entry = closing_entry_for(company, PERIOD_CLOSE, period.pk)
    if entry:
        entry.reverse(user=user)
    period.status = "OPEN"
    period.save(update_fields=["status", "updated_at"])
    return entry


@transaction.atomic
def close_fiscal_year(company, fiscal_year, user=None):
    """Close every open period, then empty current year profit into retained earnings."""
    entries = []
    for period in fiscal_year.periods.filter(status="OPEN").order_by("start_date"):
        entry = close_period(company, period, user=user)
        if entry:
            entries.append(entry)

    profit_account = _get_account_safe(company, "3300")
    retained = (
        _get_account_safe(company, "3200") or _get_account_by_type(company, "EQUITY")
    )
    year_entry = None
    if profit_account and retained and profit_account.pk != retained.pk:
        balance = profit_account.current_balance
        if balance:
            amount = abs(balance)
            year_entry = _new_je(
                company, "JE-YE-", "CLOSING", fiscal_year.end_date,
                f"Year end {fiscal_year.name}: "
                f"{profit_account.code} to {retained.code} {retained.name}",
            )
            if balance > 0:
                _dr(year_entry, profit_account, amount, f"Clear {profit_account.name}")
                _cr(year_entry, retained, amount, f"Retained {fiscal_year.name}")
            else:
                _dr(year_entry, retained, amount, f"Cover {fiscal_year.name} loss")
                _cr(year_entry, profit_account, amount, f"Clear {profit_account.name}")
            year_entry.source_document_type = YEAR_CLOSE
            year_entry.source_document_id = str(fiscal_year.pk)
            year_entry.save(update_fields=[
                "source_document_type", "source_document_id", "updated_at",
            ])
            year_entry.post(user=user)
            entries.append(year_entry)

    fiscal_year.is_closed = True
    fiscal_year.save(update_fields=["is_closed", "updated_at"])
    fiscal_year.periods.exclude(status="OPEN").update(status="LOCKED")
    fiscal_year.periods.filter(status="OPEN").update(status="CLOSED")
    return entries


@transaction.atomic
def reopen_fiscal_year(company, fiscal_year, user=None):
    """Undo a year end: reverse the sweep and every period close, then unlock."""
    reversed_entries = []
    year_entry = closing_entry_for(company, YEAR_CLOSE, fiscal_year.pk)
    if year_entry:
        year_entry.reverse(user=user)
        reversed_entries.append(year_entry)
    for period in fiscal_year.periods.all():
        entry = closing_entry_for(company, PERIOD_CLOSE, period.pk)
        if entry:
            entry.reverse(user=user)
            reversed_entries.append(entry)
        period.status = "OPEN"
        period.save(update_fields=["status", "updated_at"])
    fiscal_year.is_closed = False
    fiscal_year.save(update_fields=["is_closed", "updated_at"])
    return reversed_entries
