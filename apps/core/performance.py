"""The "My Performance" workspace on the dashboard.

Everything here follows one period, chosen with the period control at the top
of the page (GET `period`, `from`, `to`, `quarter`), and every figure is worked
out only for a role that may open the page behind it: the caller passes `may`
from dashboard_views.page_rights, and a section the role may not see is never
queried at all.

What "my" means, per document (none of these models carries a `created_by`):

* Sales invoices, orders and quotes: the sales rep on the document (for an
  invoice, the rep on the order it was raised from); where the document names
  no rep, the account manager on the customer (`Customer.assigned_to`).
* Purchase requisitions: `requested_by`.
* Purchase orders: raised from a requisition I requested
  (`requisition.requested_by`).
* Supplier bills: billed against such a purchase order
  (`purchase_order.requisition.requested_by`).

Amounts are in the book's currency: the base value stamped on the document,
or, for a document that predates the stamp, its total at its own rate. All of
it is summed in the database.
"""
from __future__ import annotations

import calendar as _calendar
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.db.models import (
    Case, Count, DecimalField, Exists, F, OuterRef, Q, Sum, Value, When,
)
from django.db.models.functions import Coalesce, TruncMonth
from django.urls import reverse
from django.utils import timezone

ZERO = Decimal("0")
ONE = Decimal("1")
MONEY = DecimalField(max_digits=20, decimal_places=2)
RATE = DecimalField(max_digits=30, decimal_places=8)

# The longest custom range the page draws (the efficiency chart has one bar
# pair per month).
MAX_MONTHS = 60
EARLIEST = date(2000, 1, 1)


# ─────────────────────────────── the period ──────────────────────────────────

def company_tz(company):
    try:
        return ZoneInfo(getattr(company, "timezone", "") or "Africa/Nairobi")
    except Exception:  # an unknown zone name must not break the page
        return ZoneInfo("Africa/Nairobi")


def company_today(company):
    """Today's date where the company is, not where the server is."""
    return timezone.now().astimezone(company_tz(company)).date()


def month_start(d):
    return d.replace(day=1)


def month_end(d):
    return d.replace(day=_calendar.monthrange(d.year, d.month)[1])


def add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    return date(y, m % 12 + 1, 1)


def quarter_of(d):
    return (d.month - 1) // 3 + 1


def quarter_range(year, q):
    start = date(year, 3 * (q - 1) + 1, 1)
    return start, month_end(add_months(start, 2))


PRESETS = [
    ("month", "This month"),
    ("quarter", "This quarter"),
    ("ytd", "Year to date"),
    ("last_year", "Last year"),
]
DEFAULT_PRESET = "ytd"


@dataclass
class Period:
    key: str            # month | quarter | ytd | last_year | q | custom
    start: date
    end: date
    label: str
    today: date
    quarter: str = ""   # "2026-Q3" when a quarter chip is chosen
    params: dict = field(default_factory=dict)

    @property
    def querystring(self):
        return urlencode(self.params)

    def months(self):
        out, m = [], month_start(self.start)
        while m <= self.end and len(out) < MAX_MONTHS:
            out.append(m)
            m = add_months(m, 1)
        return out


def _parse_date(value):
    try:
        d = date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError):
        return None
    return d if EARLIEST <= d <= date(2100, 12, 31) else None


