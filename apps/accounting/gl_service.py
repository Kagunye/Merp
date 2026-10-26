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

from django.core.exceptions import ValidationError
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

def _to_cents_summing_to(amounts, total):
    """Round each amount to the cent so that the rounded parts add up to `total`.

    The header of an invoice is what the customer reads and pays, and it is
    rounded once on the whole (three lines of KES 0.1648 VAT are KES 0.49 of
    tax, not 3 x 0.16). The journal lines have to say the same thing, so each
    part is rounded and whatever cent is left over between the parts and the
    header goes on the largest part.
    """
    from apps.sales.models import cents
    rounded = [cents(a) for a in amounts]
    residue = cents(total) - sum(rounded, Decimal("0"))
    if residue and rounded:
        heaviest = max(range(len(amounts)), key=lambda i: abs(amounts[i]))
        rounded[heaviest] += residue
    return rounded


def _invoice_targets(invoice, default_revenue, default_vat):
    """The revenue lines and the tax lines an invoice of this shape is worth.

    Returned as two lists of (account, amount, description), stated in the
    invoice's own currency and already rounded so that revenue adds up to the
    invoice's subtotal and tax to its tax figure, cent for cent. The caller
    converts each set at the invoice's booked rate. Shared with the credit
    note so a return can never book against different accounts — or different
    cents — from the sale it answers.
    """
    revenue, tax = [], []
    for line in invoice.lines.select_related(
        "product", "product__income_account", "tax_rate", "tax_rate__account"
    ):
        revenue_acct = (
            (line.product and line.product.income_account) or default_revenue
        )
        line_desc = line.description or (line.product.name if line.product else "Item")
        revenue.append((revenue_acct, line.line_total, line_desc))

        if line.tax_amount and line.tax_amount > 0:
            tax_acct = (
                (line.tax_rate and line.tax_rate.account) or default_vat
            )
            tax.append((tax_acct, line.tax_amount, f"VAT: {invoice.reference}"))

    if not revenue:
        # A header-only invoice still owes somebody its revenue.
        return [(default_revenue, invoice.total_amount,
                 f"Revenue: {invoice.reference}")], []

    def settle(targets, header):
        amounts = _to_cents_summing_to([t[1] for t in targets], header)
        return [(acct, amount, desc) for (acct, _raw, desc), amount in zip(targets, amounts)]

    return settle(revenue, invoice.subtotal), settle(tax, invoice.tax_amount)


def _invoice_entry_amounts(invoice, rate, total_base, default_revenue, default_vat):
    """Every revenue and VAT line of an invoice's entry, in the book currency.

    The VAT is the invoice's own tax figure at the booked rate — USD 4.80 at
    131.25 is KES 630.00, which is what the tax return will be asked for — and
    revenue takes the rest of the receivable. Translating line by line and
    letting the cents fall where they may would put KES 630.63 on VAT for a
    paper that says 4.80, and leave AR holding 63 cents nobody owes once the
    invoice is paid.
    """
    revenue, tax = _invoice_targets(invoice, default_revenue, default_vat)
    tax_base = to_base(sum((t[1] for t in tax), Decimal("0")), rate) if tax else Decimal("0")
    rows = []
    for targets, whole in ((tax, tax_base), (revenue, total_base - tax_base)):
        if not targets:
            continue
        bases = spread([t[1] for t in targets], rate, whole)
        rows += [(account, base, desc) for (account, _f, desc), base in zip(targets, bases)]
    return rows

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
    rate booked on it. VAT is the invoice's own (rounded) tax figure at that
    rate and revenue is the rest of the receivable, so the entry says to the
    cent what the paper says; a cent left between lines lands on the largest.

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

    _dr(je, ar_account, total_base, f"AR: {invoice.reference}")
    for account, base, desc in _invoice_entry_amounts(
            invoice, rate, total_base, default_revenue, default_vat):
        _cr(je, account, base, desc)

    je.post(user=user)

    # A direct invoice for stocked goods is also the issue note: nothing else
    # will ever take those units off the shelf or charge what they cost. An
    # invoice raised from an order issues nothing — the order's delivery did
    # that already, and doing it again would sell the same goods twice.
    issue_invoice_stock(invoice, user=user)

    # Link back to the invoice and mark it as SENT
    from apps.sales.models import Invoice as _Invoice
    _Invoice.objects.filter(pk=invoice.pk).update(
        journal_entry=je,
        status="SENT",
    )
    invoice.journal_entry = je
    invoice.status = "SENT"
    return je


# ───────────────────────── Stock on sales documents ──────────────────────────

def _issue_warehouse(invoice):
    """The shelf a direct invoice's goods leave from: the one chosen on the
    invoice, else the company's default, else its first warehouse by name."""
    from apps.organizations.models import Warehouse
    if invoice.warehouse_id:
        return invoice.warehouse
    warehouses = Warehouse.objects.filter(company=invoice.company)
    return warehouses.filter(is_default=True).first() or warehouses.order_by("name").first()


