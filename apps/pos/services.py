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
from django.db.models import Q, Sum
from django.utils import timezone

from .models import POSCashMovement, POSSale, POSSaleLine, POSSession

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


# The roles that may act on a till that is not their own: close it for a
# cashier who walked off, refund a ticket by a method other than the one it was
# paid by. Kept to the people who answer for the cash — a cashier's drawer is
# that cashier's responsibility, and every override is something the cash-up
# has to explain.
SUPERVISOR_ROLES = ("ADMIN", "FINANCE_MANAGER", "SALES_MANAGER")

# What a refund can be paid by. A split tender is never taken, so it is never
# handed back either.
REFUND_METHODS = tuple(value for value, _ in POSSale.PAYMENT_METHODS if value != "SPLIT")


def by_pk(queryset, pk):
    """One row of the queryset by primary key, or None — never a crash.

    A posted id is whatever the browser sent. A word where a UUID belongs makes
    the database lookup raise rather than miss, and a till screen that answers a
    mistyped or tampered field with a server error is a till nobody can use.
    """
    text = "" if pk is None else str(pk).strip()
    if not text:
        return None
    try:
        return queryset.filter(pk=text).first()
    except (ValidationError, ValueError, TypeError):
        return None


def membership(company, user):
    """The user's active place in this company, or None."""
    if not company or not getattr(user, "pk", None):
        return None
    from apps.organizations.models import CompanyMembership
    return CompanyMembership.objects.filter(
        company=company, user=user, is_active=True).select_related("branch").first()


def is_supervisor(company, user):
    """May this user act on someone else's till? Superusers and company admins."""
    if getattr(user, "is_superuser", False):
        return True
    found = membership(company, user)
    return bool(found and found.role in SUPERVISOR_ROLES)


def may_handle(session, user):
    """The till's own cashier, or a supervisor standing in for them."""
    return (session.cashier_id == getattr(user, "pk", None)
            or is_supervisor(session.company, user))


def active_session(company, user):
    """The one till this cashier is standing at, or None."""
    if not company:
        return None
    return POSSession.objects.filter(
        company=company, cashier=user, status="OPEN").select_related("branch", "cashier").first()


def home_branch(company, user):
    """The branch a till should open at when nobody picked one.

    The cashier's own branch on their membership comes first: a Mombasa cashier
    opening a till without thinking about it is trading in Mombasa. Without one,
    a company with a single branch has only one answer. A company with several
    has no safe default — guessing would put the day's takings and the goods
    they emptied on the wrong branch — so None is returned and the cashier is
    asked to choose.
    """
    found = membership(company, user)
    if (found and found.branch_id and found.branch.is_active
            and found.branch.company_id == company.pk):
        return found.branch
    branches = list(company.branches.filter(is_active=True)[:2])
    return branches[0] if len(branches) == 1 else None


def open_session(company, user, branch=None, opening_cash=Z):
    """Start the day's trading; an already-open till is simply handed back.

    The float is what is in the drawer before the first customer, so the cash-up
    at night can say whether the drawer is short or over.

    A cashier whose membership names a branch trades from that branch: opening
    a till at another branch would let them sell off that branch's shelves and
    put its takings in their own drawer. A supervisor may open a till anywhere.
    """
    existing = active_session(company, user)
    if existing:
        return existing
    if branch is not None and branch.company_id != company.pk:
        raise ValidationError("That branch does not belong to this company.")
    own = membership(company, user)
    if branch is None:
        branch = home_branch(company, user)
        if branch is None:
            if not company.branches.filter(is_active=True).exists():
                raise ValidationError(
                    "This company has no branch to trade from — add one under Organization.")
            raise ValidationError(
                "Choose the branch this till trades from — the company has more than one, "
                "and the takings and the goods they empty belong to a branch.")
    elif (own and own.branch_id and own.branch_id != branch.pk
          and not is_supervisor(company, user)):
        raise ValidationError(
            f"You are set up to trade from {own.branch.name}, so your till opens there. "
            f"Ask an administrator to change your branch if you are working at {branch.name}.")
    session = POSSession.objects.create(
        company=company, branch=branch, cashier=user,
        opened_at=timezone.now(), status="OPEN",
        opening_cash=_counted(opening_cash, "Opening float"))
    retotal(session)
    return session