def parse_period(params, today):
    """The period the page shows, from its GET parameters. Junk falls back to
    the default preset rather than failing the page."""
    key = (params.get("period") or "").strip()
    quarter = (params.get("quarter") or "").strip().upper()

    if quarter:
        try:
            year_s, q_s = quarter.replace(" ", "").split("-Q")
            year, q = int(year_s), int(q_s)
            if not (EARLIEST.year <= year <= 2100 and 1 <= q <= 4):
                raise ValueError
        except ValueError:
            quarter = ""
        else:
            start, end = quarter_range(year, q)
            return Period("q", start, end, f"Q{q} {year}", today, f"{year}-Q{q}",
                          {"quarter": f"{year}-Q{q}"})

    if key == "custom":
        start, end = _parse_date(params.get("from")), _parse_date(params.get("to"))
        if start or end:
            start = start or month_start(end)
            end = end or today
            if start > end:
                start, end = end, start
            # One bar pair per month: a range of decades would draw hundreds.
            earliest = add_months(month_start(end), -(MAX_MONTHS - 1))
            if start < earliest:
                start = earliest
            label = f"{start:%d %b %Y} – {end:%d %b %Y}"
            return Period("custom", start, end, label, today, "",
                          {"period": "custom", "from": start.isoformat(),
                           "to": end.isoformat()})
        key = ""

    if key not in dict(PRESETS):
        key = DEFAULT_PRESET
    if key == "month":
        start, end, label = month_start(today), month_end(today), f"{today:%B %Y}"
    elif key == "quarter":
        q = quarter_of(today)
        start, end = quarter_range(today.year, q)
        label = f"Q{q} {today.year}"
    elif key == "last_year":
        start, end = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
        label = str(today.year - 1)
    else:
        start, end = date(today.year, 1, 1), today
        label = f"Year to date {today.year}"
    params = {} if key == DEFAULT_PRESET else {"period": key}
    return Period(key, start, end, label, today, "", params)


def period_controls(period):
    """Preset links, and quarter chips grouped under this year and last."""
    presets = []
    for key, label in PRESETS:
        qs = "" if key == DEFAULT_PRESET else urlencode({"period": key})
        presets.append({"key": key, "label": label, "qs": qs,
                        "active": period.key == key})
    years = []
    for year in (period.today.year - 1, period.today.year):
        chips = []
        for q in range(1, 5):
            start, end = quarter_range(year, q)
            code = f"{year}-Q{q}"
            chips.append({
                "label": f"Q{q}", "code": code,
                "qs": urlencode({"quarter": code}),
                "future": start > period.today,
                "active": period.key == "q" and period.quarter == code,
                "current": start <= period.today <= end,
            })
        years.append({"year": year, "chips": chips})
    return {"presets": presets, "years": years}


def day_bounds(company, start, end):
    """Aware datetimes covering the company's calendar days start..end."""
    tz = company_tz(company)
    return (datetime.combine(start, time.min, tzinfo=tz),
            datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz))


# ─────────────────────────── money expressions ───────────────────────────────

def book_value(prefix=""):
    """The book-currency value of a document's total, as a database expression:
    the stamped base value, or the total at its own rate when none was stamped."""
    p = prefix
    return Case(
        When(~Q(**{f"{p}total_amount_base": 0}), then=F(f"{p}total_amount_base")),
        default=F(f"{p}total_amount") * Coalesce(F(f"{p}exchange_rate"), Value(ONE, output_field=RATE)),
        output_field=MONEY,
    )


def paid_book_value():
    """The book-currency value of what has been paid on an invoice: its paid
    share of the book value."""
    return Case(
        When(total_amount=0, then=Value(ZERO, output_field=MONEY)),
        default=F("paid_amount") * book_value() / F("total_amount"),
        output_field=MONEY,
    )


def cents(value):
    try:
        return Decimal(value or 0).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        return ZERO


# ─────────────────────────────── "my" rules ──────────────────────────────────

def mine_by_rep(user, rep="sales_rep", customer="customer"):
    """The rep on the document; failing one, the customer's account manager."""
    return Q(**{rep: user}) | Q(**{f"{rep}__isnull": True, f"{customer}__assigned_to": user})


def _invoices(company):
    from apps.sales.models import Invoice
    return Invoice.objects.filter(company=company).exclude(
        status__in=["DRAFT", "CANCELLED", "CREDIT_NOTE"])


def _bills(company):
    from apps.procurement.models import Bill
    return Bill.objects.filter(company=company).exclude(
        status__in=["DRAFT", "CANCELLED", "DEBIT_NOTE"])


