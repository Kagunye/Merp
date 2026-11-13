"""Waiter stations, bills and the cashier panel.

A station is a device signed in to the ERP by a till-role user. The people on
the floor identify themselves on it with a PIN: the acting staff member (the
"actor") is kept in the browser session for this one station, for the idle
minutes head office allows, and only while they still work this branch's floor.
Ordering, voiding and printing need an actor; taking money needs the device
user's own open till, exactly like the counter.
"""
import datetime
from itertools import groupby

from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_POST

from apps.accounting.currency import book_currency

from . import restaurant, services
from .models import POSMenuItem, POSOrder, POSSale, POSStation, POSTable

ACTOR_KEY = "pos_actor"
PAY_METHODS = [(v, l) for v, l in POSSale.PAYMENT_METHODS if v != "SPLIT"]


# ─────────────────────────────────── helpers ────────────────────────────────────

def _company(request):
    company = getattr(request, "active_company", None)
    if company is None:
        raise Http404("No active company.")
    return company


def _error(request, exc):
    text = exc.messages[0] if getattr(exc, "messages", None) else str(exc)
    messages.error(request, text)


def current_actor(request, station):
    """The staff member acting at this station now, or None.

    Valid only for the station it was entered on, for the PIN idle minutes
    since the last action, and only while the person still works this floor.
    """
    data = request.session.get(ACTOR_KEY)
    if not isinstance(data, dict) or data.get("station") != str(station.pk):
        return None
    at = parse_datetime(data.get("at") or "")
    idle = restaurant.settings_for(station.company).pin_idle_minutes or 2
    if at is None or timezone.now() - at > datetime.timedelta(minutes=idle):
        request.session.pop(ACTOR_KEY, None)
        return None
    staff = services.by_pk(restaurant.floor_staff(station.company, station.branch),
                           data.get("staff"))
    if staff is None:
        request.session.pop(ACTOR_KEY, None)
    return staff


def _set_actor(request, station, staff):
    request.session[ACTOR_KEY] = {"staff": str(staff.pk), "station": str(station.pk),
                                  "at": timezone.now().isoformat()}


def _touch(request):
    """A successful action keeps the PIN session alive."""
    data = request.session.get(ACTOR_KEY)
    if isinstance(data, dict):
        data["at"] = timezone.now().isoformat()
        request.session[ACTOR_KEY] = data


def _need_pin(request, station, order=None):
    messages.error(request, "Enter your PIN first.")
    url = reverse("pos:station_floor", args=[station.pk])
    if order is not None:
        url += f"?bill={order.pk}"
    return redirect(url)


def _station(request, pk):
    return get_object_or_404(POSStation.objects.select_related("branch", "menu", "company"),
                             pk=pk, company=_company(request), is_active=True)


def _order(request, pk):
    order = get_object_or_404(
        POSOrder.objects.select_related("station__branch", "station__company", "branch", "table",
                                        "waiter", "sale", "company"),
        pk=pk, company=_company(request))
    if order.station.branch_id != order.branch_id:
        raise Http404("That bill is not at this station's branch.")
    return order