def _refuse_stranger(session, user, doing):
    """Only the till's cashier — or a supervisor — may close or count it."""
    if not may_handle(session, user):
        who = session.cashier.get_full_name() or session.cashier.username
        raise ValidationError(
            f"This till is {who}'s — only they can {doing} it. A company administrator "
            "can do it on their behalf if they cannot.")


@transaction.atomic
def close_session(session, closing_cash=None, *, user):
    """Stop the till. Counted cash is optional; the variance needs it.

    Only the cashier whose drawer it is may close it, or a supervisor standing
    in for them: closing a till records the count that proves the drawer, and a
    colleague closing it would both stop the cashier trading and put a count
    on their shift that they never made. Whoever closes it is recorded.

    When a count is given and it differs from what the tickets say the drawer
    should hold, the difference is booked to cash over and short at once, so the
    till account in the ledger holds what was actually counted.
    """
    session = POSSession.objects.select_for_update().select_related(
        "cashier", "company", "branch").get(pk=session.pk)
    _refuse_stranger(session, user, "close")
    if session.status == "CLOSED":
        raise ValidationError("This till is already closed.")
    if closing_cash is not None and str(closing_cash).strip() != "":
        session.closing_cash = _counted(closing_cash, "Cash counted")
    retotal(session)
    session.status = "CLOSED"
    session.closed_at = timezone.now()
    session.closed_by = user
    session.save()
    _book_variance(session, user)
    return session


@transaction.atomic
def record_count(session, closing_cash, *, user):
    """Put the count on a till that was closed without one.

    A shift is sometimes shut before anyone has counted the drawer (the cashier
    is called away, the safe is locked). The count still has to be recorded —
    until it is, the drawer is unproved and any shortage sits in the ledger as
    cash on hand. A count is recorded once: a counted drawer is evidence, and
    re-counting it would rewrite a figure the variance was already booked on.
    """
    session = POSSession.objects.select_for_update().select_related(
        "cashier", "company", "branch").get(pk=session.pk)
    _refuse_stranger(session, user, "count")
    if session.status != "CLOSED":
        raise ValidationError(
            "This till is still open — record the count when you close it from the "
            "session page.")
    if session.closing_cash is not None:
        raise ValidationError(
            f"This drawer was already counted at {session.closing_cash} and cannot be "
            "counted again.")
    if closing_cash is None or str(closing_cash).strip() == "":
        raise ValidationError("Type the cash counted in the drawer.")
    session.closing_cash = _counted(closing_cash, "Cash counted")
    if not session.closed_by_id:
        session.closed_by = user
    session.save(update_fields=["closing_cash", "closed_by", "updated_at"])
    _book_variance(session, user)
    return session


def _book_variance(session, user):
    """Write a counted drawer's difference off to cash over and short (once)."""
    from apps.accounting import gl_service
    gl_service.post_till_variance(session, user=user)
    session.refresh_from_db()


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
    # Payouts and pay-ins move the notes too: a float top-up is in the drawer
    # and a delivery fare paid over the counter is not.
    paid_out, paid_in = drawer_movements(session)
    session.expected_cash = money((session.opening_cash or Z) + taken - given
                                  - paid_out + paid_in)
    session.save(update_fields=["total_sales", "total_refunds", "expected_cash", "updated_at"])
    return session


def drawer_movements(session):
    """(paid out, paid in): the drawer's payouts and pay-ins this session."""
    rows = POSCashMovement.objects.filter(session=session)
    out = rows.filter(direction="OUT").aggregate(v=Sum("amount"))["v"] or Z
    into = rows.filter(direction="IN").aggregate(v=Sum("amount"))["v"] or Z
    return money(out), money(into)


def discount_percent(value, label="Discount"):
    """A line discount as typed: a percentage from 0 to 100, to two places."""
    pct = number(value, label, limit=D("1000"))
    pct = pct.quantize(D("0.01"), rounding=ROUND_HALF_UP)
    if pct < 0 or pct > 100:
        raise ValidationError(f"{label} has to be between 0 and 100 per cent.")
    return pct