# Each tile: what it counts, whose it is, the list it opens, and whether its
# amounts are shown (`money`: the page whose readers may see the money).
TILE_SPECS = [
    {
        "key": "sales_invoices", "title": "My Sales Invoices", "row": 1,
        "list": "sales:invoice_list", "money": "sales:ar_aging",
        "icon": "receipt", "bar": "bg-success", "date": "invoice_date",
        "mine_note": "Invoices where you are the sales rep on the order, or the "
                     "account manager of the customer. Issued invoices only: "
                     "drafts, cancelled invoices and credit notes are left out.",
    },
    {
        "key": "sales_orders", "title": "My Sales Orders", "row": 1,
        "list": "sales:order_list", "money": "sales:ar_aging",
        "icon": "clipboard", "bar": "bg-info", "date": "order_date",
        "mine_note": "Orders where you are the sales rep, or the account manager "
                     "of the customer when no rep is named. Cancelled orders are "
                     "left out.",
    },
    {
        "key": "sales_quotes", "title": "My Sales Quotes", "row": 1,
        "list": "sales:quote_list", "money": "sales:ar_aging",
        "icon": "document", "bar": "bg-warning", "date": "quote_date",
        "mine_note": "Quotes where you are the sales rep, or the account manager "
                     "of the customer when no rep is named. Cancelled quotes are "
                     "left out.",
    },
    {
        "key": "purchase_invoices", "title": "My Purchase Invoices", "row": 2,
        "list": "procurement:bill_list", "money": "procurement:ap_aging",
        "icon": "receipt", "bar": "bg-teal-600 dark:bg-teal-400", "date": "bill_date",
        "mine_note": "Bills have no owner of their own: yours are the bills "
                     "against purchase orders raised from requisitions you "
                     "requested. Drafts, cancelled bills and debit notes are left out.",
    },
    {
        "key": "purchase_orders", "title": "My Purchase Orders", "row": 2,
        "list": "procurement:po_list", "money": "procurement:ap_aging",
        "icon": "cart", "bar": "bg-indigo-600 dark:bg-indigo-400", "date": "order_date",
        "mine_note": "Purchase orders have no owner of their own: yours are the "
                     "orders raised from requisitions you requested. Cancelled "
                     "orders are left out.",
    },
    {
        "key": "requisitions", "title": "My Purchase Requisitions", "row": 2,
        "list": "procurement:requisition_list", "money": "procurement:ap_aging",
        "icon": "clipboard", "bar": "bg-danger", "date": "created_at",
        "mine_note": "Requisitions you requested, by the day they were raised. "
                     "There are no purchase quotes in Mofisi, so this tile counts "
                     "requisitions. Cancelled ones are left out.",
    },
]


def tile_querysets(company, user, period):
    """{key: (queryset in the period, Q for mine, value expression)}."""
    from apps.procurement.models import PurchaseOrder, PurchaseRequisition
    from apps.sales.models import SalesOrder, SalesQuote

    s, e = period.start, period.end
    lo, hi = day_bounds(company, s, e)
    return {
        "sales_invoices": (
            _invoices(company).filter(invoice_date__range=(s, e)),
            mine_by_rep(user, "sales_order__sales_rep"), book_value()),
        "sales_orders": (
            SalesOrder.objects.filter(company=company, order_date__range=(s, e))
            .exclude(status="CANCELLED"),
            mine_by_rep(user), book_value()),
        "sales_quotes": (
            SalesQuote.objects.filter(company=company, quote_date__range=(s, e))
            .exclude(status="CANCELLED"),
            mine_by_rep(user), book_value()),
        "purchase_invoices": (
            _bills(company).filter(bill_date__range=(s, e)),
            Q(purchase_order__requisition__requested_by=user), book_value()),
        "purchase_orders": (
            PurchaseOrder.objects.filter(company=company, order_date__range=(s, e))
            .exclude(status="CANCELLED"),
            Q(requisition__requested_by=user), book_value()),
        "requisitions": (
            PurchaseRequisition.objects.filter(
                company=company, created_at__gte=lo, created_at__lt=hi)
            .exclude(status="CANCELLED"),
            Q(requested_by=user), F("total_amount")),
    }