def _minutes(since):
    return max(0, int((timezone.now() - since).total_seconds() // 60)) if since else 0


def _bill_rows(orders):
    rows = []
    for o in orders:
        totals = restaurant.order_totals(o)
        rows.append({"order": o, "totals": totals, "minutes": _minutes(o.opened_at)})
    return rows


def _till_here(request, branch):
    """The device user's open till, if it is at this branch."""
    session = services.active_session(_company(request), request.user)
    return session if session and session.branch_id == branch.pk else None


def _back(request, order):
    if request.POST.get("back") == "cashier":
        return redirect("pos:cashier_panel")
    return redirect("pos:bill_detail", pk=order.pk)


def _receipt(order):
    return redirect(reverse("pos:bill_print", args=[order.pk]) + "?autoprint=1")


# ─────────────────────────────────── stations ───────────────────────────────────

@login_required
@require_GET
def station_pick(request):
    company = _company(request)
    stations = POSStation.objects.filter(company=company, is_active=True).select_related(
        "branch", "menu").order_by("branch__name", "name")
    live = {}
    for o in restaurant.live_orders(company):
        live[o.station_id] = live.get(o.station_id, 0) + 1
    groups = []
    for branch, items in groupby(stations, key=lambda s: s.branch):
        items = list(items)
        for s in items:
            s.live_count = live.get(s.pk, 0)
        groups.append({"branch": branch, "stations": items})
    return render(request, "pos/station/pick.html", {
        "page_title": "Waiter stations", "groups": groups,
        "open_session": services.active_session(company, request.user),
    })


@login_required
@require_GET
def station_floor(request, pk):
    station = _station(request, pk)
    company = station.company
    actor = current_actor(request, station)
    conf = restaurant.settings_for(company)
    ctx = {
        "station": station, "actor": actor, "conf": conf,
        "menu": restaurant.station_menu(station),
        "rules": restaurant.running_rules(company, station.branch),
        "book": book_currency(company),
        "idle_minutes": conf.pin_idle_minutes,
    }
    if actor is None:
        ctx["staff"] = restaurant.floor_staff(company, station.branch)
        nxt = services.by_pk(POSOrder.objects.filter(company=company, station=station),
                             request.GET.get("bill"))
        ctx["next_bill"] = nxt
    else:
        tables = list(POSTable.objects.filter(
            company=company, branch=station.branch, is_active=True).filter(
            Q(station=station) | Q(station__isnull=True)))
        live = list(restaurant.live_orders(company, station.branch))
        by_table = {}
        for o in live:
            if o.table_id:
                by_table.setdefault(o.table_id, []).append(o)
        table_rows = []
        for t in tables:
            orders = sorted(by_table.get(t.pk, []), key=lambda o: o.opened_at)
            o = orders[0] if orders else None
            # A split table shows its first bill and how many more are open;
            # the total is what the whole table still owes.
            table_rows.append({
                "table": t, "order": o, "more": len(orders) - 1 if orders else 0,
                "totals": restaurant.order_totals(o) if o else None,
                "table_total": sum((restaurant.order_totals(x)["total"] for x in orders), Decimal("0")) if orders else None,
                "minutes": _minutes(o.opened_at) if o else 0,
            })
        ctx.update({
            "tables": table_rows,
            "free_tables": [r["table"] for r in table_rows if r["order"] is None],
            "bills": _bill_rows(o for o in live if o.station_id == station.pk),
        })
    return render(request, "pos/station/floor.html", ctx)


@login_required
@require_POST
def station_pin(request, pk):
    station = _station(request, pk)
    staff = services.by_pk(restaurant.floor_staff(station.company, station.branch),
                           request.POST.get("staff"))
    nxt = services.by_pk(POSOrder.objects.filter(company=station.company, station=station,
                                                 status__in=POSOrder.LIVE),
                         request.POST.get("bill"))
    back = reverse("pos:station_floor", args=[station.pk])
    if staff is None:
        messages.error(request, "Tap your name, then enter your PIN.")
        return redirect(back + (f"?bill={nxt.pk}" if nxt else ""))
    try:
        staff = restaurant.verify_pin(staff, request.POST.get("pin"))
    except ValidationError as exc:
        _error(request, exc)
        return redirect(back + (f"?bill={nxt.pk}" if nxt else ""))
    _set_actor(request, station, staff)
    messages.success(request, f"Signed in as {staff.name}.")
    if nxt is not None:
        return redirect("pos:bill_detail", pk=nxt.pk)
    return redirect(back)


@login_required
@require_POST
def station_leave(request, pk):
    station = _station(request, pk)
    data = request.session.get(ACTOR_KEY)
    if isinstance(data, dict) and data.get("station") == str(station.pk):
        request.session.pop(ACTOR_KEY, None)
    messages.info(request, "Signed out. The next person enters their PIN.")
    return redirect("pos:station_floor", pk=station.pk)


# ──────────────────────────────────── bills ─────────────────────────────────────

@login_required
@require_POST
def bill_open(request, pk):
    station = _station(request, pk)
    actor = current_actor(request, station)
    if actor is None:
        return _need_pin(request, station)
    table = None
    if (request.POST.get("table") or "").strip():
        table = services.by_pk(POSTable.objects.filter(
            company=station.company, branch=station.branch, is_active=True),
            request.POST.get("table"))
        if table is None:
            messages.error(request, "That table is not at this branch.")
            return redirect("pos:station_floor", pk=station.pk)
    try:
        order = restaurant.open_order(station, actor, table=table,
                                      covers=request.POST.get("covers") or 1,
                                      notes=request.POST.get("notes") or "")
    except ValidationError as exc:
        _error(request, exc)
        return redirect("pos:station_floor", pk=station.pk)
    _touch(request)
    messages.success(request, f"Bill {order.reference} opened"
                     + (f" for {table.name}." if table else "."))
    return redirect("pos:bill_detail", pk=order.pk)


def _bill_context(request, order, actor):
    company = order.company
    station = order.station
    conf = restaurant.settings_for(company)
    live = order.status in POSOrder.LIVE
    sections = []
    if live:
        page_rules = restaurant.active_rules(company)
        for section, items in restaurant.menu_items(restaurant.station_menu(station)):
            tiles = []
            for item in items:
                price, pct, rule = restaurant.price_for(item, order.branch, rules=page_rules)
                tiles.append({"item": item, "price": price, "pct": pct, "rule": rule,
                              "picture": services._picture(item.product)})
            sections.append({"section": section, "tiles": tiles})
    till = _till_here(request, order.branch)
    lines = list(order.lines.select_related("voided_by", "void_requested_by", "added_by",
                                            "price_rule").all())
    return {
        "order": order, "station": station, "actor": actor, "conf": conf, "live": live,
        "sections": sections, "lines": lines,
        "totals": restaurant.order_totals(order),
        "managers": [s for s in restaurant.floor_staff(company, order.branch) if s.is_manager],
        "till": till, "may_pay": live and (conf.waiter_can_take_payment or till is not None),
        "pay_token": services.new_token(), "methods": PAY_METHODS,
        "rules": restaurant.running_rules(company, order.branch),
        "book": book_currency(company),
        "active_section": request.GET.get("section") or "",
        "minutes": _minutes(order.opened_at),
    }


@login_required
def bill_detail(request, pk):
    """The bill on the station: menu on the left, the bill on the right. A POST
    with action=print prints it (bill_print itself is read-only)."""
    order = _order(request, pk)
    station = order.station
    actor = current_actor(request, station)
    if request.method == "POST":
        if request.POST.get("action") != "print":
            messages.error(request, "Nothing to do.")
            return _back(request, order)
        if actor is None and _till_here(request, order.branch) is None:
            return _need_pin(request, station, order)
        try:
            restaurant.print_bill(order)
        except ValidationError as exc:
            _error(request, exc)
            return _back(request, order)
        if actor is not None:
            _touch(request)
        return _receipt(order)
    if actor is None and order.status in POSOrder.LIVE:
        return _need_pin(request, station, order)
    return render(request, "pos/station/bill.html", _bill_context(request, order, actor))


@login_required
@require_POST
def bill_add(request, pk):
    order = _order(request, pk)
    actor = current_actor(request, order.station)
    if actor is None:
        return _need_pin(request, order.station, order)
    item = services.by_pk(POSMenuItem.objects.filter(
        section__menu__company=order.company).select_related("section", "product"),
        request.POST.get("item"))
    url = reverse("pos:bill_detail", args=[order.pk])
    section = (request.POST.get("section") or "").strip()[:40]
    if section:
        url += f"?section={section}"
    if item is None:
        messages.error(request, "That item is not on the menu.")
        return redirect(url)
    try:
        line = restaurant.add_item(order, item, request.POST.get("quantity") or "1", actor,
                                   note=request.POST.get("note") or "")
    except ValidationError as exc:
        _error(request, exc)
        return redirect(url)
    _touch(request)
    msg = f"Added {line.quantity.normalize():f} × {line.name}"
    if line.price_rule_id:
        msg += f" ({line.price_rule.name}, {line.discount_percent.normalize():f}% off)"
    messages.success(request, msg + ".")
    return redirect(url)


@login_required
@require_POST
def bill_void_line(request, pk):
    order = _order(request, pk)
    actor = current_actor(request, order.station)
    if actor is None:
        return _need_pin(request, order.station, order)
    line = services.by_pk(order.lines.all(), request.POST.get("line"))
    if line is None:
        messages.error(request, "Choose the item to void.")
        return redirect("pos:bill_detail", pk=order.pk)
    try:
        manager = restaurant.verify_manager(order.company, order.branch,
                                            request.POST.get("manager"),
                                            request.POST.get("manager_pin"))
        restaurant.void_line(line, manager, actor, request.POST.get("reason"))
    except ValidationError as exc:
        _error(request, exc)
        return redirect("pos:bill_detail", pk=order.pk)
    _touch(request)
    messages.success(request, f"{line.name} voided, authorised by {manager.name}.")
    return redirect("pos:bill_detail", pk=order.pk)


@login_required
@require_POST
def bill_split(request, pk):
    """Split a bill by item, or equally between guests; each part is paid on its own."""
    order = _order(request, pk)
    actor = current_actor(request, order.station)
    if actor is None:
        return _need_pin(request, order.station, order)
    try:
        if request.POST.get("mode") == "even":
            bills = restaurant.split_even(order, actor, request.POST.get("ways"))
            msg = f"Bill split {len(bills)} ways: " + ", ".join(b.reference for b in bills) + ". Each is paid on its own."
            target = order
        else:
            moves = {k[len("move_"):]: v for k, v in request.POST.items() if k.startswith("move_")}
            target = restaurant.split_items(order, actor, moves)
            msg = f"New bill {target.reference} made from {order.reference} for the same table."
    except ValidationError as exc:
        _error(request, exc)
        return redirect("pos:bill_detail", pk=order.pk)
    _touch(request)
    messages.success(request, msg)
    return redirect("pos:bill_detail", pk=target.pk)


@login_required
@require_POST
def bill_void(request, pk):
    order = _order(request, pk)
    actor = current_actor(request, order.station)
    if actor is None:
        return _need_pin(request, order.station, order)
    try:
        manager = restaurant.verify_manager(order.company, order.branch,
                                            request.POST.get("manager"),
                                            request.POST.get("manager_pin"))
        restaurant.void_order(order, manager, request.POST.get("reason"), requested_by=actor)
    except ValidationError as exc:
        _error(request, exc)
        return redirect("pos:bill_detail", pk=order.pk)
    _touch(request)
    messages.success(request, f"Bill {order.reference} voided, authorised by {manager.name}.")
    return redirect("pos:station_floor", pk=order.station_id)


@login_required
@require_GET
def bill_print(request, pk):
    """The bill (or, once paid, the receipt) on 80 mm thermal paper."""
    order = _order(request, pk)
    company = order.company
    conf = restaurant.settings_for(company)
    lines = list(order.lines.filter(status="ACTIVE").select_related("price_rule"))
    return render(request, "pos/station/print.html", {
        "order": order, "company": company, "conf": conf, "lines": lines,
        "totals": restaurant.order_totals(order), "sale": order.sale if order.sale_id else None,
        "book": book_currency(company), "autoprint": request.GET.get("autoprint") == "1",
        "now": timezone.now(),
    })


@login_required
@require_POST
def bill_pay(request, pk):
    order = _order(request, pk)
    token = services.clean_token(request.POST.get("token"))
    if order.status == "CLOSED" and order.sale_id and token and \
            order.sale.submission_token == token:
        messages.info(request, f"Bill {order.reference} was already paid from this form "
                               f"({order.sale.reference}); it was not taken twice.")
        return _receipt(order)
    if not token:
        messages.error(request, "That payment form had no one-time token, so it could not be "
                                "protected against being paid twice. Nothing was taken; try again.")
        return _back(request, order)
    method = (request.POST.get("payment_method") or "CASH").strip().upper()
    tendered = request.POST.get("tendered") if method == "CASH" else ""
    try:
        sale = restaurant.close_order(order, request.user, payment_method=method,
                                      tendered=tendered or "", token=token)
    except IntegrityError:
        messages.error(request, "That payment form was already used. Nothing more was taken; "
                                "open the bill again.")
        return _back(request, order)
    except ValidationError as exc:
        _error(request, exc)
        return _back(request, order)
    if current_actor(request, order.station) is not None:
        _touch(request)
    messages.success(request, f"Bill {order.reference} paid: {sale.reference}, "
                              f"{sale.total_amount} taken, {sale.change_given} change.")
    return _receipt(order)


# ─────────────────────────────────── cashier ────────────────────────────────────

@login_required
@require_GET
def cashier_panel(request):
    company = _company(request)
    session = services.active_session(company, request.user)
    ctx = {"page_title": "Cashier panel", "session": session, "book": book_currency(company),
           "methods": PAY_METHODS}
    if session is not None:
        live = _bill_rows(restaurant.live_orders(company, session.branch).order_by("opened_at"))
        for row in live:
            row["token"] = services.new_token()
            row["items"] = row["totals"]["items"]
        start, end = restaurant.day_bounds(timezone.localdate())
        closed = POSOrder.objects.filter(
            company=company, branch=session.branch, status="CLOSED", closed_at__gte=start,
            closed_at__lt=end).select_related(
            "table", "waiter", "sale").order_by("-closed_at")
        ctx.update({"live": live, "closed": closed})
    return render(request, "pos/station/cashier.html", ctx)