def _stocked_lines(document):
    return [line for line in document.lines.select_related("product")
            if line.product_id and line.product.track_inventory]


def stock_shortages(invoice, warehouse=None):
    """What a direct invoice wants off the shelf that the shelf does not hold.

    Returned as a list of sentences, empty when everything is there. Read
    before posting so the person invoicing is told plainly which product is
    short and by how much, instead of meeting a half-posted invoice.
    """
    from apps.inventory.models import StockLevel
    if invoice.sales_order_id or invoice.original_invoice_id:
        return []
    lines = _stocked_lines(invoice)
    if not lines:
        return []
    warehouse = warehouse or _issue_warehouse(invoice)
    if warehouse is None:
        return ["this company has no warehouse to issue the goods from — set one "
                "up under Inventory before invoicing stocked products"]
    wanted = {}
    for line in lines:
        product, qty = wanted.get(line.product_id, (line.product, Decimal("0")))
        wanted[line.product_id] = (product, qty + (line.quantity or Decimal("0")))
    short = []
    for product, qty in wanted.values():
        if product.allow_negative_stock:
            continue
        level = StockLevel.objects.filter(product=product, warehouse=warehouse).first()
        have = level.quantity if level else Decimal("0")
        if have < qty:
            short.append(
                f"{product.name}: {_qty_text(qty)} invoiced but only {_qty_text(have)} "
                f"in {warehouse.name}")
    return short


def issue_invoice_stock(invoice, user=None):
    """Take a direct invoice's stocked goods off the shelf, at average cost.

    One DELIVERY movement per stocked line, dated with the invoice and carrying
    its number. Each movement prices itself at the shelf's moving average and
    books Dr Cost of Goods Sold / Cr Inventory (post_stock_delivery), which is
    exactly what an order's delivery books — so a sale costs the same whether
    it went through an order or straight onto an invoice.

    Nothing is issued for an invoice raised from an order (its delivery
    issues the goods), for a credit note, or twice for the same invoice. Short
    stock is refused with a ValidationError naming what is missing, and the
    posting it is part of rolls back with it.
    """
    from apps.inventory.models import StockMovement
    if invoice.sales_order_id or invoice.original_invoice_id:
        return []
    lines = _stocked_lines(invoice)
    if not lines:
        return []
    if StockMovement.objects.filter(
            product__company=invoice.company, movement_type="DELIVERY",
            sales_order__isnull=True, reference=invoice.reference).exists():
        return []
    warehouse = _issue_warehouse(invoice)
    short = stock_shortages(invoice, warehouse)
    if short:
        raise ValidationError(
            f"Not enough stock to invoice {invoice.reference}: " + "; ".join(short)
            + ". Receive the goods first, choose a warehouse that holds them, or "
            "raise a sales order and deliver when the stock arrives.")
    movements = []
    for line in lines:
        movements.append(StockMovement.objects.create(
            product=line.product, warehouse=warehouse, movement_type="DELIVERY",
            quantity=line.quantity, movement_date=invoice.invoice_date,
            reference=invoice.reference, performed_by=user,
            notes=f"Issued on invoice {invoice.reference}",
        ))
    return movements


def _issues_for(invoice, product):
    """The movements that took this invoice's goods of `product` off the shelf.

    Goods on an order's invoice left with the order's deliveries; goods on a
    direct invoice left on the invoice's own issue, which carries its number.
    """
    from apps.inventory.models import StockMovement
    qs = StockMovement.objects.filter(movement_type="DELIVERY", product=product)
    if invoice.sales_order_id:
        return qs.filter(sales_order_id=invoice.sales_order_id)
    return qs.filter(sales_order__isnull=True, reference=invoice.reference,
                     product__company=invoice.company)


