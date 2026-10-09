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
from django.utils import timezone

from apps.accounting.models import Account, JournalEntry, JournalEntryLine
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


def _new_je(company, ref_prefix: str, entry_type: str, date, description: str = "") -> JournalEntry:
    """Create a DRAFT JournalEntry with a sequenced reference."""
    ref = SequenceCounter.next_number(company, ref_prefix)
    return JournalEntry.objects.create(
        company=company,
        reference=ref,
        entry_type=entry_type,
        posting_date=date,
        status="DRAFT",
        notes=description,
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


# ──────────────────────────── Invoice GL ────────────────────────────────────

@transaction.atomic
def post_invoice(invoice, user=None) -> JournalEntry:
    """
    Post a customer invoice to the general ledger.

    Entries created
    ---------------
    DR  Accounts Receivable (1300)          invoice.total_amount
    CR  Sales Revenue per line              line.line_total  (product.income_account or 4100)
    CR  VAT Payable per taxed line          line.tax_amount  (tax_rate.account or 2200)

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

    je = _new_je(
        company, "JE-INV-", "INVOICE", invoice.invoice_date,
        f"Invoice {invoice.reference} — {invoice.customer.name}",
    )

    # Single debit: total receivable
    _dr(je, ar_account, invoice.total_amount, f"AR: {invoice.reference}")

    # Per-line revenue credits
    for line in invoice.lines.select_related(
        "product", "product__income_account", "tax_rate", "tax_rate__account"
    ):
        revenue_acct = (
            (line.product and line.product.income_account) or default_revenue
        )
        line_desc = line.description or (line.product.name if line.product else "Item")
        _cr(je, revenue_acct, line.line_total, line_desc)

        if line.tax_amount and line.tax_amount > 0:
            tax_acct = (
                (line.tax_rate and line.tax_rate.account) or default_vat
            )
            _cr(je, tax_acct, line.tax_amount, f"VAT: {invoice.reference}")

    je.post(user=user)

    # Link back to the invoice and mark it as SENT
    from apps.sales.models import Invoice as _Invoice
    _Invoice.objects.filter(pk=invoice.pk).update(
        journal_entry=je,
        status="SENT",
    )
    invoice.journal_entry = je
    invoice.status = "SENT"
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
        debit_acct = (
            _get_account_safe(company, "1200", "1100")
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

    je = _new_je(
        company, "JE-PAY-", "PAYMENT", payment.payment_date,
        f"Payment {payment.reference} — {payment.customer.name}",
    )
    _dr(je, debit_acct, payment.amount, f"Cash receipt: {payment.reference}")
    _cr(je, ar_account, payment.amount, f"AR clear: {payment.reference}")
    je.post(user=user)

    from apps.sales.models import CustomerPayment as _CP
    _CP.objects.filter(pk=payment.pk).update(journal_entry=je)
    payment.journal_entry = je

    # ── invoice allocation ──
    from apps.sales.models import Invoice as _Invoice, PaymentAllocation

    if invoice_allocations is not None:
        # Explicit list provided by caller
        for inv, alloc_amount in invoice_allocations:
            if alloc_amount and alloc_amount > 0:
                PaymentAllocation.objects.create(
                    payment=payment, invoice=inv, allocated_amount=alloc_amount
                )
                inv.paid_amount = (inv.paid_amount or Decimal("0")) + alloc_amount
                inv.save()
    else:
        # Auto-allocate: oldest outstanding invoices first
        outstanding = (
            _Invoice.objects
            .filter(
                company=company,
                customer=payment.customer,
                status__in=["SENT", "PARTIAL", "OVERDUE"],
            )
            .order_by("invoice_date")
            .select_for_update()
        )
        remaining = payment.amount
        for inv in outstanding:
            if remaining <= 0:
                break
            allocate = min(remaining, inv.outstanding_amount)
            if allocate > 0:
                PaymentAllocation.objects.create(
                    payment=payment, invoice=inv, allocated_amount=allocate
                )
                inv.paid_amount = (inv.paid_amount or Decimal("0")) + allocate
                inv.save()
                remaining -= allocate

    return je


# ─────────────────────────── Bill GL ────────────────────────────────────────

@transaction.atomic
def post_bill(bill, user=None) -> JournalEntry:
    """
    Post a supplier bill to the general ledger.

    Entries created
    ---------------
    DR  Inventory account                  bill.subtotal   (product.inventory_account or 1400)
    DR  VAT Input (code 2210, if exists)   bill.tax_amount
    CR  Accounts Payable (2100)            bill.total_amount

    When BillLine is introduced, iterate per line for per-product accounts.
    Currently works at header level.

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

    je = _new_je(
        company, "JE-BILL-", "BILL", bill.bill_date,
        f"Bill {bill.reference} — {bill.supplier.name}",
    )

    # Try per-line posting if bill has lines queryset
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
                _dr(je, inv_acct, line.line_total, line_desc)
                _dr(je, vat_input_acct, tax_amount, f"VAT Input: {bill.reference}")
            else:
                # Non-recoverable or no VAT account: absorb tax into cost
                _dr(je, inv_acct, line.line_total + tax_amount, line_desc)
    else:
        # Header-level fallback
        if vat_input_acct and bill.tax_amount and bill.tax_amount > 0:
            # Recoverable VAT: separate input VAT debit
            _dr(je, inventory_acct, bill.subtotal, f"Purchases: {bill.reference}")
            _dr(je, vat_input_acct, bill.tax_amount, f"VAT Input: {bill.reference}")
        else:
            # Non-recoverable VAT or no VAT Input account: include tax in cost
            _dr(je, inventory_acct, bill.total_amount, f"Purchases: {bill.reference}")

    _cr(je, ap_account, bill.total_amount, f"AP: {bill.reference}")
    je.post(user=user)

    from apps.procurement.models import Bill as _Bill
    _Bill.objects.filter(pk=bill.pk).update(
        journal_entry=je,
        status="RECEIVED",
    )
    bill.journal_entry = je
    bill.status = "RECEIVED"
    return je


# ──────────────────────── Stock Movement GL ──────────────────────────────────

@transaction.atomic
def post_stock_delivery(movement, user=None) -> Optional[JournalEntry]:
    """
    Post COGS/Inventory GL for a stock outbound movement (sale/delivery).

    DR  COGS (product.cogs_account or 5000)
    CR  Inventory (product.inventory_account or 1400)

    Cost is quantity × avg_cost from StockLevel (after movement is saved).
    Returns None if cost is zero or accounts are missing (service items).
    """
    company = movement.product.company
    product = movement.product

    cogs_acct = (
        product.cogs_account
        or _get_account_safe(company, "5000")
        or _get_account_by_type(company, "COST_OF_GOODS")
    )
    inv_acct = (
        product.inventory_account
        or _get_account_safe(company, "1400")
        or _get_account_by_type(company, "ASSET")
    )

    if not cogs_acct or not inv_acct:
        return None

    # Use the avg_cost that was current before this movement
    # (movement.unit_cost may be set by the caller to match avg cost)
    cost_per_unit = movement.unit_cost if movement.unit_cost else Decimal("0")
    if not cost_per_unit:
        # Fall back to product cost_price
        cost_per_unit = product.cost_price or Decimal("0")

    cost_value = movement.quantity * cost_per_unit
    if cost_value <= 0:
        return None

    je = _new_je(
        company, "JE-COGS-", "STOCK_ADJUSTMENT", movement.movement_date,
        f"COGS: {movement.reference or product.name}",
    )
    _dr(je, cogs_acct, cost_value,
        f"COGS: {product.name} × {movement.quantity}")
    _cr(je, inv_acct, cost_value,
        f"Inventory out: {product.name} × {movement.quantity}")
    je.post(user=user)
    return je


@transaction.atomic
def post_stock_receipt(movement, user=None) -> Optional[JournalEntry]:
    """
    Post Inventory/AP GL for a stock inbound movement (receipt).

    DR  Inventory (product.inventory_account or 1400)
    CR  Accounts Payable (2100) or Cash (1100)

    Returns None if cost is zero or accounts are missing.
    """
    company = movement.product.company
    product = movement.product

    inv_acct = (
        product.inventory_account
        or _get_account_safe(company, "1400")
        or _get_account_by_type(company, "ASSET")
    )
    ap_acct = (
        _get_account_safe(company, "2100")
        or _get_account_by_type(company, "PAYABLE")
    )

    if not inv_acct or not ap_acct:
        return None

    cost_value = movement.quantity * movement.unit_cost
    if cost_value <= 0:
        return None

    je = _new_je(
        company, "JE-RECV-", "STOCK_ADJUSTMENT", movement.movement_date,
        f"Receipt: {movement.reference or product.name}",
    )
    _dr(je, inv_acct, cost_value,
        f"Inventory in: {product.name} × {movement.quantity}")
    _cr(je, ap_acct, cost_value,
        f"AP: {product.name} × {movement.quantity}")
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
    je.post(user=user)

    # Link back to claim
    claim.journal_entry = je
    type(claim).objects.filter(pk=claim.pk).update(
        journal_entry=je,
        status="PAID",
        paid_at=timezone.now().date(),
    )
    return je