def ticket_row(product, quantity, discount=Z):
    """One line as the till sees it: the shelf price, and the tax the product carries.

    A counter sale is charged the selling price on the card; a cashier never
    types a price. The one way below it is a line discount, a percentage off
    the shelf price, which only a supervisor may give (checkout enforces who).
    The line's net is the shelf price less the discount, and the VAT is worked
    out on that discounted net -- VAT is due on what the customer paid. Cost of
    sales is untouched: the goods leave the shelf at what they cost.
    """
    quantity = number(quantity, "quantity", limit=QUANTITY_LIMIT)
    if quantity <= 0:
        raise ValidationError("a line has to sell something.")
    if not product.selling_price:
        raise ValidationError(
            "has no selling price on file — the till cannot sell it until one is set.")
    text = "" if discount is None else str(discount).strip()
    pct = discount_percent(text) if text else Z
    if pct >= 100:
        raise ValidationError(
            "a 100% discount gives the goods away — the till sells, it does not give. "
            "Issue them from stock instead.")
    unit = D(str(product.selling_price))
    gross = money(unit * quantity)
    off = money(gross * pct / D("100")) if pct else Z
    net = money(gross - off)
    tax = money(net * (product.tax_rate.rate / D("100"))) if product.tax_rate_id else Z
    if net + tax <= 0:
        # A sliver of a cheap item rounds to nothing at the cent. Ringing it
        # would move goods off the shelf for no money and leave a ticket the
        # ledger cannot book, so it is refused here with the reason.
        raise ValidationError(
            f"{quantity} at {money(unit)} each comes to less than a cent — "
            "sell a larger quantity.")
    return {
        "product": product, "tax_rate": product.tax_rate if product.tax_rate_id else None,
        "quantity": quantity, "unit_price": unit,
        "discount_percent": pct, "discount": off,
        "net": net, "tax": tax, "total": net + tax,
    }


def build_ticket(company, quantities, discounts=None):
    """Price the quantities a cashier typed, skipping the boxes left empty.

    `discounts` ({product id: percent}) carries the line discounts; one
    against a product with no quantity goes with its empty line.
    """
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
            rows.append(ticket_row(product, text, (discounts or {}).get(str(pk))))
        except ValidationError as exc:
            raise ValidationError(f"{product.name}: {exc.messages[0]}")
    if not rows:
        raise ValidationError("Nothing to sell — enter a quantity against at least one product.")
    subtotal = money(sum((r["net"] for r in rows), Z))
    tax = money(sum((r["tax"] for r in rows), Z))
    return rows, subtotal, tax, money(subtotal + tax)


def till_warehouses(company, session):
    """The shelves a till may sell from: its own branch's, and any shared store.

    A warehouse that belongs to another branch is that branch's stock. A Head
    Office till selling off the Mombasa store would empty a shelf nobody at Head
    Office can see, while the takings land in a Nairobi drawer — the two
    branches' stock counts and cash-ups would both be wrong. A warehouse with no
    branch is a company-wide store any till may draw on.
    """
    from apps.organizations.models import Warehouse

    shelves = Warehouse.objects.filter(company=company, is_active=True)
    if session is not None and session.branch_id:
        shelves = shelves.filter(Q(branch=session.branch) | Q(branch__isnull=True))
    return shelves.order_by("-is_default", "name")


def issue_warehouse(company, session, wanted=None):
    """The shelf the goods come off: the one asked for, the till's own, or the default.

    Whatever is asked for has to be one of the till's own shelves (see
    till_warehouses); the default prefers the branch's own warehouse over a
    shared store.
    """
    from apps.organizations.models import Warehouse

    if wanted:
        found = by_pk(Warehouse.objects.filter(company=company), wanted)
        if not found:
            raise ValidationError("That warehouse does not belong to this company.")
        if not found.is_active:
            raise ValidationError(f"{found.name} is no longer in use — pick another shelf.")
        if not till_warehouses(company, session).filter(pk=found.pk).exists():
            where = found.branch.name if found.branch_id else "another branch"
            raise ValidationError(
                f"{found.name} is {where}'s stock and this till trades from "
                f"{session.branch.name} — sell from one of this branch's own shelves.")
        return found
    if session is not None and session.branch_id:
        found = till_warehouses(company, session).filter(branch=session.branch).first()
        if found:
            return found
    return till_warehouses(company, session).first()


