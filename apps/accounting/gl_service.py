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


# ──────────────────────────── POS sale GL ───────────────────────────────────

@transaction.atomic
def post_pos_sale(sale, user=None) -> JournalEntry:
    """A till sale is paid on the spot.

    DR  Cash (1100) for cash, else Bank (1200)   sale.total_amount
    CR  Sales revenue per line                   net of VAT
    CR  VAT payable                              sale.tax_amount
    Cost of sales is posted by the stock movements.
    """
    company = sale.session.company
    if sale.payment_method == "CASH":
        money = _get_account_safe(company, "1100") or _get_account_by_type(company, "CASH")
    else:
        money = _get_account_safe(company, "1200") or _get_account_by_type(company, "BANK")
    default_revenue = _get_account_safe(company, "4100", "4200") or _get_account_by_type(company, "REVENUE")
    default_vat = _get_account_safe(company, "2200") or _get_account_by_type(company, "TAX")
    if not money or not default_revenue:
        raise ValueError("GL posting failed: the chart of accounts needs cash/bank (1100/1200) and sales (4100).")

    je = _new_je(company, "JE-POS-", "RECEIPT", sale.created_at.date() if sale.created_at else timezone.now().date(),
                 f"Till sale {sale.reference}")
    _dr(je, money, sale.total_amount, f"{sale.get_payment_method_display()}: {sale.reference}")
    for line in sale.lines.select_related("product", "product__income_account", "product__tax_rate__account"):
        net = line.line_total - line.tax_amount
        _cr(je, line.product.income_account or default_revenue, net, f"{line.product.name} × {line.quantity:g}")
        if line.tax_amount > 0:
            rate = line.product.tax_rate
            _cr(je, (rate and rate.account) or default_vat, line.tax_amount, f"VAT: {sale.reference}")
    je.post(user=user)
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
        # Header-level: charge the account the accountant chose. A bill for a
        # purchase order clears what the goods receipt put in GRNI; any other
        # stock bill goes straight to Inventory.
        charge_acct = getattr(bill, "expense_account", None) or (
            grni_account(company) if bill.purchase_order_id else inventory_acct)
        if vat_input_acct and bill.tax_amount and bill.tax_amount > 0:
            # Recoverable VAT: separate input VAT debit
            _dr(je, charge_acct, bill.subtotal, f"Purchases: {bill.reference}")
            _dr(je, vat_input_acct, bill.tax_amount, f"VAT Input: {bill.reference}")
        else:
            # Non-recoverable VAT or no VAT Input account: include tax in cost
            _dr(je, charge_acct, bill.total_amount, f"Purchases: {bill.reference}")

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
    if movement.movement_type == "RECEIPT":
        # Goods in from a supplier: owed once their bill arrives.
        ap_acct = grni_account(company)
    else:
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


def ensure_account(company, code, name, account_type, parent_code=None) -> Account:
    """The active account `code`, created (under `parent_code`) if the chart lacks it."""
    acct = _get_account_safe(company, code)
    if acct:
        return acct
    acct, _ = Account.objects.get_or_create(
        company=company, code=code,
        defaults={"name": name, "account_type": account_type,
                  "parent": _get_account_safe(company, parent_code) if parent_code else None},
    )
    return acct


def grni_account(company) -> Account:
    """Goods received not invoiced (2150): credited when a PO is received,
    cleared when the supplier's bill for that PO is posted."""
    return ensure_account(company, "2150", "Goods Received Not Invoiced", "LIABILITY", "2000")


# ──────────────────────────── Payroll GL ────────────────────────────────────

