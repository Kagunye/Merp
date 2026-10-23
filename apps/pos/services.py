"""The till: opening a session, ringing a ticket, and handing the money back.

A counter sale is the one sale in this book that is paid for on the spot, so it
never touches receivables. The money over the counter is debited to the till — or
to the bank, for a card slip or an M-Pesa message — against the revenue the ticket
carried and the tax on it, and the goods leave the shelf at the cost the shelf
actually holds. Those two halves are one event: a ticket whose ledger entry failed
is rolled back together with its stock movements, because a till that says one
thing while the book says another is a cash-up nobody can explain at closing time.

Prices come off the product card and the money is counted once: every figure is
rounded to the cent as it is worked out, and the whole ticket is refused if the
cash in the drawer would not cover it.
"""
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import POSSale, POSSaleLine, POSSession

D = Decimal
Z = D("0")


# Larger than any till figure or quantity this book can store (money columns
# hold 13 digits before the point, quantities 8).
MONEY_LIMIT = D("1e13")
QUANTITY_LIMIT = D("1e8")
_THOUSANDS = re.compile(r"-?\d{1,3}(,\d{3})+(\.\d+)?")


def number(value, label="Amount", *, limit=MONEY_LIMIT, blank=Z) -> Decimal:
    """What a cashier typed, as a number — or a message saying why it isn't one.

    Accepts "1,250" (thousands separators). Refuses words, "NaN", "Infinity" and
    absurd sizes: Postgres would store NaN in a money column and every total
    after it would be NaN too.
    """
    text = "" if value is None else str(value).strip()
    if text == "":
        return blank
    if _THOUSANDS.fullmatch(text):
        text = text.replace(",", "")
    try:
        result = D(text)
    except InvalidOperation:
        raise ValidationError(f"{label}: “{text}” is not a number.")
    if not result.is_finite() or abs(result) >= limit:
        raise ValidationError(f"{label}: “{text}” is not a usable figure.")
    return result


def money(value, label="Amount") -> Decimal:
    """The figure as it can actually be paid: to the cent, half up."""
    return number(value, label).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def _counted(value, label):
    """Cash counted into or out of a drawer: never below zero."""
    amount = money(value, label)
    if amount < 0:
        raise ValidationError(f"{label} can't be negative.")
    return amount


def active_session(company, user):
    """The one till this cashier is standing at, or None."""
    if not company:
        return None
    return POSSession.objects.filter(
        company=company, cashier=user, status="OPEN").select_related("branch", "cashier").first()


def open_session(company, user, branch=None, opening_cash=Z):
    """Start the day's trading; an already-open till is simply handed back.

    The float is what is in the drawer before the first customer, so the cash-up
    at night can say whether the drawer is short or over.
    """
    existing = active_session(company, user)
    if existing:
        return existing
    branch = branch or company.branches.first()
    if not branch:
        raise ValidationError(
            "This company has no branch to trade from — add one under Organization.")
    session = POSSession.objects.create(
        company=company, branch=branch, cashier=user,
        opened_at=timezone.now(), status="OPEN",
        opening_cash=_counted(opening_cash, "Opening float"))
    retotal(session)
    return session


def close_session(session, closing_cash=None):
    """Stop the till. Counted cash is optional; the variance needs it."""
    if session.status == "CLOSED":
        raise ValidationError("This till is already closed.")
    if closing_cash is not None and str(closing_cash).strip() != "":
        session.closing_cash = _counted(closing_cash, "Cash counted")
    session.status = "CLOSED"
    session.closed_at = timezone.now()
    session.save()
    return session


