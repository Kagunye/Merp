"""Small presentation helpers shared across templates."""
import zlib
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template
from django.utils.html import conditional_escape, format_html

register = template.Library()


@register.simple_tag(takes_context=True)
def can_do(context, route, method="GET"):
    """Whether the signed-in user may open `route` ("namespace:url_name").

    Templates ask this before showing a button, so a role never sees an action
    the role table would only answer with a 403:
    ``{% can_do "sales:invoice_post" "POST" as may_post %}{% if may_post %}…``
    """
    from apps.core import access

    request = context.get("request")
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    namespace, _, url_name = route.partition(":")
    return access.can(user, getattr(request, "company_role", None),
                      namespace, url_name, method)


@register.simple_tag
def page_url(request, page):
    """Link to `page` while keeping every other query parameter (filters, search)."""
    params = request.GET.copy()
    params["page"] = page
    return f"{request.path}?{params.urlencode()}"


@register.simple_tag
def page_window(paginator, current, span=2):
    """Page numbers to render around the current one — `span` neighbours each side."""
    start = max(1, current - span)
    end = min(paginator.num_pages, current + span)
    return list(range(start, end + 1))


# ── Quantities and units ──────────────────────────────────────────────────

@register.filter
def trim_qty(value):
    """12.0000 reads as 12; quantities are stored to 4 dp but rarely need them."""
    try:
        text = format(Decimal(str(value)), "f")
    except (InvalidOperation, TypeError, ValueError):
        return value
    # Only zeros after the point are padding: 20 must stay 20, not become 2.
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@register.filter
def with_unit(value, uom):
    """Render '12 PCS' — and leave the number bare when no unit is on file."""
    amount = trim_qty(value)
    abbrev = getattr(uom, "abbreviation", None)
    return f"{amount} {abbrev}" if abbrev else amount

# ── Account shading ───────────────────────────────────────────────────────# Every ledger account gets its own shade so a reader can follow one account
# through the chart, the general ledger and a bank statement without reading
# the code each time. The hue is anchored to the account type -- assets stay
# cool, liabilities and tax stay warm -- and the code/name pair spreads the
# members of a family apart. It is a hash, not stored state, so the same
# account is the same colour on every page and on every reload.
TYPE_HUES = {
    "ASSET": 208,
    "CASH": 165,
    "BANK": 192,
    "RECEIVABLE": 250,
    "LIABILITY": 33,
    "PAYABLE": 14,
    "EQUITY": 288,
    "REVENUE": 138,
    "EXPENSE": 345,
    "COST_OF_GOODS": 48,
    "TAX": 95,
}


def _ledger_account(obj):
    """Accept an Account, or anything that points at one (a BankAccount)."""
    if obj is None:
        return None
    return getattr(obj, "gl_account", None) or obj


def _seed(obj):
    account = _ledger_account(obj)
    if account is None:
        return 0, 210
    code = getattr(account, "code", "") or ""
    name = getattr(account, "name", "") or ""
    account_type = getattr(account, "account_type", "") or ""
    return zlib.crc32(f"{code}|{name}".encode()), TYPE_HUES.get(account_type, 210)


def _palette(obj):
    """One place that turns an account into colours, so a chip, a row and a
    heading for the same account can never disagree."""
    seed, base = _seed(obj)
    hue = (base + seed % 21 - 10) % 360
    return {
        "hue": hue,
        "edge": f"hsl({hue}, {42 + seed % 18}%, {44 + seed % 10}%)",
        "wash": f"hsl({hue}, {45 + seed % 20}%, 96.5%)",
        "chip": (
            f"background:hsl({hue}, 60%, 92%);"
            f"color:hsl({hue}, 55%, 28%);border:1px solid hsl({hue}, 50%, 80%)"
        ),
    }


@register.simple_tag
def account_hue(obj):
    """The hue for an account -- the raw number, for custom styling."""
    return _palette(obj)["hue"]