def document_tiles(company, user, period, may):
    tiles = []
    specs = [t for t in TILE_SPECS if may(t["list"])]
    if not specs:
        return tiles
    querysets = tile_querysets(company, user, period)
    for spec in specs:
        qs, mine, value = querysets[spec["key"]]
        show_money = may(spec["money"])
        agg = {"total_n": Count("pk"), "my_n": Count("pk", filter=mine)}
        if show_money:
            agg.update(total_amt=Sum(value, output_field=MONEY),
                       my_amt=Sum(value, filter=mine, output_field=MONEY))
        row = qs.aggregate(**agg)
        tile = dict(spec)
        tile.update({
            "url": reverse(spec["list"]),
            "money_shown": show_money,
            "my_count": row["my_n"] or 0,
            "total_count": row["total_n"] or 0,
            "my_amount": cents(row.get("my_amt")) if show_money else None,
            "total_amount": cents(row.get("total_amt")) if show_money else None,
        })
        if show_money and tile["total_amount"]:
            share = tile["my_amount"] / tile["total_amount"] * 100
        elif tile["total_count"]:
            share = Decimal(tile["my_count"]) / tile["total_count"] * 100
        else:
            share = ZERO
        tile["share"] = max(ZERO, min(Decimal(100), share)).quantize(Decimal("0.1"))
        tile["share_css"] = f"{tile['share']:.1f}"
        tiles.append(tile)
    return tiles


# ─────────────────────────── gauge: collection rate ──────────────────────────

GAUGE_BANDS = [
    (20, "Poor", "stroke-danger"),
    (40, "Fair", "stroke-warning"),
    (60, "Good", "stroke-ink-faint"),
    (80, "Very good", "stroke-info"),
    (100, "Excellent", "stroke-success"),
]
GAUGE_C, GAUGE_R = (110, 110), 84


def _gauge_point(v, r=GAUGE_R):
    theta = math.pi * (1 - v / 100)
    cx, cy = GAUGE_C
    return round(cx + r * math.cos(theta), 2), round(cy - r * math.sin(theta), 2)


def gauge_geometry(rate):
    """SVG pieces for a semicircle from Poor (0%) to Excellent (100%)."""
    segments, lo = [], 0
    for hi, label, cls in GAUGE_BANDS:
        x1, y1 = _gauge_point(lo + 0.6 if lo else lo)
        x2, y2 = _gauge_point(hi - 0.6 if hi < 100 else hi)
        segments.append({"d": f"M {x1} {y1} A {GAUGE_R} {GAUGE_R} 0 0 1 {x2} {y2}",
                         "cls": cls, "label": label})
        lo = hi
    ticks = []
    for v in (0, 20, 40, 60, 80, 100):
        x, y = _gauge_point(v, GAUGE_R + 16)
        ticks.append({"v": v, "x": x, "y": y + 4})
    value = None if rate is None else float(max(0, min(100, rate)))
    angle = None if value is None else round(-90 + value * 1.8, 2)
    band = None
    if value is not None:
        band = next(label for hi, label, _ in GAUGE_BANDS if value <= hi)
    return {"segments": segments, "ticks": ticks, "angle": angle, "band": band,
            "cx": GAUGE_C[0], "cy": GAUGE_C[1], "needle": GAUGE_R - 14,
            "poor": _gauge_point(0, GAUGE_R), "excellent": _gauge_point(100, GAUGE_R)}


def collection_gauge(company, user, period):
    """My collection rate: of the invoices I own dated in the period, the share
    of their book value that has been paid (credit notes are not collections)."""
    qs = _invoices(company).filter(invoice_date__range=(period.start, period.end)).filter(
        mine_by_rep(user, "sales_order__sales_rep"))
    row = qs.aggregate(n=Count("pk"), invoiced=Sum(book_value(), output_field=MONEY),
                       paid=Sum(paid_book_value(), output_field=MONEY))
    invoiced, paid = cents(row["invoiced"]), cents(row["paid"])
    rate = (paid / invoiced * 100).quantize(Decimal("0.1")) if invoiced > 0 else None

    from apps.sales.models import SalesQuote
    quotes = SalesQuote.objects.filter(
        company=company, quote_date__range=(period.start, period.end)).filter(mine_by_rep(user))
    q = quotes.aggregate(
        won=Count("pk", filter=Q(status__in=["ACCEPTED", "CONVERTED"])),
        decided=Count("pk", filter=Q(status__in=["ACCEPTED", "CONVERTED", "EXPIRED", "CANCELLED"])))
    conversion = (Decimal(q["won"]) / q["decided"] * 100).quantize(Decimal("0.1")) \
        if q["decided"] else None
    return {"count": row["n"] or 0, "invoiced": invoiced, "paid": paid, "rate": rate,
            "conversion": conversion, "geometry": gauge_geometry(rate)}


# ─────────────────────────── efficiency by month ─────────────────────────────