def retotal(session):
    """Bring the till's running figures back in line with its tickets.

    Recomputed from the sales rather than nudged by each one, so a ticket that
    failed part-way through cannot leave the session claiming a sale that never
    happened. Only cash moves the drawer: a card slip is the bank's business.
    """
    tickets = POSSale.objects.filter(session=session, refund_of__isnull=True)
    refunds = POSSale.objects.filter(session=session).exclude(refund_of=None)
    session.total_sales = money(tickets.aggregate(v=Sum("total_amount"))["v"] or Z)
    session.total_refunds = money(refunds.aggregate(v=Sum("total_amount"))["v"] or Z)
    taken = money(tickets.filter(payment_method="CASH").aggregate(v=Sum("total_amount"))["v"] or Z)
    given = money(refunds.filter(payment_method="CASH").aggregate(v=Sum("total_amount"))["v"] or Z)
    session.expected_cash = money((session.opening_cash or Z) + taken - given)
    session.save(update_fields=["total_sales", "total_refunds", "expected_cash", "updated_at"])
    return session


def ticket_row(product, quantity):
    """One line as the till sees it: the shelf price, and the tax the product carries.

    A counter sale is charged the selling price on the card. There is no price
    list and no negotiated discount at a till, and a cashier typing a cheaper
    figure would be a loss the book could never explain.
    """
    quantity = number(quantity, "quantity", limit=QUANTITY_LIMIT)
    if quantity <= 0:
        raise ValidationError("a line has to sell something.")
    if not product.selling_price:
        raise ValidationError(
            "has no selling price on file — the till cannot sell it until one is set.")
    unit = D(str(product.selling_price))
    net = money(unit * quantity)
    tax = money(net * (product.tax_rate.rate / D("100"))) if product.tax_rate_id else Z
    return {
        "product": product, "tax_rate": product.tax_rate if product.tax_rate_id else None,
        "quantity": quantity, "unit_price": unit,
        "net": net, "tax": tax, "total": net + tax,
    }


def build_ticket(company, quantities):
    """Price the quantities a cashier typed, skipping the boxes left empty."""
    from apps.inventory.models import Product

    products = Product.objects.filter(
        company=company, is_active=True).select_related(
        "tax_rate", "income_account").order_by("name")
    by_pk = {str(p.pk): p for p in products}
    rows = []
    for pk, raw in (quantities or {}).items():
        text = "" if raw is None else str(raw).strip()
        if text in ("", "0"):
            continue
        product = by_pk.get(str(pk))
        if not product:
            # Someone posted a line for a product this company does not trade in.
            raise ValidationError("That product is not on this company's list.")
        try:
            rows.append(ticket_row(product, text))
        except ValidationError as exc:
            raise ValidationError(f"{product.name}: {exc.messages[0]}")
    if not rows:
        raise ValidationError("Nothing to sell — enter a quantity against at least one product.")
    subtotal = money(sum((r["net"] for r in rows), Z))
    tax = money(sum((r["tax"] for r in rows), Z))
    return rows, subtotal, tax, money(subtotal + tax)


def issue_warehouse(company, session, wanted=None):
    """The shelf the goods come off: the one asked for, the till's own, or the default."""
    from apps.organizations.models import Warehouse

    if wanted:
        found = Warehouse.objects.filter(pk=wanted, company=company).first()
        if not found:
            raise ValidationError("That warehouse does not belong to this company.")
        return found
    if session.branch_id:
        found = Warehouse.objects.filter(
            branch=session.branch, company=company, is_active=True).order_by(
            "-is_default", "name").first()
        if found:
            return found
    return Warehouse.objects.filter(company=company, is_active=True).order_by(
        "-is_default", "name").first()


def issue_stock(sale, rows, warehouse, movement_type, note, user=None):
    """Move the goods, and let the shelf price what they cost.

    The movement is created with no cost on purpose: the stock model values it at
    the moving average the shelf holds and books cost of sales against inventory,
    which is the same money either way — a cashier's guess at cost would only make
    the margin report disagree with the ledger.
    """
    from apps.inventory.models import StockMovement

    if not warehouse:
        raise ValidationError(
            "There is no warehouse to take stock from — add one under Organization.")
    for row in rows:
        product = row["product"]
        if not product.track_inventory:
            continue
        try:
            StockMovement.objects.create(
                product=product, warehouse=warehouse, movement_type=movement_type,
                quantity=row["quantity"], unit_cost=Z,
                movement_date=timezone.localdate(),
                reference=sale.reference, performed_by=user or sale.session.cashier,
                notes=f"{note} {sale.reference}",
            )
        except ValidationError as exc:
            # The shelf's own refusal, in one sentence the cashier can read.
            raise ValidationError(exc.messages[0])