def issue_stock(sale, rows, warehouse, movement_type, note, user=None, unit_costs=None):
    """Move the goods, and let the shelf price what they cost.

    The movement is created with no cost on purpose: the stock model values it at
    the moving average the shelf holds and books cost of sales against inventory,
    which is the same money either way — a cashier's guess at cost would only make
    the margin report disagree with the ledger.

    `unit_costs` ({product id: cost of one}) is the one exception: a refund
    brings goods back at the cost they went out at (see issued_unit_costs),
    which is a figure the book itself wrote, not one anybody typed.
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
                quantity=row["quantity"], unit_cost=(unit_costs or {}).get(product.pk, Z),
                movement_date=timezone.localdate(),
                reference=sale.reference, performed_by=user or sale.session.cashier,
                notes=f"{note} {sale.reference}",
            )
        except ValidationError as exc:
            # The shelf's own refusal, in one sentence the cashier can read.
            raise ValidationError(exc.messages[0])


def issued_unit_costs(sale):
    """What one of each product cost the book when this ticket sold it.

    The ticket's own delivery movements carry it: the shelf's average at the
    moment the goods left, which is what cost of sales was charged with. A
    refund puts the goods back at that figure. At the shelf they came off it
    makes no difference while its average has not moved, but taken back at
    another branch's counter the goods would otherwise come in at *that*
    shelf's average — a sugar sold at Nairobi's 120 and returned in Mombasa
    at 130 would put 10 more into inventory than ever left it, and take 10
    more off cost of sales than the sale charged, so a fully refunded ticket
    would leave a profit behind.
    """
    from apps.inventory.models import StockMovement

    # Read off each movement's unit cost (held to four places), never its
    # total (rounded to the cent): two at 104.1237 total 208.25, and 208.25 / 2
    # would put the return back at 104.125 — a shelf value the book never had.
    quantity, value = {}, {}
    for move in StockMovement.objects.filter(
            reference=sale.reference, movement_type="DELIVERY",
            product__company=sale.session.company):
        quantity[move.product_id] = quantity.get(move.product_id, Z) + move.quantity
        value[move.product_id] = value.get(move.product_id, Z) + move.quantity * move.unit_cost
    return {pk: (value[pk] / qty).quantize(D("0.0001"))
            for pk, qty in quantity.items() if qty}


def new_token():
    """A one-time token for a terminal or refund form, rendered as a hidden field."""
    import uuid
    return uuid.uuid4().hex


def clean_token(token):
    """What the browser sent back as the form token, trimmed to what is stored."""
    return ("" if token is None else str(token)).strip()[:64]


def submitted_ticket(session, token):
    """The ticket — sale or refund — this form token already made on this till.

    A double click or a reload of a slow POST sends the same form — and the
    same token — twice. The first submission rang the ticket; the second must
    hand that ticket back rather than take the money, move the goods and book
    the entry again. A form posted without a token (an older page, a script)
    could not be guarded, so the terminal and refund views refuse it outright
    and both forms always render one.

    The database holds a token once per till across sales and refunds alike,
    so the lookup does too: it is the caller's job to tell "the same form
    again" from "a token already spent on something else" (see token_refusal).
    """
    token = clean_token(token)
    if not token or session is None:
        return None
    return POSSale.objects.filter(session=session, submission_token=token).first()


def token_refusal(found, *, refund_of=None):
    """Why a form whose token is already spent on `found` must not go ahead, or "".

    "" means `found` is exactly what this form makes — the same ticket rung
    again, or the same refund of the same sale — and the caller hands it back.
    Anything else is a token used for another action (a terminal token sent
    with a refund, a refund token sent to the terminal) or for another sale's
    refund: taking the money would break the one-token-one-ticket rule, and
    handing back the other ticket would show the cashier the wrong thing. So
    it is refused, with a fresh form to start from.
    """
    if found is None:
        return ""
    if refund_of is None:
        if not found.refund_of_id:
            return ""
        return (f"That form's one-time token was already used for refund {found.reference}, "
                "so this ticket was not rung. Nothing was taken — ring it again from a fresh "
                "terminal screen.")
    if found.refund_of_id == refund_of.pk:
        return ""
    if found.refund_of_id:
        return (f"That form's one-time token was already used for refund {found.reference} "
                f"of another ticket, so nothing was refunded on {refund_of.reference}. Open "
                f"{refund_of.reference} again and refund it from its page.")
    return (f"That form's one-time token was already used to ring {found.reference}, so "
            f"nothing was refunded on {refund_of.reference}. Open {refund_of.reference} again "
            "and refund it from its page.")


def has_discount(discounts):
    """Did the post ask for any line discount at all (a non-zero figure)?"""
    for raw in (discounts or {}).values():
        text = "" if raw is None else str(raw).strip()
        if not text:
            continue
        try:
            if number(text, "Discount", limit=D("1000")) != 0:
                return True
        except ValidationError:
            return True
    return False


def company_people(company):
    """The users who work in this company: the salespeople a ticket may name."""
    from apps.accounts.models import User
    return User.objects.filter(
        memberships__company=company, memberships__is_active=True,
        is_active=True).distinct().order_by("first_name", "last_name", "username")


@transaction.atomic
def checkout(company, user, quantities, *, session=None, payment_method="CASH",
             tendered=Z, customer=None, warehouse=None, notes="", token="",
             discounts=None, salesperson=None, customer_reference=""):
    """Ring up a ticket: count the money, move the goods, book the entry.

    Returns the sale, with the change already worked out on its own row.

    `token` is the one-time token the terminal form was rendered with. It is
    stored on the ticket, and the database refuses a second ticket on the same
    till with the same token — so the same form submitted twice can only ever
    take the money once (see submitted_ticket).
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

    if has_discount(discounts) and not is_supervisor(company, user):
        # A discount is money the shop decides not to take; the person who
        # answers for the takings decides it, not the cashier ringing it.
        raise ValidationError(
            "Only a supervisor (an administrator, finance or sales manager) can give a "
            "discount at the till — nothing was rung. Ask one to apply it.")
    if salesperson is not None and not company_people(company).filter(
            pk=salesperson.pk).exists():
        raise ValidationError("That salesperson does not work in this company.")

    rows, subtotal, tax, total = build_ticket(company, quantities, discounts)
    discount_total = money(sum((r["discount"] for r in rows), Z))
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
        subtotal=subtotal, discount_amount=discount_total, tax_amount=tax, total_amount=total,
        payment_method=payment_method,
        amount_tendered=handed, change_given=change,
        status="COMPLETED", notes=notes, submission_token=clean_token(token),
        salesperson=salesperson,
        customer_reference=(customer_reference or "").strip()[:100],
    )
    for row in rows:
        POSSaleLine.objects.create(
            sale=sale, product=row["product"], tax_rate=row["tax_rate"],
            quantity=row["quantity"], unit_price=row["unit_price"],
            discount_percent=row["discount_percent"], discount_amount=row["discount"],
            tax_amount=row["tax"], line_total=money(row["net"] + row["tax"]),
        )
    issue_stock(sale, rows, shelf, "DELIVERY", "Sold at the till", user=user)

    from apps.accounting import gl_service
    gl_service.post_pos_sale(sale, user=user)
    retotal(session)
    sale.refresh_from_db()
    return sale