def _monthly(qs, date_field, value):
    rows = (qs.annotate(m=TruncMonth(date_field)).values("m")
            .annotate(v=Sum(value, output_field=MONEY), n=Count("pk")).order_by("m"))
    out = {}
    for r in rows:
        m = r["m"]
        if isinstance(m, datetime):
            m = m.date()
        if m is None:
            continue
        key = m.replace(day=1)
        v, n = out.get(key, (ZERO, 0))
        out[key] = (v + cents(r["v"]), n + r["n"])
    return out


def efficiency(company, user, period, may):
    """Sales against lost sales per month: sales are issued invoices; lost are
    quotes that expired or were cancelled, plus cancelled orders (each on its
    own date). Mine and the whole company's."""
    from apps.sales.models import SalesOrder, SalesQuote
    s, e = period.start, period.end
    invoices = _invoices(company).filter(invoice_date__range=(s, e))
    lost_quotes = SalesQuote.objects.filter(
        company=company, quote_date__range=(s, e), status__in=["EXPIRED", "CANCELLED"])
    lost_orders = SalesOrder.objects.filter(
        company=company, order_date__range=(s, e), status="CANCELLED")
    quotes = SalesQuote.objects.filter(company=company, quote_date__range=(s, e)).exclude(
        status="CANCELLED")
    orders = SalesOrder.objects.filter(company=company, order_date__range=(s, e)).exclude(
        status="CANCELLED")
    mine_inv = mine_by_rep(user, "sales_order__sales_rep")
    mine_doc = mine_by_rep(user)

    months = period.months()
    series = {}
    for scope, inv_q, doc_q in (("mine", mine_inv, mine_doc), ("company", Q(), Q())):
        sold = _monthly(invoices.filter(inv_q), "invoice_date", book_value())
        lq = _monthly(lost_quotes.filter(doc_q), "quote_date", book_value())
        lo = _monthly(lost_orders.filter(doc_q), "order_date", book_value())
        series[scope] = {
            "sales": [float(sold.get(m, (ZERO, 0))[0]) for m in months],
            "lost": [float(lq.get(m, (ZERO, 0))[0] + lo.get(m, (ZERO, 0))[0]) for m in months],
        }
        if scope == "mine":
            q_m = _monthly(quotes.filter(doc_q), "quote_date", book_value())
            o_m = _monthly(orders.filter(doc_q), "order_date", book_value())
            stats = []
            for m in months:
                stats.append({
                    "month": m,
                    "quotes": q_m.get(m, (ZERO, 0)),
                    "orders": o_m.get(m, (ZERO, 0)),
                    "invoiced": sold.get(m, (ZERO, 0)),
                    "lost": (lq.get(m, (ZERO, 0))[0] + lo.get(m, (ZERO, 0))[0]),
                })
    for scope in series.values():
        scope["empty"] = not any(scope["sales"]) and not any(scope["lost"])
    totals = {
        "quotes": sum((r["quotes"][0] for r in stats), ZERO),
        "orders": sum((r["orders"][0] for r in stats), ZERO),
        "invoiced": sum((r["invoiced"][0] for r in stats), ZERO),
        "lost": sum((r["lost"] for r in stats), ZERO),
    }
    return {
        "labels": [m.strftime("%b %Y") for m in months],
        "mine": series["mine"], "company": series["company"],
        "my_stats": stats, "my_totals": totals,
    }


# ─────────────────────────────── task list ───────────────────────────────────

def task_counters(company, user, today):
    from apps.projects.models import Task
    open_tasks = Task.objects.filter(project__company=company, assigned_to=user).exclude(
        status__in=["DONE", "CANCELLED"])
    not_due = Q(due_date__isnull=True) | Q(due_date__gt=today)
    row = open_tasks.aggregate(
        overdue=Count("pk", filter=Q(due_date__lt=today)),
        today=Count("pk", filter=Q(due_date=today)),
        current=Count("pk", filter=not_due & ~Q(status="IN_PROGRESS")),
        in_progress=Count("pk", filter=not_due & Q(status="IN_PROGRESS")),
    )
    url = reverse("projects:task_list")
    return [
        {"key": "overdue", "label": "Overdue", "count": row["overdue"], "url": url, "tone": "danger"},
        {"key": "today", "label": "Due today", "count": row["today"], "url": url, "tone": "warning"},
        {"key": "current", "label": "Current", "count": row["current"], "url": url, "tone": "info"},
        {"key": "in_progress", "label": "In progress", "count": row["in_progress"], "url": url, "tone": "success"},
    ]


