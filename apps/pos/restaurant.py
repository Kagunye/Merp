"""Restaurant layer of the till: stations, menus, happy hours, PIN staff,
open bills, manager voids and the day end.

A bill is only ever money once it is closed, and it is closed by ringing it
through an open till session (services.checkout_lines). Everything before
that (ordering, voiding, printing the bill) moves no stock and books nothing,
so a voided order costs the ledger nothing and a closed one is booked exactly
like a counter sale.
"""
import datetime
import uuid
from decimal import ROUND_HALF_UP, Decimal

from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from . import services
from .models import (POSDayEnd, POSMenuItem, POSOrder, POSOrderLine, POSPriceRule, POSSale,
                     POSSaleLine, POSSession, POSSettings, POSStaff)

D = Decimal
Z = D("0")
CENT = D("0.01")
PIN_MIN, PIN_MAX = 4, 8
PIN_TRIES = 5
PIN_LOCK = datetime.timedelta(minutes=5)

INBOUND = ("RECEIPT", "TRANSFER_IN", "ADJUSTMENT_PLUS", "RETURN_IN", "OPENING", "PRODUCTION_IN")
OUTBOUND = ("DELIVERY", "TRANSFER_OUT", "ADJUSTMENT_MINUS", "RETURN_OUT", "PRODUCTION_OUT")


def cents(value):
    return D(value or 0).quantize(CENT, rounding=ROUND_HALF_UP)


# ─────────────────────────────── settings & menus ───────────────────────────────

def settings_for(company):
    """Head office's POS settings, created with the defaults the first time."""
    found, _ = POSSettings.objects.get_or_create(company=company)
    return found


def station_menu(station):
    """The menu a station sells from: its own, else head office's default,
    provided it is offered at the station's branch."""
    menu = station.menu or settings_for(station.company).default_menu
    if menu is None or not menu.is_active:
        return None
    if menu.branches.exists() and not menu.branches.filter(pk=station.branch_id).exists():
        return None
    return menu


def menu_items(menu):
    """Sections with their available items, in menu order."""
    if menu is None:
        return []
    out = []
    for section in menu.sections.prefetch_related("items__product__tax_rate").all():
        items = [i for i in section.items.all() if i.is_available and i.product.is_active]
        if items:
            out.append((section, items))
    return out


# ─────────────────────────────────── staff PINs ─────────────────────────────────

def clean_pin(pin):
    pin = (pin or "").strip()
    if not pin.isdigit() or not PIN_MIN <= len(pin) <= PIN_MAX:
        raise ValidationError(f"A PIN is {PIN_MIN} to {PIN_MAX} digits.")
    if len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
        raise ValidationError("That PIN is too easy to guess (repeated or running digits).")
    return pin


def set_pin(staff, pin):
    staff.pin_hash = make_password(clean_pin(pin))
    staff.failed_pins = 0
    staff.locked_until = None
    staff.save(update_fields=["pin_hash", "failed_pins", "locked_until", "updated_at"])


def floor_staff(company, branch):
    """Who may work this branch's floor: active, with a PIN, at this branch or any."""
    return POSStaff.objects.filter(company=company, is_active=True).exclude(pin_hash="").filter(
        Q(branch__isnull=True) | Q(branch=branch)).order_by("name")


