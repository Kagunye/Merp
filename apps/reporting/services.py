"""Report calculations that are more than a ledger query."""
from collections import defaultdict
from decimal import Decimal

Z = Decimal("0")
UNCATEGORISED = "Uncategorised"


def _share(total, part, whole):
    """``total`` x part / whole, or nothing when whole is nothing."""
    if not whole:
        return Z
    return total * part / whole


def _invoice_line_revenue(invoice, lines):
    """Each line's revenue in the book's currency, exactly as the invoice's
    journal entry credited it: the same rounding, the same spread of the
    cents, the same rate (see gl_service._invoice_entry_amounts)."""
    from apps.accounting.gl_service import _invoice_entry_amounts
    from apps.accounting.models import to_base
    if not lines:
        return []
    try:
        rate = invoice.rate
    except Exception:  # a posted invoice always has one; never 500 a report
        rate = invoice.exchange_rate or Decimal("1")
    total_base = invoice.total_amount_base or to_base(invoice.total_amount, rate)
    rows = _invoice_entry_amounts(invoice, rate, total_base, None, None)
    revenue = rows[-len(lines):] if len(rows) >= len(lines) else []
    if len(revenue) != len(lines):
        return [Z] * len(lines)
    return [base for _acct, base, _desc in revenue]


def _invoice_entries(company, date_from, date_to):
    """(product, quantity, revenue, cost) for every line of every live invoice
    and credit note dated in the window, in the book's currency.

    Drafts were never posted and a cancelled invoice had its revenue reversed
    and its goods put back, so neither counts. A credit note counts against
    the sale: its revenue (and, when the goods came back, their quantity and
    cost) is negative.

    The cost is what the stock movements the document made were booked at:
    the issue a direct invoice's line made, or the return a credit note's
    line made. An order's invoice issued nothing itself -- its goods left on
    the order's deliveries -- so its lines take their share, by quantity, of
    what those deliveries of the product cost.
    """
    from apps.inventory.models import StockMovement
    from apps.sales.models import Invoice

    invoices = (Invoice.objects.filter(company=company, invoice_date__gte=date_from,
                                       invoice_date__lte=date_to)
                .exclude(status__in=("DRAFT", "CANCELLED")))
    order_cost = {}
    for invoice in invoices:
        note = invoice.status == "CREDIT_NOTE" or bool(invoice.original_invoice_id)
        sign = -1 if note else 1
        lines = list(invoice.lines.select_related(
            "product", "product__income_account", "tax_rate", "tax_rate__account",
            "stock_movement"))
        revenue = _invoice_line_revenue(invoice, lines)
        for line, rev in zip(lines, revenue):
            cost = Z
            moves_goods = (not note) or invoice.goods_returned
            if line.stock_movement_id:
                cost = line.stock_movement.total_cost or Z
            elif (not note and invoice.sales_order_id and line.product_id
                    and line.product.track_inventory):
                key = (invoice.sales_order_id, line.product_id)
                if key not in order_cost:
                    qty_value = [(m.quantity, m.total_cost) for m in StockMovement.objects.filter(
                        sales_order_id=invoice.sales_order_id, product_id=line.product_id,
                        movement_type="DELIVERY")]
                    order_cost[key] = (sum((q for q, _ in qty_value), Z),
                                       sum((v for _, v in qty_value), Z))
                delivered, value = order_cost[key]
                cost = _share(value, min(line.quantity, delivered), delivered)
            yield (line.product, (line.quantity if moves_goods else Z) * sign,
                   rev * sign, cost * sign)


def _pos_entries(company, date_from, date_to):
    """(product, quantity, revenue, cost) for every till line rung in the window.

    Revenue is the line before tax, which is what the till's entry credited.
    A refund is a ticket of its own and counts negative. The cost is what the
    ticket's own stock movements (its reference, DELIVERY for a sale and
    RETURN_IN for a refund) were booked at, shared between the ticket's lines
    of the same product by quantity.
    """
    from apps.inventory.models import StockMovement
    from apps.pos.models import POSSale

    sales = POSSale.objects.filter(
        session__company=company, created_at__date__gte=date_from,
        created_at__date__lte=date_to)
    for sale in sales:
        refund = bool(sale.refund_of_id)
        sign = -1 if refund else 1
        lines = list(sale.lines.select_related("product"))
        moves = defaultdict(lambda: Z)
        for m in StockMovement.objects.filter(
                reference=sale.reference, product__company=company,
                movement_type="RETURN_IN" if refund else "DELIVERY"):
            moves[m.product_id] += m.total_cost or Z
        sold = defaultdict(lambda: Z)
        for line in lines:
            sold[line.product_id] += line.quantity
        for line in lines:
            cost = _share(moves.get(line.product_id, Z), line.quantity, sold[line.product_id])
            yield (line.product, line.quantity * sign, line.net_total * sign, cost * sign)