# ─────────────────────────────── approvals ───────────────────────────────────

def approval_counters(company, user, may, today):
    """Everything waiting on a decision this login's role may take. Nobody's
    own request is counted: each view refuses the person it is for, and the
    person who raised or submitted it, so those are not waiting on them."""
    out = []

    if may("procurement:requisition_approve", "POST") and may("procurement:requisition_list"):
        from apps.procurement.models import PurchaseRequisition
        n = PurchaseRequisition.objects.filter(
            company=company, status="PENDING_APPROVAL",
        ).exclude(requested_by=user).exclude(submitted_by=user).count()
        out.append({"key": "requisitions", "label": "Purchase requisitions", "count": n,
                    "icon": "cart",
                    "url": reverse("procurement:requisition_list") + "?status=PENDING_APPROVAL"})

    if may("hr:leave_approve", "POST") and may("hr:leave_list"):
        from apps.hr.models import LeaveRequest
        n = LeaveRequest.objects.filter(
            employee__company=company, status="PENDING",
        ).exclude(employee__user=user).exclude(created_by=user).count()
        out.append({"key": "leave", "label": "Leave requests", "count": n, "icon": "calendar",
                    "url": reverse("hr:leave_list") + "?status=PENDING"})

    if may("expenses:claim_approve", "POST") and may("expenses:claim_list"):
        from apps.expenses.models import ExpenseClaim
        n = ExpenseClaim.objects.filter(
            company=company, status="SUBMITTED",
        ).exclude(employee__user=user).exclude(created_by=user).count()
        out.append({"key": "expense_claims", "label": "Expense claims", "count": n,
                    "icon": "receipt", "url": reverse("expenses:claim_list") + "?status=SUBMITTED"})

    if may("timesheets:entry_approve", "POST") and may("timesheets:entry_list"):
        from apps.timesheets.models import TimesheetEntry
        n = TimesheetEntry.objects.filter(
            company=company, status="SUBMITTED", entry_date__lte=today,
        ).exclude(user=user).count()
        out.append({"key": "timesheets", "label": "Timesheets", "count": n, "icon": "clock",
                    "url": reverse("timesheets:entry_list") + "?status=SUBMITTED"})

    if may("payroll:run_list"):
        from apps.payroll.models import PayrollRun, Payslip
        runs = PayrollRun.objects.filter(company=company)
        if may("payroll:run_approve", "POST"):
            # A run is ready for approval once it has payslips: approving an
            # empty run is refused by the view.
            has_slips = Exists(Payslip.objects.filter(payroll_run=OuterRef("pk")))
            n = runs.filter(status__in=["DRAFT", "REVIEW"]).filter(has_slips).count()
            out.append({"key": "payroll_approve", "label": "Payroll runs to approve",
                        "count": n, "icon": "cash",
                        "url": reverse("payroll:run_list") + "?status=REVIEW"})
        if may("payroll:run_post", "POST"):
            # Whoever approved a run may not also post it.
            n = runs.filter(status="APPROVED", journal_entry__isnull=True).exclude(
                approved_by=user).count()
            out.append({"key": "payroll_post", "label": "Payroll runs to post",
                        "count": n, "icon": "bank",
                        "url": reverse("payroll:run_list") + "?status=APPROVED"})
    return out


# ─────────────────────────────── calendar ────────────────────────────────────

CALENDAR_KINDS = {
    # (label, dot colour class)
    "lead": ("Lead closes", "bg-info"),
    "interview": ("Interview", "bg-warning"),
    "task": ("Task due", "bg-brand"),
    "invoice": ("Invoice due", "bg-success"),
}
CALENDAR_LIMIT = 300  # per kind, per month


def parse_month(value, today):
    try:
        y, m = str(value).split("-")[:2]
        d = date(int(y), int(m), 1)
        if EARLIEST <= d <= date(2100, 12, 1):
            return d
    except (TypeError, ValueError):
        pass
    return month_start(today)