@transaction.atomic
def refund(sale, user, quantities, *, reason="", payment_method=None, token=""):
    """Take goods back and hand the money over the counter again.

    The refund is a ticket of its own rather than an edit of the sale: the money
    that came in and the money that went back are two events, and the till has to
    show both. It is priced at what the customer actually paid on the line, never
    at today's shelf price, and can never give back more than the ticket took.

    The money comes out of the drawer of the person handing it back. A refund is
    booked on the refunder's own open till — never on the till that rang the
    sale, which may belong to another cashier or already be counted and closed.
    Any cashier may take back goods sold at another till (a customer returns to
    whichever counter is open), but the notes they pay out are theirs to account
    for at their own cash-up. Without an open till of their own there is no
    drawer to pay from, so the refund is refused.

    The money goes back the way it came in: a card or M-Pesa sale was paid into
    the bank, so paying it back in notes would take cash out of a drawer that
    never received it. Only a company administrator may refund by another
    method.

    Each line's refunds are worked out on the running total rather than one by
    one: the money returned so far on a line is always the line's own figure
    times the share of its quantity that has come back, rounded once. Rounding
    each partial refund on its own would let three refunds of a third each pay
    back a cent or two more than the line took; on the running total, the last
    refund is exactly the remainder.
    """
    from apps.core.models import SequenceCounter

    company = sale.session.company
    if sale.refund_of_id:
        raise ValidationError(f"{sale.reference} is itself a refund — refund the original ticket.")
    if sale.status == "REFUNDED":
        raise ValidationError(f"{sale.reference} has already been refunded in full.")

    till = active_session(company, user)
    if not till:
        raise ValidationError(
            "You have no open till to pay this refund from — open your own session first. "
            "The money comes out of the drawer of the person handing it back.")
    if till.status != "OPEN":
        raise ValidationError(f"Your till is {till.get_status_display()} and cannot pay out.")

    original = sale.payment_method
    method = (str(payment_method).strip().upper() if payment_method else "") or original
    if method not in REFUND_METHODS:
        raise ValidationError(
            f"“{payment_method}” is not a way this till pays money back — choose one of "
            + ", ".join(label for value, label in POSSale.PAYMENT_METHODS
                        if value in REFUND_METHODS) + ".")
    if method != original and not is_supervisor(company, user):
        raise ValidationError(
            f"{sale.reference} was paid by {sale.get_payment_method_display()}, so it is "
            f"refunded by {sale.get_payment_method_display()}: the money goes back the way "
            "it came in. Only a company administrator can refund it another way.")

    # Lock the ticket so two refunds of it at two counters are worked out one
    # after the other, each seeing what the other already gave back.
    sale = POSSale.objects.select_for_update().get(pk=sale.pk)
    done_qty, done_net, done_tax = {}, {}, {}
    for line in POSSaleLine.objects.filter(sale__refund_of=sale):
        key = line.product_id
        done_qty[key] = done_qty.get(key, Z) + line.quantity
        done_net[key] = done_net.get(key, Z) + line.net_total
        done_tax[key] = done_tax.get(key, Z) + (line.tax_amount or Z)

    rows = []
    for line in sale.lines.select_related("product"):
        raw = str((quantities or {}).get(str(line.pk), "") or "").strip()
        if raw in ("", "0"):
            continue
        quantity = number(raw, f"Refund quantity for {line.product.name}", limit=QUANTITY_LIMIT)
        if quantity <= 0:
            continue
        key = line.product_id
        left = line.quantity - done_qty.get(key, Z)
        if quantity > left:
            raise ValidationError(
                f"Only {left} of {line.product.name} on {sale.reference} is still returnable.")
        # What this line has paid back once these goods are in, worked out on
        # the running quantity and rounded once — minus what was already paid.
        share = (done_qty.get(key, Z) + quantity) / line.quantity
        net = money(line.net_total * share) - done_net.get(key, Z)
        # The tax a customer paid comes back with the goods, on the same basis.
        tax = money((line.tax_amount or Z) * share) - done_tax.get(key, Z)
        # Never below nothing, and never more than the line still holds — a
        # refund rung before this rule may already have taken a cent too many.
        net = max(Z, min(net, line.net_total - done_net.get(key, Z)))
        tax = max(Z, min(tax, (line.tax_amount or Z) - done_tax.get(key, Z)))
        if net + tax <= 0 and line.line_total > 0:
            raise ValidationError(
                f"{quantity} of {line.product.name} is worth less than a cent back — "
                "refund a larger quantity.")
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
    if money((sale.refunded_amount or Z) + total) > money(sale.total_amount):
        raise ValidationError(
            f"{sale.reference} took {sale.total_amount} and {sale.refunded_amount} has "
            f"already gone back — {total} more would repay more than the ticket took.")
    if method == "CASH" and money(till.expected_cash) < total:
        raise ValidationError(
            f"Your drawer holds {money(till.expected_cash)} and cannot pay back {total}.")

    # The goods go back on the shelf they came off when the refunding till may
    # stock it; a ticket brought back to another branch's counter is shelved
    # there, where the goods physically are.
    shelf = sale.warehouse
    if not shelf or not till_warehouses(company, till).filter(pk=shelf.pk).exists():
        shelf = issue_warehouse(company, till)

    ticket = POSSale.objects.create(
        session=till,
        reference=SequenceCounter.next_number(company, "POS-R"),
        customer=sale.customer, warehouse=shelf,
        subtotal=subtotal, discount_amount=Z, tax_amount=tax, total_amount=total,
        payment_method=method, amount_tendered=total, change_given=Z,
        status="REFUNDED", refund_of=sale, notes=reason,
        submission_token=clean_token(token),
    )
    for row in rows:
        POSSaleLine.objects.create(
            sale=ticket, product=row["product"], tax_rate=row["tax_rate"],
            quantity=row["quantity"], unit_price=row["unit_price"],
            discount_amount=Z, tax_amount=row["tax"], line_total=money(row["net"] + row["tax"]),
        )
    # Back on the shelf at what each one cost when the sale took it off —
    # whichever branch's shelf it now goes on — so cost of sales is credited
    # with exactly what the sale charged it and a full refund leaves stock
    # and margin where they stood before the sale.
    issue_stock(ticket, rows, shelf, "RETURN_IN", "Returned to stock on", user=user,
                unit_costs=issued_unit_costs(sale))

    from apps.accounting import gl_service
    gl_service.post_pos_sale(ticket, user=user, refund=True)

    returned = money((sale.refunded_amount or Z) + total)
    sale.refunded_amount = returned
    sale.status = "REFUNDED" if returned >= sale.total_amount else "PARTIAL_REFUND"
    sale.save(update_fields=["refunded_amount", "status", "updated_at"])
    retotal(till)
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
    paid_out, paid_in = drawer_movements(session)
    drawer = money((session.opening_cash or Z) + totals("CASH")[0] - totals("CASH")[1]
                   - paid_out + paid_in)
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
        "cash_taken": totals("CASH")[0],
        "cash_given": totals("CASH")[1],
        "paid_out": paid_out,
        "paid_in": paid_in,
        "movements": list(POSCashMovement.objects.filter(session=session)
                          .select_related("account", "performed_by", "journal_entry")
                          .order_by("created_at")),
        "expected_cash": drawer,
        "counted_cash": money(counted) if counted is not None else None,
        "variance": money(counted - drawer) if counted is not None else None,
        "total_sales": money(session.total_sales),
        "total_refunds": money(session.total_refunds),
        "ticket_count": POSSale.objects.filter(session=session, refund_of__isnull=True).count(),
        "refund_count": POSSale.objects.filter(session=session).exclude(refund_of=None).count(),
    }