def sales_by_category(company, date_from, date_to, include_invoices=True, include_pos=True):
    """Quantity, revenue and cost of sales per category for the window,
    rolled up level 1 > 2 > 3, plus an "Uncategorised" row.

    Returns {"rows": [...], "totals": {...}}. Every row carries ``qty``,
    ``revenue``, ``cost``, ``margin``, ``margin_pct`` and ``share_pct`` (of
    revenue). A parent's row is the sum of everything beneath it; when a
    parent also has products filed directly under it, a "Directly under …"
    row is added beneath it, so each set of children always adds up to its
    parent and the level-1 rows plus Uncategorised add up to the totals.
    Each line's cost is rounded to the cent before it is added in, so no
    row can drift from the sum of the rows beneath it.
    """
    from apps.inventory.models import ProductCategory

    direct = defaultdict(lambda: {"qty": Z, "revenue": Z, "cost": Z})
    sources = []
    if include_invoices:
        sources.append(_invoice_entries(company, date_from, date_to))
    if include_pos:
        sources.append(_pos_entries(company, date_from, date_to))
    for source in sources:
        for product, qty, revenue, cost in source:
            key = product.category_id if product is not None else None
            bucket = direct[key]
            bucket["qty"] += qty or Z
            bucket["revenue"] += revenue or Z
            # Every figure is held to the cent per line, so the rows (each a
            # plain sum of lines) add up to their parents exactly.
            bucket["cost"] += Decimal(cost or Z).quantize(Decimal("0.01"))

    tree = ProductCategory.tree(company, active_only=False)
    known = set()

    def totals_of(node):
        cat = node["category"]
        known.add(cat.pk)
        own = direct.get(cat.pk, {"qty": Z, "revenue": Z, "cost": Z})
        node["own"] = dict(own)
        node["sum"] = dict(own)
        for child in node["children"]:
            child_sum = totals_of(child)
            for k in ("qty", "revenue", "cost"):
                node["sum"][k] += child_sum[k]
        return node["sum"]

    for node in tree:
        totals_of(node)

    def cents(x):
        return Decimal(x).quantize(Decimal("0.01"))

    def row(label, level, figures, category=None, kind="category"):
        revenue, cost = cents(figures["revenue"]), cents(figures["cost"])
        return {"label": label, "level": level, "category": category, "kind": kind,
                "qty": figures["qty"], "revenue": revenue, "cost": cost,
                "margin": revenue - cost}

    def active(figures):
        return any(figures[k] for k in ("qty", "revenue", "cost"))

    rows = []

    def walk(node):
        if not active(node["sum"]):
            return
        cat = node["category"]
        rows.append(row(cat.name, node["level"], node["sum"], category=cat))
        shown_children = [c for c in node["children"] if active(c["sum"])]
        if shown_children and active(node["own"]):
            rows.append(row(f"Directly under {cat.name}", node["level"] + 1, node["own"],
                            category=cat, kind="direct"))
        for child in node["children"]:
            walk(child)

    for node in tree:
        walk(node)

    # Products whose category is not in the tree at all (one of another
    # company's, which should never happen) are reported as uncategorised
    # rather than dropped, so the totals still agree.
    loose = {"qty": Z, "revenue": Z, "cost": Z}
    for key, figures in direct.items():
        if key is None or key not in known:
            for k in loose:
                loose[k] += figures[k]
    if active(loose):
        rows.append(row(UNCATEGORISED, 1, loose, kind="uncategorised"))

    # The totals are the sum of the printed level-1 rows, so they agree to the
    # cent with what the page shows.
    top = [r for r in rows if r["level"] == 1]
    totals = {
        "qty": sum((r["qty"] for r in top), Z),
        "revenue": sum((r["revenue"] for r in top), Z),
        "cost": sum((r["cost"] for r in top), Z),
    }
    totals["margin"] = totals["revenue"] - totals["cost"]
    for r in rows + [totals]:
        r["margin_pct"] = (r["margin"] / r["revenue"] * 100) if r["revenue"] else None
        r["share_pct"] = (r["revenue"] / totals["revenue"] * 100) if totals["revenue"] else None
    return {"rows": rows, "totals": totals}