def calendar_events(company, user, month, may, money=False):
    """The month's dated items, each only for a role that may open its list:
    expected close dates of open leads, scheduled interviews, my open tasks'
    due dates and open invoices' due dates."""
    start, end = month_start(month), month_end(month)
    events = []

    def link(name, pk):
        return reverse(name, args=[pk]) if may(name) else ""

    if may("crm:lead_list"):
        from apps.crm.models import Lead
        for lead in (Lead.objects.filter(company=company, is_active=True,
                                         expected_close_date__range=(start, end))
                     .exclude(status__in=["WON", "LOST"])
                     .order_by("expected_close_date")[:CALENDAR_LIMIT]):
            events.append({"date": lead.expected_close_date.isoformat(), "kind": "lead",
                           "title": lead.name, "meta": lead.get_status_display(),
                           "mine": lead.assigned_to_id == user.pk,
                           "url": link("crm:lead_detail", lead.pk)})
    if may("recruitment:interview_list"):
        from apps.recruitment.models import Interview
        lo, hi = day_bounds(company, start, end)
        tz = company_tz(company)
        for iv in (Interview.objects.filter(candidate__opening__company=company,
                                            status="SCHEDULED",
                                            scheduled_at__gte=lo, scheduled_at__lt=hi)
                   .select_related("candidate").order_by("scheduled_at")[:CALENDAR_LIMIT]):
            local = iv.scheduled_at.astimezone(tz)
            events.append({"date": local.date().isoformat(), "kind": "interview",
                           "title": str(iv.candidate), "meta": local.strftime("%H:%M"),
                           "mine": iv.interviewer_id == user.pk,
                           "url": link("recruitment:candidate_detail", iv.candidate_id)})
    if may("projects:task_list"):
        from apps.projects.models import Task
        for t in (Task.objects.filter(project__company=company, assigned_to=user,
                                      due_date__range=(start, end))
                  .exclude(status__in=["DONE", "CANCELLED"])
                  .select_related("project").order_by("due_date")[:CALENDAR_LIMIT]):
            events.append({"date": t.due_date.isoformat(), "kind": "task", "title": t.title,
                           "meta": t.project.name, "mine": True,
                           "url": link("projects:task_detail", t.pk)})
    if may("sales:invoice_list"):
        for inv in (_invoices(company).filter(status__in=["SENT", "PARTIAL", "OVERDUE"],
                                              due_date__range=(start, end))
                    .select_related("customer").order_by("due_date")[:CALENDAR_LIMIT]):
            meta = inv.customer.name
            if money:
                meta += f" · {inv.currency} {inv.outstanding_amount:,.2f}"
            events.append({"date": inv.due_date.isoformat(), "kind": "invoice",
                           "title": inv.reference, "meta": meta, "mine": False,
                           "url": link("sales:invoice_detail", inv.pk)})
    for ev in events:
        ev["label"], ev["tone"] = CALENDAR_KINDS[ev["kind"]]
    events.sort(key=lambda ev: (ev["date"], ev["kind"]))
    return {"month": start.strftime("%Y-%m"), "label": start.strftime("%B %Y"),
            "events": events}


def calendar_kinds(may):
    """The kinds of item this role's calendar can show (for the legend)."""
    gates = {"lead": "crm:lead_list", "interview": "recruitment:interview_list",
             "task": "projects:task_list", "invoice": "sales:invoice_list"}
    return [{"kind": k, "label": CALENDAR_KINDS[k][0], "tone": CALENDAR_KINDS[k][1]}
            for k, gate in gates.items() if may(gate)]


# ─────────────────────────────── top 20 ──────────────────────────────────────

TOP_N = 20