@transaction.atomic
def checkout(company, user, quantities, *, session=None, payment_method="CASH",
             tendered=Z, customer=None, warehouse=None, notes=""):
    """Ring up a ticket: count the money, move the goods, book the entry.

    Returns the sale, with the change already worked out on its own row.
    """
    from apps.core.models import SequenceCounter

    session = session or active_session(company, user)
    if not session:
        raise ValidationError(
            "There is no open till for you to sell from — open a session first.")
    if session.status != "OPEN":
        raise ValidationError(f"This till is {session.get_status_display()} and cannot sell.")
    if payment_method not in dict(POSSale.PAYMENT_METHODS):
        raise ValidationError(f"{payment_method} is not a payment method this till takes.")
    if payment_method == "SPLIT":
        # Two tenders on one ticket would be two receipts in the drawer and the
        # cash-up cannot prove either, so each part is rung as its own ticket.
        raise ValidationError(
            "A split tender is not taken here yet — ring each part as its own ticket.")

    rows, subtotal, tax, total = build_ticket(company, quantities)
    handed_text = "" if tendered is None else str(tendered).strip()
    if payment_method != "CASH":
        # A card slip or an M-Pesa message pays the bill it was quoted for: there
        # is no tendered figure to authorise and no change to hand back.
        handed = total
    elif handed_text == "":
        # Counting out the exact bill is the common case at a counter, so an empty
        # tender box means "paid in full" rather than a refusal to take the sale.
        handed = total
    else:
        handed = money(handed_text, "Cash tendered")
        if handed < total:
            raise ValidationError(
                f"Only {handed} tendered for a bill of {total} — the drawer cannot "
                "give change it does not have.")
    change = money(handed - total)

    shelf = issue_warehouse(company, session, warehouse)
    sale = POSSale.objects.create(
        session=session,
        reference=SequenceCounter.next_number(company, "POS-"),
        customer=customer, warehouse=shelf,
        subtotal=subtotal, discount_amount=Z, tax_amount=tax, total_amount=total,
        payment_method=payment_method,
        amount_tendered=handed, change_given=change,
        status="COMPLETED", notes=notes,
    )
    for row in rows:
        POSSaleLine.objects.create(
            sale=sale, product=row["product"], tax_rate=row["tax_rate"],
            quantity=row["quantity"], unit_price=row["unit_price"],
            discount_amount=Z, tax_amount=row["tax"], line_total=money(row["net"] + row["tax"]),
        )
    issue_stock(sale, rows, shelf, "DELIVERY", "Sold at the till", user=user)

    from apps.accounting import gl_service
    gl_service.post_pos_sale(sale, user=user)
    retotal(session)
    sale.refresh_from_db()
    return sale