def verify_pin(staff, pin):
    """Check a PIN, counting failures: five wrong in a row lock the person out
    for five minutes, so a PIN cannot be guessed at a station left open."""
    now = timezone.now()
    with transaction.atomic():
        staff = POSStaff.objects.select_for_update().get(pk=staff.pk)
        if staff.locked_until and staff.locked_until > now:
            left = max(1, int((staff.locked_until - now).total_seconds() // 60) + 1)
            raise ValidationError(f"Too many wrong PINs. {staff.name} is locked for {left} more "
                                  "minute(s); ask a manager if it is urgent.")
        if not staff.is_active or not staff.pin_hash:
            raise ValidationError(f"{staff.name} has no working PIN. Ask a manager to set one.")
        if check_password((pin or "").strip(), staff.pin_hash):
            if staff.failed_pins or staff.locked_until:
                POSStaff.objects.filter(pk=staff.pk).update(failed_pins=0, locked_until=None)
            return staff
        failed = staff.failed_pins + 1
        locked = now + PIN_LOCK if failed >= PIN_TRIES else None
        POSStaff.objects.filter(pk=staff.pk).update(
            failed_pins=0 if locked else failed, locked_until=locked)
    if locked:
        raise ValidationError(f"Wrong PIN. That was {PIN_TRIES} in a row, so {staff.name} is "
                              "locked for five minutes.")
    raise ValidationError(f"Wrong PIN for {staff.name}. {PIN_TRIES - failed} tries left.")


def verify_manager(company, branch, staff_id, pin):
    """A manager's say-so for a void: the manager's own PIN, entered now."""
    staff = floor_staff(company, branch).filter(pk=staff_id).first() if staff_id else None
    if staff is None or not staff.is_manager:
        raise ValidationError("Choose a manager to authorise this.")
    return verify_pin(staff, pin)


# ─────────────────────────────────── pricing ────────────────────────────────────

def _category_chain(product):
    ids, cat, seen = set(), product.category, set()
    while cat is not None and cat.pk not in seen:
        seen.add(cat.pk)
        ids.add(cat.pk)
        cat = cat.parent
    return ids


def rule_runs(rule, branch_id, when):
    """Is this rule running at this branch at this moment?"""
    local = timezone.localtime(when)
    day, now_t = local.weekday(), local.time()
    if rule.valid_from and local.date() < rule.valid_from:
        return False
    if rule.valid_to and local.date() > rule.valid_to:
        return False
    start, end = rule.start_time, rule.end_time
    if start <= end:
        running, run_day = start <= now_t < end, day
    elif now_t >= start:            # overnight window, before midnight
        running, run_day = True, day
    elif now_t < end:               # overnight window, after midnight: yesterday's
        running, run_day = True, (day - 1) % 7
    else:
        running, run_day = False, day
    if not running or run_day not in rule.day_list:
        return False
    branch_ids = [b.pk for b in rule.branches.all()]
    return not branch_ids or branch_id in branch_ids


def rule_covers(rule, item):
    if rule.product_id and rule.product_id != item.product_id:
        return False
    if rule.section_id and rule.section_id != item.section_id:
        return False
    if rule.category_id and rule.category_id not in _category_chain(item.product):
        return False
    return True


def active_rules(company):
    """The company's switched-on price rules, loaded once for a whole page."""
    return list(POSPriceRule.objects.filter(company=company, is_active=True).prefetch_related(
        "branches"))


def price_for(item, branch, when=None, rules=None):
    """(unit price, discount %, rule) for one menu item ordered now. The best
    running rule wins; with none running the item sells at its menu price.
    Pass `rules` (active_rules) when pricing a whole menu."""
    when = when or timezone.now()
    best = None
    if rules is None:
        rules = active_rules(item.product.company)
    for rule in rules:
        if rule_runs(rule, branch.pk, when) and rule_covers(rule, item):
            if best is None or rule.discount_percent > best.discount_percent:
                best = rule
    pct = best.discount_percent if best else Z
    return D(str(item.unit_price or 0)), pct, best


def running_rules(company, branch, when=None):
    when = when or timezone.now()
    return [r for r in POSPriceRule.objects.filter(company=company, is_active=True).prefetch_related(
        "branches") if rule_runs(r, branch.pk, when)]


# ──────────────────────────────────── bills ─────────────────────────────────────

def live_orders(company, branch=None):
    qs = POSOrder.objects.filter(company=company, status__in=POSOrder.LIVE).select_related(
        "table", "waiter", "station")
    return qs.filter(branch=branch) if branch is not None else qs


def order_totals(order):
    """Net, VAT and total of the bill's live lines, worked out as the till will."""
    subtotal = discount = tax = Z
    count = Z
    for line in order.lines.filter(status="ACTIVE").select_related("product__tax_rate"):
        subtotal += line.net
        discount += line.discount
        count += line.quantity
        if line.product.tax_rate_id:
            tax += cents(line.net * line.product.tax_rate.rate / D("100"))
    subtotal, tax = cents(subtotal), cents(tax)
    return {"subtotal": subtotal, "discount": cents(discount), "tax": tax,
            "total": subtotal + tax, "items": count}


def _check_staff(order_or_station, staff, doing):
    if staff is None or not staff.is_active:
        raise ValidationError(f"Enter your PIN before you {doing}.")
    if staff.company_id != order_or_station.company_id:
        raise ValidationError("That person does not work in this company.")
    if staff.branch_id and staff.branch_id != order_or_station.branch_id:
        raise ValidationError(f"{staff.name} works at another branch.")


@transaction.atomic
def open_order(station, waiter, *, table=None, covers=1, notes=""):
    from apps.core.models import SequenceCounter

    if not station.is_active:
        raise ValidationError(f"{station.name} is switched off.")
    _check_staff(station, waiter, "open a bill")
    conf = settings_for(station.company)
    if table is None and conf.require_table and station.kind == "WAITER":
        raise ValidationError("Choose a table: head office asks for one on every bill.")
    if table is not None:
        if table.branch_id != station.branch_id or not table.is_active:
            raise ValidationError("That table is not at this branch.")
        if table.station_id and table.station_id != station.pk:
            raise ValidationError(f"{table.name} is served from another station.")
        busy = live_orders(station.company).filter(table=table).first()
        if busy:
            raise ValidationError(f"{table.name} already has bill {busy.reference} open. "
                                  "Add to that one, or close it first.")
    try:
        covers = int(covers or 1)
    except (TypeError, ValueError):
        raise ValidationError("Guests has to be a whole number.")
    if not 1 <= covers <= 200:
        raise ValidationError("Guests has to be between 1 and 200.")
    return POSOrder.objects.create(
        company=station.company, branch=station.branch, station=station, table=table,
        waiter=waiter, covers=covers, notes=(notes or "")[:500], opened_at=timezone.now(),
        reference=SequenceCounter.next_number(station.company, "BILL-"))


def _claim(order, doing):
    """Hold the bill for this change: a second device acting on it waits, then
    sees what the first one did."""
    if not POSOrder.objects.filter(pk=order.pk, status__in=POSOrder.LIVE).update(
            updated_at=timezone.now()):
        order.refresh_from_db(fields=["status"])
        raise ValidationError(f"Bill {order.reference} is {order.get_status_display().lower()}, "
                              f"so you cannot {doing}.")
    order.refresh_from_db()


@transaction.atomic
def add_item(order, item, quantity, staff, *, note=""):
    _check_staff(order, staff, "add to a bill")
    _claim(order, "add to it")
    if not isinstance(item, POSMenuItem) or not item.is_available or not item.product.is_active:
        raise ValidationError("That item is not available right now.")
    menu = station_menu(order.station)
    if menu is None or item.section.menu_id != menu.pk:
        raise ValidationError(f"{item.label} is not on this station's menu.")
    qty = services.number(quantity, "Quantity", limit=services.QUANTITY_LIMIT)
    if qty <= 0:
        raise ValidationError("Quantity has to be more than nothing.")
    price, pct, rule = price_for(item, order.branch)
    if not price:
        raise ValidationError(f"{item.label} has no price on file yet.")
    line = POSOrderLine.objects.create(
        order=order, menu_item=item, product=item.product, name=item.label, quantity=qty,
        unit_price=price, discount_percent=pct, price_rule=rule, note=(note or "")[:120],
        added_by=staff)
    if order.status == "BILLED":
        # Something more was ordered after the bill was printed: it is a new bill.
        POSOrder.objects.filter(pk=order.pk).update(status="OPEN", billed_at=None)
    return line


@transaction.atomic
def void_line(line, manager, requested_by, reason):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Give the reason for the void; it goes on the day-end report.")
    order = line.order
    _claim(order, "void from it")
    if manager is None or not manager.is_manager:
        raise ValidationError("Only a manager can void an item.")
    line.refresh_from_db()
    if line.status != "ACTIVE":
        raise ValidationError(f"{line.name} is already voided.")
    POSOrderLine.objects.filter(pk=line.pk, status="ACTIVE").update(
        status="VOID", voided_by=manager, void_requested_by=requested_by,
        void_reason=reason[:255], voided_at=timezone.now())
    if order.status == "BILLED":
        POSOrder.objects.filter(pk=order.pk).update(status="OPEN", billed_at=None)


@transaction.atomic
def void_order(order, manager, reason, requested_by=None):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Give the reason for voiding the bill.")
    _claim(order, "void it")
    if manager is None or not manager.is_manager:
        raise ValidationError("Only a manager can void a bill.")
    now = timezone.now()
    order.lines.filter(status="ACTIVE").update(
        status="VOID", voided_by=manager, void_requested_by=requested_by,
        void_reason=reason[:255], voided_at=now)
    POSOrder.objects.filter(pk=order.pk).update(
        status="VOID", voided_by=manager, void_reason=reason[:255], closed_at=now)


@transaction.atomic
def print_bill(order):
    _claim(order, "print it")
    if not order.lines.filter(status="ACTIVE").exists():
        raise ValidationError("There is nothing on the bill to print.")
    POSOrder.objects.filter(pk=order.pk).update(status="BILLED", billed_at=timezone.now())
    order.refresh_from_db()
    return order


@transaction.atomic
def close_order(order, user, *, payment_method="CASH", tendered="", token="", session=None):
    """Take the money: ring the bill through the cashier's open till, then
    mark it closed against the ticket. Only one close ever succeeds."""
    _claim(order, "close it")
    session = session or services.active_session(order.company, user)
    if session is None:
        raise ValidationError("Open your till first: a bill is paid into an open till.")
    if session.branch_id != order.branch_id:
        raise ValidationError(f"Bill {order.reference} belongs to {order.branch}; your till is at "
                              f"{session.branch}.")
    lines = list(order.lines.filter(status="ACTIVE").select_related("product__tax_rate"))
    if not lines:
        raise ValidationError("There is nothing on the bill to pay for. Void it instead.")
    salesperson = None
    if order.waiter.user_id and services.company_people(order.company).filter(
            pk=order.waiter.user_id).exists():
        salesperson = order.waiter.user
    sale = services.checkout_lines(
        order.company, user,
        [{"product": l.product, "quantity": l.quantity, "unit_price": l.unit_price,
          "discount_percent": l.discount_percent} for l in lines],
        session=session, payment_method=payment_method, tendered=tendered,
        warehouse=order.station.warehouse_id and str(order.station.warehouse_id) or None,
        notes=f"Bill {order.reference}" + (f", {order.table.name}" if order.table_id else ""),
        token=token, salesperson=salesperson,
        customer_reference=order.reference)
    POSOrder.objects.filter(pk=order.pk).update(
        status="CLOSED", sale=sale, closed_by=user, closed_at=timezone.now())
    order.refresh_from_db()
    return sale


# ─────────────────────────────────── day end ────────────────────────────────────

def day_bounds(day):
    """The local day as [start, end) aware datetimes."""
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.datetime.combine(day, datetime.time.min), tz)
    return start, start + datetime.timedelta(days=1)


def day_report(company, branch, day):
    """Everything the manager signs off at the end of a trading day at one branch."""
    from apps.inventory.models import StockMovement

    start, end = day_bounds(day)
    sessions = POSSession.objects.filter(company=company, branch=branch).filter(
        Q(opened_at__lt=end) & (Q(closed_at__isnull=True) | Q(closed_at__gte=start)))
    sales = POSSale.objects.filter(session__in=sessions, created_at__gte=start, created_at__lt=end)
    tickets = sales.filter(refund_of__isnull=True)
    refunds = sales.exclude(refund_of__isnull=True)

    def total(qs, field="total_amount"):
        return cents(qs.aggregate(v=Sum(field))["v"])

    by_method = []
    for value, label in POSSale.PAYMENT_METHODS:
        taken = total(tickets.filter(payment_method=value))
        back = total(refunds.filter(payment_method=value))
        if taken or back:
            by_method.append({"method": label, "taken": taken, "refunded": back,
                              "net": taken - back})

    lines = POSSaleLine.objects.filter(sale__in=tickets)
    rlines = POSSaleLine.objects.filter(sale__in=refunds)
    gross_sales = total(lines, "line_total")
    vat = total(lines, "tax_amount") - total(rlines, "tax_amount")
    discounts = total(lines, "discount_amount") - total(rlines, "discount_amount")
    revenue = (gross_sales - total(lines, "tax_amount")) - (
        total(rlines, "line_total") - total(rlines, "tax_amount"))

    refs = list(tickets.values_list("reference", flat=True))
    rrefs = list(refunds.values_list("reference", flat=True))
    out_moves = StockMovement.objects.filter(product__company=company, reference__in=refs,
                                             movement_type="DELIVERY")
    back_moves = StockMovement.objects.filter(product__company=company, reference__in=rrefs,
                                              movement_type="RETURN_IN")
    cogs = cents(sum((m.quantity * m.unit_cost for m in out_moves), Z)) - cents(
        sum((m.quantity * m.unit_cost for m in back_moves), Z))

    from .models import POSCashMovement
    moves = POSCashMovement.objects.filter(session__in=sessions, created_at__gte=start,
                                           created_at__lt=end)
    payouts = total(moves.filter(direction="OUT"), "amount")
    payins = total(moves.filter(direction="IN"), "amount")

    orders = POSOrder.objects.filter(company=company, branch=branch)
    closed = orders.filter(status="CLOSED", closed_at__gte=start, closed_at__lt=end).select_related(
        "waiter", "sale")
    waiters = {}
    for o in closed:
        row = waiters.setdefault(o.waiter_id, {"name": o.waiter.name, "code": o.waiter.code,
                                               "bills": 0, "covers": 0, "takings": Z})
        row["bills"] += 1
        row["covers"] += o.covers
        row["takings"] += cents(o.sale.total_amount if o.sale_id else 0)
    sections = {}
    for line in POSOrderLine.objects.filter(order__in=closed, status="ACTIVE").select_related(
            "menu_item__section"):
        name = line.menu_item.section.name if line.menu_item_id else "Other"
        row = sections.setdefault(name, {"section": name, "quantity": Z, "net": Z})
        row["quantity"] += line.quantity
        row["net"] += line.net
    happy = cents(sum((l.discount for l in POSOrderLine.objects.filter(
        order__in=closed, status="ACTIVE", price_rule__isnull=False)), Z))

    voided_lines = POSOrderLine.objects.filter(
        order__company=company, order__branch=branch, status="VOID",
        voided_at__gte=start, voided_at__lt=end).select_related(
        "order", "voided_by", "void_requested_by", "added_by")
    voids = [{"bill": l.order.reference, "item": l.name, "quantity": l.quantity, "value": l.net,
              "reason": l.void_reason, "manager": l.voided_by.name if l.voided_by_id else "",
              "asked_by": (l.void_requested_by or l.added_by).name, "at": timezone.localtime(
                  l.voided_at)} for l in voided_lines]

    tills = []
    for s in sessions.select_related("cashier"):
        tills.append({"pk": s.pk, "cashier": s.cashier.get_full_name() or s.cashier.username,
                      "status": s.get_status_display(), "opening": s.opening_cash,
                      "expected": s.expected_cash, "counted": s.closing_cash,
                      "variance": (s.closing_cash - s.expected_cash) if s.closing_cash is not None else None})

    # Stock moved at this branch's shelves today, product by product.
    from apps.organizations.models import Warehouse
    shelves = list(Warehouse.objects.filter(company=company, branch=branch).values_list("pk", flat=True))
    stock = {}
    for m in StockMovement.objects.filter(product__company=company, warehouse_id__in=shelves,
                                          movement_date=day).select_related("product"):
        row = stock.setdefault(m.product_id, {"product": m.product.name, "sku": m.product.sku,
                                              "in": Z, "sold": Z, "out": Z})
        if m.movement_type in INBOUND:
            row["in"] += m.quantity
        elif m.movement_type == "DELIVERY":
            row["sold"] += m.quantity
        elif m.movement_type in OUTBOUND:
            row["out"] += m.quantity
    for pk, row in stock.items():
        before = StockMovement.objects.filter(product_id=pk, warehouse_id__in=shelves,
                                              movement_date__lt=day)
        opening = Z
        for mt, qty in before.values_list("movement_type", "quantity"):
            opening += qty if mt in INBOUND else -qty
        row["opening"] = opening
        row["closing"] = opening + row["in"] - row["sold"] - row["out"]

    from .models import POSDrawerEvent
    drawer = [{"when": e.created_at, "reason": e.get_reason_display(), "code": e.reason,
               "note": e.note, "who": (e.user.get_full_name() or e.user.username) if e.user_id else "",
               "ticket": e.sale.reference if e.sale_id else "", "printer": e.printer.name if e.printer_id else "",
               "ok": e.ok}
              for e in POSDrawerEvent.objects.filter(company=company, created_at__gte=start, created_at__lt=end)
              .filter(Q(branch=branch) | Q(branch__isnull=True))
              .select_related("user", "sale", "printer").order_by("created_at")]

    live = list(live_orders(company, branch))
    open_tills = [t for t in tills if t["status"] == "Open"]
    done = POSDayEnd.objects.filter(company=company, branch=branch, business_date=day).first()
    blockers = []
    if live and settings_for(company).day_end_requires_closed_bills:
        blockers.append(f"{len(live)} bill(s) still open: " + ", ".join(o.reference for o in live[:6]))
    if open_tills:
        blockers.append(f"{len(open_tills)} till(s) still open: close and count them first.")
    if day > timezone.localdate():
        blockers.append("That day has not come yet.")
    return {
        "day": day, "branch": branch, "done": done, "blockers": blockers,
        "tickets": tickets.count(), "refund_count": refunds.count(),
        "by_method": by_method, "takings": sum((r["net"] for r in by_method), Z),
        "pnl": {"revenue": revenue, "vat": vat, "discounts": discounts, "happy_hour": happy,
                "cogs": cogs, "gross_profit": revenue - cogs,
                "margin": ((revenue - cogs) / revenue * 100).quantize(D("0.1")) if revenue else None,
                "payouts": payouts, "payins": payins},
        "waiters": sorted(waiters.values(), key=lambda r: -r["takings"]),
        "sections": sorted(sections.values(), key=lambda r: -r["net"]),
        "voids": voids, "void_value": cents(sum((v["value"] for v in voids), Z)),
        "voided_bills": orders.filter(status="VOID", closed_at__gte=start, closed_at__lt=end).count(),
        "tills": tills, "live_orders": live,
        "drawer": drawer, "no_sale": sum(1 for d in drawer if d["code"] == "NO_SALE"),
        "stock": sorted(stock.values(), key=lambda r: r["product"]),
    }


def _plain(value):
    if isinstance(value, (Decimal, uuid.UUID)):
        return str(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items() if k not in ("branch", "done", "live_orders")}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "pk"):
        return str(value.pk)
    return value


@transaction.atomic
def close_day(company, branch, day, user):
    from apps.organizations.models import Branch
    Branch.objects.filter(pk=branch.pk).update(updated_at=timezone.now())   # one close at a time
    report = day_report(company, branch, day)
    if report["done"]:
        raise ValidationError(f"{day:%d %b %Y} at {branch} was already closed by "
                              f"{report['done'].closed_by} at "
                              f"{timezone.localtime(report['done'].closed_at):%H:%M}.")
    if report["blockers"]:
        raise ValidationError("The day cannot be closed yet: " + " ".join(report["blockers"]))
    return POSDayEnd.objects.create(company=company, branch=branch, business_date=day,
                                    closed_by=user, closed_at=timezone.now(),
                                    summary=_plain(report))