# ─────────────────────────── payouts and pay-ins ────────────────────────────

# The kinds of ledger account a payout or a pay-in may name as its other side.
# A payout is an expense paid over the counter (fare, airtime, cleaning) or
# notes taken to the safe or the bank; a pay-in is a float top-up, from the
# owner (equity) or from the safe or the bank. Receivables and payables are
# left out: a customer's or supplier's money is allocated on its own screen.
MOVEMENT_ACCOUNT_TYPES = {
    "OUT": ("EXPENSE", "BANK", "CASH"),
    "IN": ("EQUITY", "BANK", "CASH"),
}
# The default other side: general operating expenses for a payout, the
# owner's float (opening equity) for a pay-in.
MOVEMENT_DEFAULT_CODES = {"OUT": ("6000",), "IN": ("3100", "3000")}


def till_cash_account(company):
    """The ledger account a cash ticket debits: the drawer (1100)."""
    from apps.accounting.models import Account
    return (Account.objects.filter(company=company, code="1100", is_active=True).first()
            or Account.objects.filter(company=company, account_type="CASH",
                                      is_active=True).order_by("code").first())


def movement_accounts(company, direction):
    """The accounts a payout (OUT) or pay-in (IN) may be booked against."""
    from apps.accounting.models import Account
    types = MOVEMENT_ACCOUNT_TYPES.get(direction, ())
    accounts = Account.objects.filter(company=company, is_active=True, account_type__in=types)
    drawer = till_cash_account(company)
    if drawer is not None:
        # The drawer against itself would move nothing.
        accounts = accounts.exclude(pk=drawer.pk)
    return accounts.order_by("code")