@transaction.atomic
def refund(sale, user, quantities, *, reason="", payment_method=None):
    """Take goods back and hand the money over the counter again.

    The refund is a ticket of its own rather than an edit of the sale: the money
    that came in and the money that went back are two events, and the till has to
    show both. It is priced at the unit price the customer actually paid, never at
    today's shelf price, and can never give back more than the ticket took.
    """
    from apps.core.models import SequenceCounter

    company = sale.session.company
    if sale.refund_of_id:
        raise ValidationError(f"{sale.reference} is itself a refund — refund the original ticket.")
    if sale.status == "REFUNDED":
        raise ValidationError(f"{sale.reference} has already been refunded in full.")

    already = {}
    for line in POSSaleLine.objects.filter(sale__refund_of=sale):
        already[line.product_id] = already.get(line.product_id, Z) + line.quantity

    rows = []
    for line in sale.lines.select_related("product"):
        raw = str((quantities or {}).get(str(line.pk), "") or "").strip()
        if raw in ("", "0"):
            continue
        quantity = number(raw, f"Refund quantity for {line.product.name}", limit=QUANTITY_LIMIT)
        if quantity <= 0:
            continue
        left = line.quantity - already.get(line.product_id, Z)
        if quantity > left:
            raise ValidationError(
                f"Only {left} of {line.product.name} on {sale.reference} is still returnable.")
        net = money(line.unit_price * quantity)
        # The tax a customer paid comes back with the goods: it was charged on
        # this line at this price, so it is repaid on the same basis.
        tax = money(line.tax_amount * (quantity / line.quantity)) if line.tax_amount else Z
        rows.append({
            "product": line.product, "tax_rate": line.tax_rate,
            "quantity": quantity, "unit_price": line.unit_price,
            "net": net, "tax": tax, "total": net + tax,
        })
    if not rows:
        raise ValidationError("Nothing was given back — enter a quantity to refund.")

    subtotal = money(sum((r["net"] for r in rows), Z))
    tax = money(sum((r["tax"] for r in rows), Z))
    total = money(subtotal + tax)
    method = payment_method or sale.payment_method
    if method == "CASH" and money(sale.session.expected_cash) < total:
        raise ValidationError(
            f"The drawer holds {money(sale.session.expected_cash)} and cannot pay back {total}.")

    ticket = POSSale.objects.create(
        session=sale.session,
        reference=SequenceCounter.next_number(company, "POS-R"),
        customer=sale.customer, warehouse=sale.warehouse,
        subtotal=subtotal, discount_amount=Z, tax_amount=tax, total_amount=total,
        payment_method=method, amount_tendered=total, change_given=Z,
        status="REFUNDED", refund_of=sale, notes=reason,
    )
    for row in rows:
        POSSaleLine.objects.create(
            sale=ticket, product=row["product"], tax_rate=row["tax_rate"],
            quantity=row["quantity"], unit_price=row["unit_price"],
            discount_amount=Z, tax_amount=row["tax"], line_total=money(row["net"] + row["tax"]),
        )
    # The goods go back on the shelf they came off, at what the shelf counts one
    # as worth now — the same rule the sale was issued under.
    issue_stock(ticket, rows, sale.warehouse, "RETURN_IN", "Returned to stock on", user=user)

    from apps.accounting import gl_service
    gl_service.post_pos_sale(ticket, user=user, refund=True)

    returned = money((sale.refunded_amount or Z) + total)
    sale.refunded_amount = returned
    sale.status = "REFUNDED" if returned >= sale.total_amount else "PARTIAL_REFUND"
    sale.save(update_fields=["refunded_amount", "status", "updated_at"])
    retotal(sale.session)
    ticket.refresh_from_db()
    return ticket


def till_report(session):
    """What the drawer should hold, by how the money was taken.

    Card and mobile slips belong to the bank rather than to the till, so a cash-up
    counts only the notes that came and went over the counter — while still
    showing the slips the bank has to agree with.
    """
    def totals(method):
        taken = POSSale.objects.filter(
            session=session, payment_method=method,
            refund_of__isnull=True).aggregate(v=Sum("total_amount"))["v"] or Z
        given = POSSale.objects.filter(
            session=session, payment_method=method).exclude(
            refund_of=None).aggregate(v=Sum("total_amount"))["v"] or Z
        return money(taken), money(given)

    counted = session.closing_cash
    drawer = money((session.opening_cash or Z) + totals("CASH")[0] - totals("CASH")[1])
    methods = []
    for value, label in POSSale.PAYMENT_METHODS:
        taken, given = totals(value)
        if taken == Z and given == Z:
            continue
        methods.append({"method": value, "label": label, "taken": taken,
                        "given": given, "net": money(taken - given)})
    return {
        "methods": methods,
        "opening_cash": money(session.opening_cash or Z),
        "expected_cash": drawer,
        "counted_cash": money(counted) if counted is not None else None,
        "variance": money(counted - drawer) if counted is not None else None,
        "total_sales": money(session.total_sales),
        "total_refunds": money(session.total_refunds),
        "ticket_count": POSSale.objects.filter(session=session, refund_of__isnull=True).count(),
        "refund_count": POSSale.objects.filter(session=session).exclude(refund_of=None).count(),
    }
