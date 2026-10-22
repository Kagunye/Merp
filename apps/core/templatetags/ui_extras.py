"""Small presentation helpers shared across templates."""
import zlib
from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


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
        text = format(Decimal(str(value)), "f").rstrip("0").rstrip(".")
    except (InvalidOperation, TypeError, ValueError):
        return value
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
