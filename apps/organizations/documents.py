"""Printed documents: one A4 layout for quotations, sales orders, invoices,
credit notes and purchase orders (templates/_print/document.html).

Each document type is turned into the same shape - title, details box,
parties, lines, totals - so the template stays one file. Terms and
Conditions print on their own page only for the types that carry terms
(quotation, sales order, invoice, purchase order); a credit note does not.
Bank details print on invoices only.
"""
import io
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

Z = Decimal("0")
LOGO_MAX = 4 * 1024 * 1024

# kind -> (title, terms key or None, show bank details)
KINDS = {
    "quote": ("Quotation", "quote", False),
    "order": ("Sales Order", "order", False),
    "invoice": ("Tax Invoice", "invoice", True),
    "credit_note": ("Credit Note", None, False),
    "po": ("Purchase Order", "po", False),
}


def layout_for(company):
    from .models import DocumentLayout
    found, _ = DocumentLayout.objects.get_or_create(company=company)
    return found


def save_logo(layout, upload):
    """Keep a print-sized copy of the logo (PNG keeps transparency)."""
    from PIL import Image, UnidentifiedImageError
    if upload is None:
        raise ValidationError("Choose a logo to upload.")
    if getattr(upload, "size", 0) > LOGO_MAX:
        raise ValidationError("That logo is over 4 MB. Use a smaller file.")
    try:
        img = Image.open(upload)
        img.load()
    except (UnidentifiedImageError, OSError, ValueError):
        raise ValidationError("That file is not a picture (use PNG, JPG or WebP).")
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    img.thumbnail((900, 300))
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    layout.logo_data, layout.logo_type, layout.logo_updated = out.getvalue(), "image/png", timezone.now()
    layout.save(update_fields=["logo_data", "logo_type", "logo_updated", "updated_at"])


def logo_url(layout):
    if layout.logo_data:
        return reverse("organizations:document_logo") + f"?v={int(layout.logo_updated.timestamp())}"
    company = layout.company
    try:
        return company.logo.url if company.logo else ""
    except ValueError:
        return ""


def _address(obj):
    parts = [getattr(obj, f, "") for f in ("address_line1", "address_line2")]
    town = " ".join(p for p in (getattr(obj, "city", ""), getattr(obj, "postal_code", "")) if p)
    parts += [town, getattr(obj, "country", "")]
    return [p for p in parts if p]


def _party(obj, label):
    if obj is None:
        return None
    return {"label": label, "name": obj.name, "lines": _address(obj),
            "phone": getattr(obj, "phone", ""), "email": getattr(obj, "email", ""),
            "pin": getattr(obj, "tax_id", "")}


def _line(n, product, description, qty, price, disc_pct, tax_rate):
    gross = qty * price
    disc = (gross * (disc_pct or Z) / 100) if disc_pct else Z
    net = gross - disc
    vat = net * tax_rate.rate / 100 if tax_rate else Z
    uom = getattr(getattr(product, "unit_of_measure", None), "abbreviation", "") if product else ""
    return {"n": n, "code": getattr(product, "sku", "") if product else "",
            "item": getattr(product, "name", "") if product else "",
            "description": description or (product.name if product else ""),
            "qty": qty, "unit": uom or "EA", "price": price, "discount": disc, "vat": vat, "total": net}


def _rep(user):
    return (user.get_full_name() or user.username) if user else ""


def build(kind, doc):
    """The document as the template wants it."""
    company = doc.company
    rows, meta = [], []
    party, ship = None, None
    if kind == "quote":
        lines = doc.lines.select_related("product__unit_of_measure", "tax_rate")
        rows = [_line(i, l.product, l.description, l.quantity, l.unit_price, l.discount_percent, l.tax_rate)
                for i, l in enumerate(lines, 1)]
        meta = [("Number", doc.reference), ("Date", doc.quote_date), ("Valid until", doc.valid_until),
                ("Sales person", _rep(doc.sales_rep))]
        party = _party(doc.customer, "Quoted to")
        date = doc.quote_date
    elif kind == "order":
        lines = doc.lines.select_related("product__unit_of_measure", "tax_rate")
        rows = [_line(i, l.product, l.description, l.quantity, l.unit_price, l.discount_percent, l.tax_rate)
                for i, l in enumerate(lines, 1)]
        meta = [("Number", doc.reference), ("Date", doc.order_date), ("Delivery", doc.delivery_date),
                ("Sales person", _rep(doc.sales_rep))]
        party = _party(doc.customer, "Sold to")
        date = doc.order_date
    elif kind in ("invoice", "credit_note"):
        lines = doc.lines.select_related("product__unit_of_measure", "tax_rate")
        rows = [_line(i, l.product, l.description, l.quantity, l.unit_price, l.discount_percent, l.tax_rate)
                for i, l in enumerate(lines, 1)]
        meta = [("Number", doc.reference), ("Date", doc.invoice_date)]
        if kind == "credit_note":
            meta.append(("Credits invoice", doc.original_invoice.reference if doc.original_invoice_id else ""))
        else:
            meta.append(("Due date", doc.due_date))
            if doc.sales_order_id:
                meta.append(("Order", doc.sales_order.reference))
        party = _party(doc.customer, "Sold to")
        date = doc.invoice_date
    elif kind == "po":
        lines = doc.lines.select_related("product__unit_of_measure", "tax_rate")
        rows = [_line(i, l.product, l.description, l.quantity, l.unit_cost, Z, l.tax_rate)
                for i, l in enumerate(lines, 1)]
        meta = [("Number", doc.reference), ("Date", doc.order_date), ("Deliver by", doc.expected_delivery_date)]
        party = _party(doc.supplier, "Supplier")
        wh = doc.warehouse
        ship = {"label": "Deliver to", "name": wh.name if wh else company.name,
                "lines": _address(wh) if wh else _address(company), "phone": "", "email": "", "pin": ""}
        date = doc.order_date
    else:
        raise ValueError(kind)
    title, terms_key, bank = KINDS[kind]
    layout = layout_for(company)
    if ship is None and layout.show_ship_to and party:
        ship = {**party, "label": "Ship to"}
    # Gross of the lines, then whatever brings it to the stored net (line and
    # document discounts together), so the box always adds up to the total.
    gross = sum((r["qty"] * r["price"] for r in rows), Z).quantize(Decimal("0.01"))
    net = (doc.total_amount or Z) - (doc.tax_amount or Z)
    discount = max(gross - net, Z)
    customer = getattr(doc, "customer", None)
    if customer is not None:
        meta.append(("Account", customer.name))
    return {
        "kind": kind, "doc": doc, "company": company, "layout": layout, "logo": logo_url(layout),
        "title": title, "date": date, "currency": getattr(doc, "currency", company.currency),
        "meta": [(k, v) for k, v in meta if v not in (None, "")],
        "party": party, "ship": ship, "rows": rows,
        "subtotal": net + discount, "discount": discount, "tax": doc.tax_amount, "total": doc.total_amount,
        "notes": getattr(doc, "notes", "") or "",
        "terms": layout.terms_for(terms_key) if terms_key else "",
        "bank": layout.bank_details if bank else "",
        "company_lines": _address(company),
    }


def render_document(request, kind, doc):
    return render(request, "_print/document.html", build(kind, doc))