@transaction.atomic
def post_payroll(run, user=None) -> JournalEntry:
    """Accrue an approved payroll run on its last day.

    DR  6100 Salaries & wages            gross pay
    DR  6110 NSSF employer               employer NSSF
    DR  6130 Housing levy employer       employer levy
    CR  2300 PAYE payable                PAYE
    CR  2400 NSSF payable                employee + employer NSSF
    CR  2500 SHIF payable                SHIF
    CR  2510 Housing levy payable        employee + employer levy
    CR  2620 Net salaries payable        net pay
    """
    if run.journal_entry_id:
        return run.journal_entry
    from django.db.models import Sum
    co = run.company
    t = run.payslips.aggregate(gross=Sum("gross_salary"), paye=Sum("paye_tax"), nssf=Sum("nssf_employee"),
                               shif=Sum("nhif_shif"), levy=Sum("housing_levy"), net=Sum("net_salary"))
    t = {k: v or Decimal("0") for k, v in t.items()}
    # Employer contributions match the employee's for NSSF and the housing levy.
    er_nssf, er_levy = t["nssf"], t["levy"]
    je = _new_je(co, "JE-PAY-", "PAYROLL", run.period_end, f"Payroll {run.reference}")
    _dr(je, ensure_account(co, "6100", "Salaries & Wages", "EXPENSE", "6000"), t["gross"], "Gross pay")
    _dr(je, ensure_account(co, "6110", "NSSF - Employer", "EXPENSE", "6100"), er_nssf, "Employer NSSF")
    _dr(je, ensure_account(co, "6130", "Housing Levy - Employer", "EXPENSE", "6100"), er_levy, "Employer housing levy")
    _cr(je, ensure_account(co, "2300", "PAYE Payable", "LIABILITY", "2000"), t["paye"], "PAYE")
    _cr(je, ensure_account(co, "2400", "NSSF Payable", "LIABILITY", "2000"), t["nssf"] + er_nssf, "NSSF")
    _cr(je, ensure_account(co, "2500", "SHIF/NHIF Payable", "LIABILITY", "2000"), t["shif"], "SHIF")
    _cr(je, ensure_account(co, "2510", "Housing Levy Payable", "LIABILITY", "2000"), t["levy"] + er_levy,
        "Housing levy")
    _cr(je, ensure_account(co, "2620", "Net Salaries Payable", "LIABILITY", "2000"), t["net"], "Net pay due to staff")
    je.post(user=user)
    type(run).objects.filter(pk=run.pk).update(journal_entry=je)
    run.journal_entry = je
    return je


# ──────────────────────────── Fixed assets GL ───────────────────────────────

def _asset_accounts(asset):
    co, cat = asset.company, asset.category
    cost = (cat and cat.gl_asset_account) or _get_account_safe(co, "1610", "1630", "1620", "1600")
    accumulated = (cat and cat.gl_accumulated_account) or ensure_account(
        co, "1690", "Accumulated Depreciation", "ASSET", "1600")
    expense = (cat and cat.gl_depreciation_account) or ensure_account(co, "6900", "Depreciation", "EXPENSE", "6000")
    return cost, accumulated, expense


@transaction.atomic
def post_depreciation(dep, user=None) -> JournalEntry:
    """DR Depreciation expense / CR Accumulated depreciation, dated the period."""
    asset = dep.asset
    _, accumulated, expense = _asset_accounts(asset)
    je = _new_je(asset.company, "JE-DEP-", "DEPRECIATION", dep.period_date,
                 f"Depreciation {asset.code} {dep.period_date:%b %Y}")
    _dr(je, expense, dep.depreciation_amount, f"Depreciation: {asset.name}")
    _cr(je, accumulated, dep.depreciation_amount, f"Accumulated depreciation: {asset.code}")
    je.post(user=user)
    type(dep).objects.filter(pk=dep.pk).update(journal_entry=je)
    dep.journal_entry = je
    return je