@register.simple_tag
def account_edge(obj):
    """Strong colour for accents: chips, dots, borders, headings."""
    return _palette(obj)["edge"]


@register.simple_tag
def account_wash(obj):
    """Pale wash to shade a whole row or panel."""
    return _palette(obj)["wash"]


@register.simple_tag
def account_row(obj):
    """One style attribute: pale wash across the row, edge stripe down the left."""
    p = _palette(obj)
    return f"background:{p['wash']};box-shadow:inset 3px 0 0 {p['edge']}"


@register.simple_tag
def account_chip(obj):
    """Style for the code pill, so account codes read as colours too."""
    return _palette(obj)["chip"]


@register.simple_tag
def account_dot(obj):
    """Style for a small round swatch next to an account name."""
    return f"background:{_palette(obj)['edge']}"


@register.simple_tag
def account_legend():
    """(type label, swatch style) pairs for the account-type families."""
    return [
        (label.replace("_", " ").title(), f"background:hsl({hue}, 50%, 55%)")
        for label, hue in sorted(TYPE_HUES.items())
    ]


# ── Numbers: money, quantities, percentages ──────────────────────────────
# Every amount on every page goes through `money`, so 126500 always reads
# "126,500.00" and a negative is always marked the same way. They are safe on
# None, "" and junk: a blank renders as an em dash, junk is shown as it came.
EMPTY = "—"  # em dash: "no value", as opposed to a real zero