def return_credit_note_stock(note, user=None):
    """Put the goods a credit note takes back onto the shelf they left.

    Each stocked line comes back as a RETURN_IN movement priced at the average
    cost of the issues that sent those goods out — not at today's shelf
    average, which may have moved since. post_stock_receipt then books
    Dr Inventory / Cr Cost of Goods Sold for that value, reversing the cost
    charged at the sale.

    Refused (ValidationError) when the goods never left the shelf against the
    invoice — an order invoiced before it was delivered — or when this return
    and earlier ones would bring back more than was issued.
    """
    from apps.inventory.models import StockMovement
    from apps.sales.models import Invoice as _Invoice
    original = note.original_invoice
    returned = []
    for line in _stocked_lines(note):
        product = line.product
        issues = list(_issues_for(original, product))
        issued_qty = sum((m.quantity for m in issues), Decimal("0"))
        if issued_qty <= 0:
            raise ValidationError(
                f"No {product.name} has left the warehouse against {original.reference}, "
                "so there is nothing to take back into stock. Untick 'goods returned' "
                "to credit the price only, or deliver the order first.")
        earlier_notes = list(_Invoice.objects.filter(
            original_invoice=original, status="CREDIT_NOTE", goods_returned=True,
        ).exclude(pk=note.pk).values_list("reference", flat=True))
        back_already = sum((m.quantity for m in StockMovement.objects.filter(
            movement_type="RETURN_IN", product=product, reference__in=earlier_notes,
            product__company=original.company)), Decimal("0"))
        if back_already + line.quantity > issued_qty:
            raise ValidationError(
                f"Only {_qty_text(issued_qty)} {product.name} left the warehouse against "
                f"{original.reference} and {_qty_text(back_already)} came back already, so "
                f"{_qty_text(line.quantity)} more cannot be returned to stock.")
        issued_value = sum((m.total_cost or Decimal("0") for m in issues), Decimal("0"))
        unit_cost = (issued_value / issued_qty).quantize(Decimal("0.0001"))
        returned.append(StockMovement.objects.create(
            product=product, warehouse=issues[0].warehouse, movement_type="RETURN_IN",
            quantity=line.quantity, unit_cost=unit_cost,
            movement_date=note.invoice_date, reference=note.reference,
            performed_by=user,
            notes=f"Returned on credit note {note.reference} against {original.reference}",
        ))
    return returned


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

    When the note says the goods came back (`goods_returned`), the units go
    back on the shelf they left from at the cost they were issued at:

    DR  Inventory                          quantity x original issue cost
    CR  Cost of Goods Sold                 the same

    so the cost charged when they were sold is taken back exactly. A note that
    only corrects the price moves no stock and books no cost.
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
    # The same cents the note shows, exactly as an invoice of the same lines
    # would have posted them, only the other way round.
    for account, base, desc in _invoice_entry_amounts(
            note, rate, total_base, default_revenue, default_vat):
        _dr(je, account, base, desc)
    _cr(je, ar_account, total_base, f"AR credit: {note.reference}")

    je.post(user=user)

    if note.goods_returned:
        # Goods that came back are stock again, at what they cost when they
        # left; a price-only credit moves nothing.
        return_credit_note_stock(note, user=user)

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
    charge = (original.expense_account
              if original and original.expense_account_id else inventory_acct)
    if entry is None or entry.status != "POSTED":
        return _bill_targets(note, charge, vat_input_acct), note.rate

    # Every line of the bill except the payable it raised, netted per account:
    # a bill billed below its order credits price variance, and a note against
    # it has to give back its share of that credit too, or the tax it returns
    # would no longer be the tax the bill charged.
    lines = list(entry.lines.exclude(account__account_type="PAYABLE")
                 .select_related("account").order_by("account__code", "id"))
    charged = {}
    for line in lines:
        row = charged.setdefault(line.account_id, [line.account, Decimal("0"), line.description])
        row[1] += (line.debit_amount or Decimal("0")) - (line.credit_amount or Decimal("0"))
    rows = [row for row in charged.values() if row[1]]
    note_base = note.total_amount_base or to_base(note.total_amount, note.rate)
    billed = sum((row[1] for row in rows), Decimal("0"))
    if not rows or billed <= 0 or note_base <= 0:
        return _bill_targets(note, charge, vat_input_acct), note.rate

    # The tax comes back at exactly the tax the note carries, converted the way
    # the note itself is: scaling the bill's VAT line instead rounds twice (once
    # in the bill's currency, once in ours) and leaves VAT input a few cents
    # away from the tax the paper says was reversed. The rest of the note is
    # shared across the other lines in the bill's own proportions.
    Z = Decimal("0")
    vat_base = Z
    if vat_input_acct is not None and note.tax_amount:
        if any(account.pk == vat_input_acct.pk for account, _a, _d in rows):
            vat_base = min(to_base(note.tax_amount, note.rate), note_base)
    others = sum((amount for account, amount, _d in rows
                  if not (vat_base and account.pk == vat_input_acct.pk)), Z)
    rest = note_base - vat_base
    targets = []
    for account, amount, description in rows:
        if vat_base and account.pk == vat_input_acct.pk:
            value = vat_base
        elif others:
            value = (amount * rest / others).quantize(Decimal("0.01"))
        else:
            continue
        targets.append((account, value,
                        f"Debit note {note.reference}: {description or account.name}"))
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

    The accrual is the exception. A bill that cleared the received-not-invoiced
    accrual closed it: the receipt put the goods on the shelf at the order's
    price and the bill answered that receipt in full. A note against it is the
    supplier lowering what those goods cost, not a receipt being undone — no
    quantity comes off the shelf, because a header-level note does not say
    which goods or how many. Crediting the accrual would re-open it on an order
    that is finished, with nothing ever coming to clear it. So the accrual's
    share is shown as the bill charged it and then handed straight on to
    purchase price variance, the same account a bill above or below the order
    price lands its difference on. Stock is held at the price it came in at
    throughout, and the inventory account keeps agreeing with the shelf. Goods
    that physically go back are taken off the shelf with a stock movement of
    their own.
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
    original = note.original_bill
    matched = bool(original and original.purchase_order_id)
    variance = None
    for (account, _amount, desc), base in zip(targets, bases):
        if base < 0:
            # The bill credited this account (it was billed below the order
            # price), so the note's share of that credit is given back.
            _dr(je, account, -base, desc)
            continue
        if account.code == NOT_BILLED[0]:
            variance = variance or ensure_account(company, *PRICE_VARIANCE)
            _cr(je, account, base, desc)
            _dr(je, account, base,
                f"Accrual stays closed — {note.reference} lowers the price, not the receipt")
            _cr(je, variance, base, f"Price reduced on {note.reference}")
        elif matched and account.account_type == "ASSET":
            # An order-matched bill never brings stock in (the receipt did), so a
            # stock line here is old paper; the reduction is a price variance too.
            variance = variance or ensure_account(company, *PRICE_VARIANCE)
            _cr(je, variance, base, f"Price reduced on {note.reference}")
        else:
            _cr(je, account, base, desc)

    je.post(user=user)

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
                if inv.invoice_date and payment.payment_date < inv.invoice_date:
                    # Money cannot settle a bill that did not exist yet. A receipt
                    # dated before its invoice is either a mistyped date or a
                    # deposit — and a deposit is money on account until the
                    # invoice is raised and the credit applied to it.
                    raise ValidationError(
                        f"Receipt {payment.reference} is dated {payment.payment_date:%d %b %Y}, "
                        f"before invoice {inv.reference} ({inv.invoice_date:%d %b %Y}); "
                        "a payment can only settle an invoice dated on or before it. "
                        "Correct the date, or leave it on account and apply it once the "
                        "invoice exists.")
                allocations.append(
                    (inv, alloc_amount, to_base(alloc_amount, inv.rate)))
    else:
        outstanding = (
            _Invoice.objects
            .filter(
                company=company,
                customer=payment.customer,
                status__in=["SENT", "PARTIAL", "OVERDUE"],
                # Only papers that existed on the day the money came in: an
                # invoice raised later is settled by applying the credit then.
                invoice_date__lte=payment.payment_date,
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
    je.post(user=user)

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


@transaction.atomic
def apply_customer_credit(payment, invoice_allocations, on_date, user=None):
    """Match money already on a customer's account to invoices they owe.

    A receipt that settled nothing (or not all of itself) left its money as a
    credit in AR at the rate it came in at. Applying it later follows the same
    rules as allocating on the receipt form: the invoice must be this
    customer's, open, in the receipt's own currency, dated on or before the day
    the credit is applied, and owed no less than it is given; the slices
    together cannot use more than is still unapplied.

    In the book's money the credit is released at the receipt's rate and the
    invoice cleared at its own. When those differ the gap is realised exchange,
    booked in one entry dated the day of application:

    DR  Accounts Receivable (1300)        the credit released, at the receipt's rate
    CR  Accounts Receivable (1300)        the invoices cleared, at their own rates
    CR/DR Exchange gain 4310 / loss 7300  the difference

    At the same rate there is nothing to book — the credit and the invoice
    already net to nothing in AR — and only the allocation rows are written.

    Returns (journal entry or None, [(invoice, amount, base)]). Raises
    ValidationError saying what is wrong otherwise.
    """
    from apps.sales.models import CustomerPayment, Invoice as _Invoice, PaymentAllocation
    from apps.sales.receivables import unapplied_amount

    payment = (CustomerPayment.objects.select_for_update()
               .select_related("customer", "company").get(pk=payment.pk))
    company = payment.company
    if payment.status != "RECEIVED" or not payment.journal_entry_id:
        raise ValidationError(
            f"{payment.reference} is not a live posted receipt, so it has no credit to apply.")
    if on_date < payment.payment_date:
        raise ValidationError(
            f"Credit from {payment.reference} cannot be applied on {on_date:%d %b %Y}: "
            f"the money only arrived on {payment.payment_date:%d %b %Y}.")
    before = unapplied_amount(payment)
    wanted = [(inv, amt) for inv, amt in invoice_allocations if amt and amt > 0]
    if not wanted:
        raise ValidationError("Enter an amount against at least one invoice.")
    total = sum((amt for _inv, amt in wanted), Decimal("0"))
    if total > before:
        raise ValidationError(
            f"The invoices add up to {total:.2f} {payment.currency} but only "
            f"{before:.2f} of {payment.reference} is still unapplied.")

    rows = []
    for inv, amount in wanted:
        inv = _Invoice.objects.select_for_update().get(pk=inv.pk)
        if inv.company_id != company.pk or inv.customer_id != payment.customer_id:
            raise ValidationError(
                f"{inv.reference} is not {payment.customer.name}'s invoice — credit is "
                "only applied within the same customer's account.")
        if inv.status not in ("SENT", "PARTIAL", "OVERDUE") or inv.outstanding_amount <= 0:
            raise ValidationError(f"{inv.reference} is not open, so nothing on it can be settled.")
        if (inv.currency or "").upper() != (payment.currency or "").upper():
            raise ValidationError(
                f"{inv.reference} is billed in {inv.currency} and {payment.reference} is "
                f"{payment.currency}: credit only settles an invoice in the same money.")
        if amount > inv.outstanding_amount:
            raise ValidationError(
                f"{inv.reference} is only owed {inv.outstanding_amount:.2f} — it cannot "
                f"take {amount:.2f}.")
        if on_date < inv.invoice_date:
            raise ValidationError(
                f"{inv.reference} is dated {inv.invoice_date:%d %b %Y}; credit cannot "
                f"settle it on {on_date:%d %b %Y}, before it existed.")
        rows.append((inv, amount, to_base(amount, inv.rate)))

    rate = payment.rate
    # Released as the difference of the two remainders, so whatever stays on
    # account is still worth exactly to_base(remaining, rate) in AR — the
    # figure every receivables report reads it at.
    released = to_base(before, rate) - to_base(before - total, rate)
    cleared = sum((base for _inv, _amt, base in rows), Decimal("0"))

    je = None
    if released != cleared:
        ar_account = (_get_account_safe(company, "1300")
                      or _get_account_by_type(company, "RECEIVABLE"))
        if not ar_account:
            raise ValidationError("Accounts Receivable (1300) is missing from the chart.")
        je = _new_je(
            company, "JE-APP-", "PAYMENT", on_date,
            f"Credit from {payment.reference} applied — {payment.customer.name}",
            source_currency=payment.currency, exchange_rate=rate,
        )
        refs = ", ".join(inv.reference for inv, _a, _b in rows)
        _dr(je, ar_account, released, f"On-account credit released: {payment.reference}")
        _cr(je, ar_account, cleared, f"AR clear: {refs}")
        _book_exchange_difference(
            je, company, released - cleared,
            f"Realised exchange on applying {payment.reference}")
        je.post(user=user)

    for inv, amount, base in rows:
        PaymentAllocation.objects.create(
            payment=payment, invoice=inv, allocated_amount=amount, base_amount=base,
            allocation_date=on_date, journal_entry=je,
        )
        inv.paid_amount = (inv.paid_amount or Decimal("0")) + amount
        inv.save()
    return je, rows


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

    # The day the ticket was rung is the day in Nairobi, not in UTC: a ticket
    # rung at 01:30 belongs to today's takings, the same day its goods left the
    # shelf on (stock movements are dated with localdate too). Reading the date
    # off the stored UTC timestamp would put everything rung between midnight
    # and 03:00 into the previous day's revenue — and, at a month end, into the
    # previous month.
    je = _new_je(
        company, "JE-RFND-" if refund else "JE-POS-",
        "PAYMENT" if refund else "RECEIPT", _pos_day(sale.created_at),
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
        raise ValidationError(
            "A ticket worth nothing books nothing — every line has to be worth at "
            "least a cent.")
    if refund:
        _cr(je, counter, total, f"Money back over the counter: {sale.reference}")
    else:
        _dr(je, counter, total, f"Counter receipt: {sale.reference}")
    je.post(user=user)

    from apps.pos.models import POSSale as _Sale
    _Sale.objects.filter(pk=sale.pk).update(journal_entry=je)
    sale.journal_entry = je
    return je


def _pos_day(moment):
    """The business day a till event belongs to: its date in the book's own
    time zone (Africa/Nairobi), never the UTC date it is stored under."""
    return timezone.localdate(moment) if moment else timezone.localdate()


# Where a drawer that does not count out to its tickets is written off. An
# expense rather than a loss on any one sale: nobody can say which ticket the
# missing notes went with, only that the shift as a whole is short or over.
CASH_OVER_SHORT = ("6750", "Cash Over and Short", "EXPENSE", "6000")


@transaction.atomic
def post_till_variance(session, user=None) -> Optional[JournalEntry]:
    """Book the gap between what a drawer held and what its tickets said.

    A drawer counted short                   A drawer counted over
    DR  cash over and short (6750)           DR  the till (1100)
    CR  the till (1100)                      CR  cash over and short (6750)

    Every cash ticket debited the till with the bill, so until the count is
    booked the ledger still shows the missing notes as cash on hand — the
    balance sheet would claim money the drawer does not have. The count is the
    fact; the entry brings the till account down (or up) to it, against an
    expense account the owner can read as "what the counter lost this month".

    Booked once per session: a session that already carries its variance entry
    hands that entry back, so a repeated close or a late count can never write
    the same shortage off twice. A drawer that counted out exactly, or was
    never counted, books nothing.
    """
    from apps.pos.models import POSSession

    session = POSSession.objects.select_for_update().select_related(
        "company", "cashier", "variance_entry").get(pk=session.pk)
    if session.variance_entry_id:
        return session.variance_entry
    variance = session.variance
    if variance is None:
        return None
    variance = Decimal(variance).quantize(Decimal("0.01"))
    if variance == 0:
        return None

    company = session.company
    # The same account a cash ticket debits, so the write-off lands on the
    # money it is correcting.
    drawer = _get_account_safe(company, "1100") or _get_account_by_type(company, "CASH")
    if not drawer:
        raise ValueError(
            "GL posting failed: no till account (1100) to write the drawer's variance off.")
    over_short = ensure_account(company, *CASH_OVER_SHORT)
    who = session.cashier.get_full_name() or session.cashier.username
    je = _new_je(
        company, "JE-TILL-", "MANUAL", _pos_day(session.closed_at),
        f"Till {'short' if variance < 0 else 'over'} by {abs(variance)} at close — {who}, "
        f"{session.branch.name}: counted {session.closing_cash} against "
        f"{session.expected_cash} expected",
    )
    if variance < 0:
        _dr(je, over_short, -variance, f"Drawer short at cash-up — {who}")
        _cr(je, drawer, -variance, f"Notes missing from the drawer — {who}")
    else:
        _dr(je, drawer, variance, f"Notes over in the drawer — {who}")
        _cr(je, over_short, variance, f"Drawer over at cash-up — {who}")
    je.post(user=user)
    POSSession.objects.filter(pk=session.pk).update(variance_entry=je)
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
        # Header-level fallback
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

    Entries created, for a bill raised from a purchase order
    --------------------------------------------------------
    DR  Stock Received Not Invoiced (2110) what the receipt accrued for the
                                           quantities this bill answers
    DR/CR Purchase Price Variance (5400)   the bill's goods value less that
    DR  VAT Input (code 2210, if exists)   bill.tax_amount
    CR  Accounts Payable (2100)            bill.total_amount

    A bill for goods that are already in the warehouse does not add them to
    stock again — it releases the accrual the receipt put up, and exactly that
    accrual: the receipt valued the goods at the order's price, so that is what
    sits in 2110 for them. When the supplier's invoice asks for more or less
    than the order agreed, the difference is a price variance and is booked as
    one. Stock stays at the price it was received at, so the inventory account
    keeps agreeing with the shelf, the accrual closes to the cent on an order
    that is fully billed, and the variance account shows in one place how far
    suppliers' invoices strayed from the orders they answer.

    Entries created, for a bill with no order
    -----------------------------------------
    DR  the account the bill was coded to  bill.subtotal (bill.expense_account)
    DR  VAT Input (code 2210, if exists)   bill.tax_amount
    CR  Accounts Payable (2100)            bill.total_amount

    A bill with no order behind it brings nothing onto the shelf — goods only
    arrive through a receipt — so it is charged to the expense or asset account
    the bookkeeper chose. Coded to Stock Received Not Invoiced, it answers goods
    that were received without an order, and records the accrual it released.

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

    # A bill with no order is charged where the bookkeeper coded it. Older
    # bills, entered before bills carried an account, keep the stock account
    # they were always posted to.
    charge_acct = (bill.expense_account
                   if not bill.purchase_order_id and bill.expense_account_id
                   else inventory_acct)
    targets = _bill_targets(bill, charge_acct, vat_input_acct)

    _cr(je, ap_account, total_base, f"AP: {bill.reference}")
    bases = spread([t[1] for t in targets], rate, total_base)
    cleared = Decimal("0")
    if bill.purchase_order_id:
        # Goods that arrived before this bill were put into stock on the day
        # they came in, against the received-not-invoiced accrual. This bill is
        # the supplier asking to be paid for that same stock, so it clears the
        # accrual instead of charging the cost to stock a second time.
        goods = Decimal("0")
        for (account, _foreign, desc), base in zip(targets, bases):
            if account is vat_input_acct:
                _dr(je, account, base, desc)
            else:
                goods += base
        remaining = max(accrual_remaining(bill.purchase_order), Decimal("0"))
        # What the receipt accrued for the quantities this bill was raised
        # for. A bill from before that figure was kept falls back on whatever
        # the order still has standing, as it always did.
        due = bill.accrual_to_clear if bill.accrual_to_clear else min(goods, remaining)
        cleared = min(due, remaining)
        if cleared > 0:
            _dr(je, ensure_account(company, *NOT_BILLED), cleared,
                f"Goods received and now billed: {bill.reference}")
        variance = goods - cleared
        if variance:
            ppv = ensure_account(company, *PRICE_VARIANCE)
            if variance > 0:
                _dr(je, ppv, variance,
                    f"Billed above the order price: {bill.reference}")
            else:
                _cr(je, ppv, -variance,
                    f"Billed below the order price: {bill.reference}")
    else:
        for (account, _foreign, desc), base in zip(targets, bases):
            _dr(je, account, base, desc)
            if account.code == NOT_BILLED[0] and account is not vat_input_acct:
                # Goods taken in without an order and billed now: the bill
                # releases the accrual their receipt put up.
                cleared += base
    je.post(user=user)

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
# Where the gap between an order's price and the supplier's invoice goes. Stock
# is carried at the price it was received at; a supplier who bills more (or
# less) than the order agreed changes what the purchase cost, not what the
# goods on the shelf are worth, so the difference is a cost of its own.
PRICE_VARIANCE = ("5400", "Purchase Price Variance", "COST_OF_GOODS", "5000")


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


@transaction.atomic
def post_production_rounding(company, difference, date, reference, user=None) -> Optional[JournalEntry]:
    """Empty work in progress of the cents a finished batch could not carry.

    A work order's output is valued at what its materials cost, divided into a
    unit cost held to four places. Multiplied back out, that unit cost can miss
    the materials by a cent or two, and the miss would sit in 1450 for ever. A
    positive difference is materials that went in and did not come out; a
    negative one is the reverse. Either way it is a stock adjustment, booked so
    work in progress closes to nothing once the order is complete.
    """
    difference = to_base(difference or Decimal("0"), Decimal("1"))
    if not difference:
        return None
    wip = ensure_account(company, *WIP)
    adjust = ensure_account(company, *STOCK_ADJUSTMENTS)
    je = _new_je(
        company, "JE-WIP-", "STOCK_ADJUSTMENT", date,
        f"Rounding on completion of {reference}",
    )
    if difference > 0:
        _dr(je, adjust, difference, f"Rounding on {reference}")
        _cr(je, wip, difference, f"Work in progress cleared: {reference}")
    else:
        _dr(je, wip, -difference, f"Work in progress cleared: {reference}")
        _cr(je, adjust, -difference, f"Rounding on {reference}")
    je.post(user=user)
    return je


# ──────────────────────── Expense Claim GL ───────────────────────────────────

@transaction.atomic
def post_expense_claim(claim, bank_account=None, user=None, payment_date=None) -> JournalEntry:
    """
    Post an approved expense claim payment to the GL.

    DR  Expense account per line   (ExpenseCategory.gl_account or 6000)
    CR  Bank / Cash                claim.total_amount  (bank_account.gl_account or 1100)

    Parameters
    ----------
    claim        : expenses.ExpenseClaim
    bank_account : banking.BankAccount  (the petty cash / bank account used to pay)
    user         : accounts.User
    payment_date : the day the money leaves (defaults to today)

    The entry is dated the day the claim is paid, not the day it was claimed.
    Nothing moves in the ledger when an employee hands in receipts: the bank
    balance changes on the day the money leaves it, and that is the day the bank
    statement will show. Dating the entry on the claim date puts a September
    payment into August — into a month that may already be reconciled or
    closed — and the bank never agrees with the book. The claim date stays on
    the claim, and on the entry's narrative, as the date of the document.

    Returns the JournalEntry.
    """
    company = claim.company
    paid_on = payment_date or timezone.localdate()

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
        company, "JE-EXP-", "MANUAL", paid_on,
        f"Expense Claim {claim.reference} (claimed {claim.claim_date:%d %b %Y}, "
        f"paid {paid_on:%d %b %Y})",
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
        paid_at=paid_on,
    )
    claim.status = "PAID"
    claim.paid_at = paid_on
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
    # The form only offers the right kinds of account, but a run can be saved
    # by other means, and the ledger is where a wrong choice does its damage: a
    # month charged to the bank debits the bank with the whole gross and
    # credits it with the net, so the bank looks richer by the deductions and
    # no salary cost is ever reported.
    if salary_acct.account_type != "EXPENSE":
        raise ValueError(
            f"GL posting failed: '{run.reference}' charges its pay to {salary_acct.code} "
            f"{salary_acct.name}, which is a {salary_acct.get_account_type_display()} "
            "account. Pay is a cost — choose an expense account on the run, or leave "
            "it blank for 6100 Salaries & Wages.")
    if net_acct.account_type not in ("BANK", "CASH"):
        raise ValueError(
            f"GL posting failed: '{run.reference}' pays the net out of {net_acct.code} "
            f"{net_acct.name}, which is a {net_acct.get_account_type_display()} account. "
            "Take-home pay leaves a bank or cash account — choose one on the run, or "
            "leave it blank for the bank.")

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

    je.post(user=user)
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


def _closed_year_refusal(fiscal_year):
    """Why a period of this year cannot change state on its own, or ""."""
    if fiscal_year.is_closed:
        return (f"{fiscal_year.name} is closed, so its periods are locked with it. "
                "Reopen the fiscal year first: reopening one month inside a closed "
                "year would leave its result in retained earnings and in the month "
                "at the same time.")
    return ""


@transaction.atomic
def reopen_period(company, period, user=None):
    """Reverse the period's closing entry and hand the months their figures back."""
    from django.core.exceptions import ValidationError

    refusal = _closed_year_refusal(period.fiscal_year)
    if refusal:
        raise ValidationError(refusal)
    entry = closing_entry_for(company, PERIOD_CLOSE, period.pk)
    if entry:
        entry.reverse(user=user)
    period.status = "OPEN"
    period.save(update_fields=["status", "updated_at"])
    return entry


def income_statement_balances(company, as_at):
    """Every profit and loss account's balance at the close of `as_at`.

    Unlike period_activity this counts the closing entries too: it answers
    "what is still sitting in the account", which is what a year end has to
    leave at nothing. A late adjustment filed into a month that was already
    closed shows up here and nowhere else, because the month's own closing
    entry was drawn before the adjustment existed.
    """
    rows = (
        JournalEntryLine.objects.filter(
            account__company=company,
            account__account_type__in=INCOME_STATEMENT_TYPES,
            journal_entry__status="POSTED",
            journal_entry__posting_date__lte=as_at,
        )
        .values("account_id", "account__code", "account__name")
        .annotate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        .order_by("account__code")
    )
    balances = []
    for row in rows:
        net = (row["dr"] or Decimal("0")) - (row["cr"] or Decimal("0"))
        if net:
            balances.append({
                "account_id": row["account_id"],
                "code": row["account__code"],
                "name": row["account__name"],
                "net": net,
            })
    return balances


@transaction.atomic
def close_fiscal_year(company, fiscal_year, user=None):
    """Close every open period, then carry the year's whole result to retained earnings.

    The year end entry does two things. It empties current year profit as it
    stood on the last day of the year — dated, because a later year's closed
    January also lands in 3300 and is that year's business, not this one's.
    And it sweeps whatever is still left in a profit and loss account at the
    year end straight to retained earnings: a late adjustment filed into a
    month after that month was closed is real profit for the year, and leaving
    it behind would start next year with last year's sales still on the books.
    Afterwards every income and expense account reads nothing as at the year
    end, and retained earnings has moved by exactly the year's profit.

    Years close in order. An earlier year still open would have its own
    profit sitting in 3300 at this year's end, and sweeping it here and again
    when that year closes would count it twice.
    """
    from django.core.exceptions import ValidationError
    from apps.organizations.models import FiscalYear

    earlier = (FiscalYear.objects
               .filter(company=company, is_closed=False,
                       start_date__lt=fiscal_year.start_date)
               .exclude(pk=fiscal_year.pk).order_by("start_date").first())
    if earlier:
        raise ValidationError(
            f"{earlier.name} is still open. Years are closed in order, so close "
            f"{earlier.name} before {fiscal_year.name}.")

    entries = []
    for period in fiscal_year.periods.filter(status="OPEN").order_by("start_date"):
        entry = close_period(company, period, user=user)
        if entry:
            entries.append(entry)

    year_end = fiscal_year.end_date
    profit_account = _get_account_safe(company, "3300")
    retained = (
        _get_account_safe(company, "3200") or _get_account_by_type(company, "EQUITY")
    )
    if profit_account and retained and profit_account.pk == retained.pk:
        # A chart with a single equity account keeps the year's result in it
        # already; there is nothing to move from one to the other.
        profit_account = None
    carried = profit_account.balance_as_at(year_end) if profit_account else Decimal("0")
    residual = income_statement_balances(company, year_end)

    if carried or residual:
        if not retained:
            raise ValueError(
                "Closing a year needs a Retained Earnings account (code 3200).")
        year_entry = _new_je(
            company, "JE-YE-", "CLOSING", year_end,
            f"Year end {fiscal_year.name}: "
            + (f"{profit_account.code} " if profit_account else "")
            + f"to {retained.code} {retained.name}",
        )
        profit = carried
        for row in residual:
            account = Account.objects.get(pk=row["account_id"])
            label = f"Year end {fiscal_year.name}: {row['code']} posted after its month closed"
            if row["net"] < 0:
                _dr(year_entry, account, -row["net"], label)
            else:
                _cr(year_entry, account, row["net"], label)
            profit -= row["net"]
        if carried > 0:
            _dr(year_entry, profit_account, carried, f"Clear {profit_account.name}")
        elif carried < 0:
            _cr(year_entry, profit_account, -carried, f"Clear {profit_account.name}")
        if profit > 0:
            _cr(year_entry, retained, profit, f"Retained {fiscal_year.name}")
        elif profit < 0:
            _dr(year_entry, retained, -profit, f"Cover {fiscal_year.name} loss")
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
    """Undo a year end: unlock, then reverse the sweep and every period close.

    The year is unlocked before anything is reversed, because the reversals
    are dated inside it and a closed year accepts no postings. A later year
    that is already closed has to be reopened first: its own year end was
    drawn on the figures this year carried forward.
    """
    from django.core.exceptions import ValidationError
    from apps.organizations.models import FiscalYear

    later = (FiscalYear.objects
             .filter(company=company, is_closed=True,
                     start_date__gt=fiscal_year.start_date)
             .exclude(pk=fiscal_year.pk).order_by("-start_date").first())
    if later:
        raise ValidationError(
            f"{later.name} is closed, and its year end was drawn on what "
            f"{fiscal_year.name} carried forward. Reopen {later.name} first.")

    fiscal_year.is_closed = False
    fiscal_year.save(update_fields=["is_closed", "updated_at"])
    fiscal_year.periods.filter(status="LOCKED").update(status="CLOSED")

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
    return reversed_entries