@transaction.atomic
def post_asset_disposal(disposal, received_into=None, user=None) -> JournalEntry:
    """Take the asset off the books.

    DR  Accumulated depreciation     accumulated to date
    DR  Bank / cash                  proceeds
    CR  Asset cost account           purchase cost
    and the balance to Other income (gain) or Depreciation (loss on disposal).
    """
    asset = disposal.asset
    co = asset.company
    cost_acct, accumulated, expense = _asset_accounts(asset)
    if not cost_acct:
        raise ValueError("GL posting failed: no fixed-asset account (1610) to take the asset out of.")
    money = received_into or payment_accounts(co).first()
    if disposal.proceeds and not money:
        raise ValueError("GL posting failed: add a bank or cash account to receive the sale proceeds.")
    je = _new_je(co, "JE-DSP-", "MANUAL", disposal.disposal_date, f"Disposal of {asset.code} {asset.name}")
    _dr(je, accumulated, asset.accumulated_depreciation, f"Clear accumulated depreciation: {asset.code}")
    _dr(je, money, disposal.proceeds, f"Proceeds: {asset.code}")
    _cr(je, cost_acct, asset.purchase_cost, f"Remove cost: {asset.code}")
    if disposal.gain_loss > 0:
        _cr(je, ensure_account(co, "4300", "Other Income", "REVENUE", "4000"), disposal.gain_loss,
            f"Gain on disposal: {asset.code}")
    elif disposal.gain_loss < 0:
        _dr(je, expense, -disposal.gain_loss, f"Loss on disposal: {asset.code}")
    je.post(user=user)
    type(disposal).objects.filter(pk=disposal.pk).update(journal_entry=je)
    disposal.journal_entry = je
    return je


# ──────────────────────── Expense Claim GL ───────────────────────────────────

def payment_accounts(company):
    """Accounts money can be paid from: bank accounts first, then cash."""
    from django.db.models import Case, IntegerField, Value, When
    if company is None:
        return Account.objects.none()
    return Account.objects.filter(
        company=company, is_active=True, account_type__in=["BANK", "CASH"],
    ).annotate(
        _rank=Case(When(account_type="BANK", then=Value(0)), default=Value(1), output_field=IntegerField())
    ).order_by("_rank", "code")


def _claims_payable(company):
    return (
        _get_account_safe(company, "2610", "2600")
        or _get_account_by_type(company, "LIABILITY")
    )


@transaction.atomic
def accrue_expense_claim(claim, user=None) -> JournalEntry:
    """On approval: recognise the expense and what is owed to the employee.

    DR  Expense account per line   (ExpenseCategory.gl_account or 6000)
    CR  Employee claims payable    (2610, else 2600, else first liability)
    """
    if claim.journal_entry_id:
        return claim.journal_entry
    company = claim.company
    default_expense = _get_account_safe(company, "6000") or _get_account_by_type(company, "EXPENSE")
    payable = _claims_payable(company)
    if not default_expense:
        raise ValueError("GL posting failed: no expense account in the chart of accounts.")
    if not payable:
        raise ValueError("GL posting failed: add an 'Employee claims payable' liability account (2610).")

    je = _new_je(company, "JE-EXP-", "MANUAL", claim.claim_date,
                 f"Expense claim {claim.reference} — {claim.employee}")
    for line in claim.lines.select_related("category", "category__gl_account"):
        _dr(je, (line.category and line.category.gl_account) or default_expense, line.amount, line.description)
    _cr(je, payable, claim.total_amount, f"Owed to {claim.employee}: {claim.reference}")
    je.post(user=user)
    type(claim).objects.filter(pk=claim.pk).update(journal_entry=je)
    claim.journal_entry = je
    return je


@transaction.atomic
def pay_expense_claim(claim, pay_from, paid_on, user=None) -> JournalEntry:
    """Reimburse an approved claim.

    DR  Employee claims payable    claim.total_amount
    CR  Bank / cash paid from      claim.total_amount
    """
    company = claim.company
    payable = _claims_payable(company)
    if not payable:
        raise ValueError("GL posting failed: no 'Employee claims payable' account.")
    je = _new_je(company, "JE-EXPPAY-", "PAYMENT", paid_on,
                 f"Reimburse {claim.employee}: {claim.reference}")
    _dr(je, payable, claim.total_amount, f"Clear claim {claim.reference}")
    _cr(je, pay_from, claim.total_amount, f"Paid: {claim.reference}")
    je.post(user=user)
    type(claim).objects.filter(pk=claim.pk).update(status="PAID", paid_at=paid_on)
    claim.status, claim.paid_at = "PAID", paid_on
    return je