def _as_decimal(value):
    """Decimal for anything number-like; None for None/""/junk/NaN/inf."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        d = value
    else:
        text = str(value).strip().replace(",", "")
        if not text:
            return None
        try:
            d = Decimal(text)
        except (InvalidOperation, TypeError, ValueError):
            return None
    return d if d.is_finite() else None


def _grouped(d, places):
    d = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    if d == 0:
        d = abs(d)  # never print "-0.00"
    return f"{d:,.{places}f}"


def _blank_or_junk(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return EMPTY
    if isinstance(value, (float, Decimal)):  # NaN / infinity
        return EMPTY
    return conditional_escape(value)


def _signed(d, text):
    if d < 0 and text.startswith("-"):
        return format_html('<span class="neg">{}</span>', text)
    return text


@register.filter(is_safe=True)
def money(value, currency=None):
    """126500 -> "126,500.00";  -50 -> <span class="neg">-50.00</span>.

    ``{{ x|money:"KES" }}`` prefixes the currency code: "KES 126,500.00".
    None or "" render an em dash; junk renders as given (escaped).
    Use it inside a ``<td class="num">`` so the column right-aligns.
    """
    d = _as_decimal(value)
    if d is None:
        return _blank_or_junk(value)
    text = _grouped(d, 2)
    out = _signed(d, text)
    if currency:
        return format_html("{} {}", currency, out)
    return out


@register.filter(is_safe=True)
def qty(value, places=None):
    """Quantities with thousands separators: 1250.5000 -> "1,250.5".

    Trailing zeros are trimmed (up to 4 dp kept). ``{{ x|qty:2 }}`` fixes the
    decimals instead: "1,250.50". Negatives get the ``neg`` class like money.
    """
    d = _as_decimal(value)
    if d is None:
        return _blank_or_junk(value)
    try:
        fixed = None if places in (None, "") else max(0, int(places))
    except (TypeError, ValueError):
        fixed = None
    if fixed is not None:
        text = _grouped(d, fixed)
    else:
        text = _grouped(d, 4)
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        if text == "-0":
            text = "0"
    return _signed(d, text)


@register.filter(is_safe=True)
def pct(value, places=1):
    """A value that is already a percentage: 16 -> "16%", 12.345 -> "12.3%".

    ``{{ x|pct:2 }}`` keeps up to 2 decimals; trailing zeros are trimmed.
    For a ratio (0.16) multiply by 100 first.
    """
    d = _as_decimal(value)
    if d is None:
        return _blank_or_junk(value)
    try:
        places = max(0, int(places))
    except (TypeError, ValueError):
        places = 1
    text = _grouped(d, places)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text == "-0":
        text = "0"
    return _signed(d, f"{text}%")


# ── Status badges ─────────────────────────────────────────────────────────
# One table maps every status code in the app to a badge colour, so "PAID" is
# the same green on invoices, bills, payslips and loans. The class names are
# written out in full so the Tailwind build (which scans apps/**/*.py) keeps them.
STATUS_VARIANTS = {
    "badge-success": {
        "PAID", "POSTED", "APPROVED", "COMPLETED", "ACTIVE", "DELIVERED", "DONE",
        "RESOLVED", "HIRED", "WON", "FULLY_RECEIVED", "RECONCILED", "APPLIED",
        "RELEASED", "FILLED", "ACCEPTED", "SETTLED", "CONVERTED", "QUALIFIED",
        "INVOICED", "BILLED", "CREDITED", "PROCESSED", "SUCCESS", "YES", "TRUE",
    },
    "badge-warning": {
        "PARTIAL", "PENDING", "PENDING_APPROVAL", "SUBMITTED", "PARTIALLY_DELIVERED",
        "PARTIALLY_RECEIVED", "PARTIALLY_PAID", "ON_HOLD", "REVIEW", "WAITING",
        "UNDER_MAINTENANCE", "PROBATION", "ON_LEAVE", "NEGOTIATION", "DUE",
        "PARTIAL_REFUND", "HIGH", "LOCKED", "WARNING",
    },
    "badge-danger": {
        "OVERDUE", "REJECTED", "FAILED", "VOID", "LOST", "TERMINATED", "EXPIRED",
        "WRITTEN_OFF", "URGENT", "NO_SHOW", "ERROR", "BLOCKED", "SUSPENDED",
        "DECLINED", "DISPUTED", "BOUNCED",
    },
    "badge-info": {
        "SENT", "OPEN", "CONFIRMED", "IN_PROGRESS", "SCHEDULED", "NEW", "RECEIVED",
        "ISSUED", "CONTACTED", "INTERVIEW", "OFFER", "SCREENING", "PROPOSAL",
        "PLANNING", "PO_CREATED", "TODO", "PROCESSING", "READY", "ASSIGNED", "INFO",
        "MEDIUM", "REFUNDED",
    },
    "badge-neutral": {
        "DRAFT", "CANCELLED", "CLOSED", "INACTIVE", "ARCHIVED", "REVERSED",
        "WITHDRAWN", "RETIRED", "DISPOSED", "RESIGNED", "OBSOLETE", "LOW", "NO",
        "FALSE", "NONE",
    },
}
_STATUS_LOOKUP = {code: variant for variant, codes in STATUS_VARIANTS.items() for code in codes}


def _status_key(status):
    return str(status if status is not None else "").strip().upper().replace(" ", "_").replace("-", "_")


@register.filter
def status_variant(status):
    """The badge class for a status code: "PAID" -> "badge-success".

    Booleans work too: True -> success, False -> neutral. Unknown -> neutral.
    """
    return _STATUS_LOOKUP.get(_status_key(status), "badge-neutral")


@register.filter
def status_label(status):
    """"PARTIALLY_DELIVERED" -> "Partially delivered"."""
    text = str(status if status is not None else "").strip().replace("_", " ")
    return text[:1].upper() + text[1:].lower() if text else EMPTY


@register.simple_tag
def status_badge(status, label=None):
    """``{% status_badge inv.status inv.get_status_display %}`` -> a coloured pill.

    The label defaults to the code in sentence case.
    """
    return format_html(
        '<span class="badge {}" data-status="{}"><span class="badge-dot" aria-hidden="true"></span>{}</span>',
        status_variant(status), _status_key(status), label or status_label(status),
    )