def top_twenty(company, period, may):
    s, e = period.start, period.end
    out = []
    if may("procurement:bill_list") and may("procurement:ap_aging"):
        rows = (_bills(company).filter(bill_date__range=(s, e))
                .values("supplier_id", "supplier__name")
                .annotate(amount=Sum(book_value(), output_field=MONEY), n=Count("pk"))
                .order_by("-amount", "supplier__name")[:TOP_N])
        detail = may("crm:supplier_detail")
        out.append({"key": "vendors", "label": "Vendors", "what": "Purchases (bills)", "rows": [
            {"name": r["supplier__name"], "amount": cents(r["amount"]), "count": r["n"],
             "url": reverse("crm:supplier_detail", args=[r["supplier_id"]]) if detail else ""}
            for r in rows]})
    if may("sales:invoice_list") and may("sales:ar_aging"):
        rows = (_invoices(company).filter(invoice_date__range=(s, e))
                .values("customer_id", "customer__name")
                .annotate(amount=Sum(book_value(), output_field=MONEY), n=Count("pk"))
                .order_by("-amount", "customer__name")[:TOP_N])
        detail = may("crm:customer_detail")
        out.append({"key": "customers", "label": "Customers", "what": "Sales (invoices)", "rows": [
            {"name": r["customer__name"], "amount": cents(r["amount"]), "count": r["n"],
             "url": reverse("crm:customer_detail", args=[r["customer_id"]]) if detail else ""}
            for r in rows]})
    if may("inventory:product_list") and may("sales:invoice_list") and may("sales:ar_aging"):
        from apps.sales.models import InvoiceLine
        net = (F("quantity") * F("unit_price")
               * (Value(Decimal("100")) - F("discount_percent")) / Value(Decimal("100"))
               * Coalesce(F("invoice__exchange_rate"), Value(ONE, output_field=RATE)))
        rows = (InvoiceLine.objects.filter(
                    invoice__in=_invoices(company).filter(invoice_date__range=(s, e)),
                    product__isnull=False)
                .values("product_id", "product__name")
                .annotate(amount=Sum(net, output_field=MONEY), qty=Sum("quantity"))
                .order_by("-amount", "product__name")[:TOP_N])
        detail = may("inventory:product_detail")
        out.append({"key": "inventory", "label": "Inventory", "what": "Revenue excl. tax", "rows": [
            {"name": r["product__name"], "amount": cents(r["amount"]), "qty": r["qty"],
             "url": reverse("inventory:product_detail", args=[r["product_id"]]) if detail else ""}
            for r in rows]})
    return out


# ─────────────────────────────── the page ────────────────────────────────────

QUICK_ACCESS = [
    # (route the tile opens, label, icon, extra right needed)
    ("banking:account_list", "Cash Book", "bank", None),
    ("banking:transaction_list", "Receipt & Payment Journal", "cash", None),
    ("accounting:journal_entry_create", "Journal Entry", "document", "POST"),
    ("procurement:bill_list", "Purchase Invoices", "cart", None),
    ("sales:invoice_list", "Sales Invoices", "receipt", None),
]


def build(request, may):
    """Context for the workspace. Each block is None (or empty) when the role
    may not open the page behind it, and is then never computed."""
    company, user = request.active_company, request.user
    today = company_today(company)
    period = parse_period(request.GET, today)
    sales_money = may("sales:ar_aging")
    sales_pages = all(may(n) for n in ("sales:invoice_list", "sales:order_list", "sales:quote_list"))

    ctx = {
        "period": period,
        "period_controls": period_controls(period),
        "tiles": document_tiles(company, user, period, may),
        "gauge": None, "efficiency": None, "tasks": None,
        "calendar": None, "calendar_kinds": calendar_kinds(may),
    }
    ctx["tile_rows"] = [[t for t in ctx["tiles"] if t["row"] == r] for r in (1, 2)]
    if may("sales:invoice_list") and sales_money:
        ctx["gauge"] = collection_gauge(company, user, period)
    if sales_pages and sales_money:
        ctx["efficiency"] = efficiency(company, user, period, may)
        ctx["efficiency_chart"] = {k: ctx["efficiency"][k] for k in ("labels", "mine", "company")}
    if may("projects:task_list"):
        ctx["tasks"] = task_counters(company, user, today)
    ctx["approvals"] = approval_counters(company, user, may, today)
    if ctx["calendar_kinds"]:
        ctx["calendar"] = calendar_events(company, user, month_start(today), may, sales_money)
        ctx["calendar"]["today"] = today.isoformat()
    ctx["top20"] = top_twenty(company, period, may)
    ctx["quick_access"] = [
        {"url": reverse(name), "label": label, "icon": icon}
        for name, label, icon, extra in QUICK_ACCESS
        if may(name) and (extra is None or may(name, extra))]
    return ctx