def default_movement_account(company, direction):
    allowed = movement_accounts(company, direction)
    for code in MOVEMENT_DEFAULT_CODES.get(direction, ()):
        found = allowed.filter(code=code).first()
        if found:
            return found
    kind = "EXPENSE" if direction == "OUT" else "EQUITY"
    return allowed.filter(account_type=kind).first() or allowed.first()


def submitted_movement(session, token):
    token = clean_token(token)
    if not token or session is None:
        return None
    return POSCashMovement.objects.filter(session=session, submission_token=token).first()


@transaction.atomic
def record_cash_movement(session, user, direction, amount, reason, account=None, *, token=""):
    """Take notes out of the drawer (OUT) or put them in (IN), and book it.

    Only the till's cashier, or a supervisor standing in for them, may move
    the drawer's money, and only while the till is open: a counted, closed
    drawer is evidence. A payout can never take out more than the drawer is
    expected to hold -- the same rule a cash refund keeps. The form's one-time
    token makes a double click hand back the movement it already made.
    """
    from apps.core.models import SequenceCounter
    from apps.accounting import gl_service

    session = POSSession.objects.select_for_update().select_related(
        "company", "cashier", "branch").get(pk=session.pk)
    company = session.company
    _refuse_stranger(session, user, "pay money in or out of")
    if session.status != "OPEN":
        raise ValidationError(
            f"This till is {session.get_status_display()} — money can only be paid in or out "
            "of an open drawer.")
    direction = (direction or "").strip().upper()
    if direction not in dict(POSCashMovement.DIRECTIONS):
        raise ValidationError("Choose whether the money is paid out of the drawer or into it.")
    token = clean_token(token)
    if not token:
        raise ValidationError(
            "That form had no one-time token, so it could not be protected against being "
            "sent twice. Nothing was moved — open the payout form again.")
    already = submitted_movement(session, token)
    if already:
        return already
    amount = money(amount, "Amount")
    if amount <= 0:
        raise ValidationError("The amount has to be more than nothing.")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Say what the money is for — the cash-up has to explain it.")
    allowed = movement_accounts(company, direction)
    if account is None or account == "":
        account = default_movement_account(company, direction)
    elif not hasattr(account, "pk"):
        account = by_pk(allowed, account)
        if account is None:
            raise ValidationError("That account cannot be the other side of a drawer movement.")
    if account is None or not allowed.filter(pk=account.pk).exists():
        raise ValidationError("That account cannot be the other side of a drawer movement.")
    retotal(session)
    if direction == "OUT" and money(session.expected_cash) < amount:
        raise ValidationError(
            f"The drawer should hold {money(session.expected_cash)} and cannot pay out {amount}.")
    movement = POSCashMovement.objects.create(
        session=session, direction=direction, amount=amount, reason=reason[:255],
        account=account, performed_by=user, submission_token=token,
        reference=SequenceCounter.next_number(company, "POS-M"),
    )
    gl_service.post_pos_cash_movement(movement, user=user)
    retotal(session)
    movement.refresh_from_db()
    return movement


# ─────────────────────────── item lookup ────────────────────────────────────

LOOKUP_PAGE_SIZE = 25


def category_tree(company):
    """{id: (name, parent id)} for the company's product categories."""
    from apps.inventory.models import ProductCategory
    return {c["pk"]: (c["name"], c["parent_id"]) for c in ProductCategory.objects.filter(
        company=company).values("pk", "name", "parent_id")}


def category_with_descendants(company, category_id):
    """The category and every category under it, as a set of ids (empty if unknown)."""
    tree = category_tree(company)
    wanted = next((pk for pk in tree if str(pk) == str(category_id).strip()), None)
    if wanted is None:
        return set()
    found, frontier = {wanted}, {wanted}
    while frontier:
        nxt = {pk for pk, (_, parent) in tree.items() if parent in frontier and pk not in found}
        found |= nxt
        frontier = nxt
    return found


def top_category(tree, category_id):
    """The level-1 ancestor of a category (itself when it has no parent)."""
    seen = set()
    current = category_id
    while current in tree and tree[current][1] and current not in seen:
        seen.add(current)
        current = tree[current][1]
    return current


def search_products(company, q="", category=None, exact=False):
    """The company's active products matching a code or name, optionally in a category."""
    from apps.inventory.models import Product

    products = Product.objects.filter(company=company, is_active=True).select_related(
        "tax_rate", "unit_of_measure", "category")
    q = (q or "").strip()
    if q:
        if exact:
            products = products.filter(Q(sku__iexact=q) | Q(barcode=q))
        else:
            products = products.filter(
                Q(name__icontains=q) | Q(sku__icontains=q) | Q(barcode__icontains=q))
    if category not in (None, ""):
        products = products.filter(category_id__in=category_with_descendants(company, category))
    return products.order_by("name", "sku")


def product_card(product, on_hand=None):
    """A product as the till's screens see it (JSON-safe)."""
    rate = product.tax_rate.rate if product.tax_rate_id else Z
    return {
        "id": str(product.pk),
        "sku": product.sku,
        "barcode": product.barcode,
        "name": product.name,
        "unit": product.unit_of_measure.abbreviation if product.unit_of_measure_id else "",
        "price": str(money(product.selling_price or Z)),
        "unit_price": str(D(str(product.selling_price or 0))),
        "tax_rate": str(rate),
        "type": product.product_type,
        "tracked": bool(product.track_inventory),
        "category": str(product.category_id) if product.category_id else "",
        "on_hand": (str(on_hand if on_hand is not None else Z)
                    if product.track_inventory else None),
        "image": product.image.url if product.image else "",
    }
