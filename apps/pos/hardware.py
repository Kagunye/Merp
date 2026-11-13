"""Till hardware: product pictures, receipt printers and cash drawers.

Pictures are kept in the database as small thumbnails (a serverless host's
disk is wiped, and the offline service worker can keep them).

Printers speak ESC/POS. The bytes are built here, once, for every kind of
connection; who carries them depends on the printer:
  BRIDGE  the browser hands them to the Mofisi bridge on the till PC
          (tools/mofisi-bridge), which opens a socket to the printer's LAN IP;
  EPSON   the browser posts them to an Epson ePOS printer as <command> hex;
  STAR    the browser drives a Star WebPRNT printer (drawer and text);
  SERVER  the server opens the socket itself (a server on the shop's LAN).
Most cash drawers hang off the printer's drawer port, so "open the drawer" is
the ESC p pulse on pin 2 or pin 5.
"""
import base64
import io
import ipaddress
import socket

from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import POSDrawerEvent, POSPrinter, ProductPicture

MAX_UPLOAD = 6 * 1024 * 1024
THUMB = 320

# ─────────────────────────────────── pictures ───────────────────────────────────


def save_picture(product, upload):
    """Shrink an uploaded picture to a thumbnail and keep it with the product."""
    from PIL import Image, UnidentifiedImageError

    if upload is None:
        raise ValidationError("Choose a picture to upload.")
    if getattr(upload, "size", 0) > MAX_UPLOAD:
        raise ValidationError("That picture is over 6 MB. Use a smaller one.")
    try:
        img = Image.open(upload)
        img.load()
    except (UnidentifiedImageError, OSError, ValueError):
        raise ValidationError("That file is not a picture (use JPG, PNG or WebP).")
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "transparency" in img.info else "RGB")
    img.thumbnail((THUMB, THUMB))
    out = io.BytesIO()
    try:
        img.save(out, "WEBP", quality=80, method=4)
        ctype = "image/webp"
    except (OSError, KeyError, ValueError):
        out = io.BytesIO()
        img.convert("RGB").save(out, "JPEG", quality=82, optimize=True)
        ctype = "image/jpeg"
    pic, _ = ProductPicture.objects.update_or_create(
        product=product, defaults={"data": out.getvalue(), "content_type": ctype,
                                   "width": img.width, "height": img.height})
    return pic


def picture_url(product):
    """The picture's address, versioned so browsers can keep it for ever."""
    from django.urls import reverse
    try:
        pic = product.pos_picture
    except ProductPicture.DoesNotExist:
        pic = None
    if pic is not None:
        return reverse("pos:product_picture", args=[product.pk]) + f"?v={int(pic.updated_at.timestamp())}"
    return product.image.url if getattr(product, "image", None) else ""


# ─────────────────────────────────── ESC/POS ────────────────────────────────────

ESC, GS = b"\x1b", b"\x1d"
INIT = ESC + b"@"
LEFT, CENTER, RIGHT = ESC + b"a\x00", ESC + b"a\x01", ESC + b"a\x02"
BOLD, NOBOLD = ESC + b"E\x01", ESC + b"E\x00"
BIG, NORMAL = GS + b"!\x11", GS + b"!\x00"
CUT = GS + b"V\x42\x03"


def drawer_pulse(pin=2):
    """ESC p m t1 t2: pulse the drawer kick-out (m 0 = pin 2, 1 = pin 5)."""
    return ESC + b"p" + (b"\x01" if int(pin) == 5 else b"\x00") + b"\x19\xfa"


def _text(value):
    return str(value).encode("cp437", errors="replace")


def _qr(data):
    """GS ( k: a QR code the printer draws itself (model 2, size 5, level M)."""
    payload = data.encode("ascii", errors="ignore")[:700]
    n = len(payload) + 3
    return (GS + b"(k\x04\x001A2\x00" + GS + b"(k\x03\x001C\x05" + GS + b"(k\x03\x001E1"
            + GS + b"(k" + bytes([n % 256, n // 256]) + b"1P0" + payload + GS + b"(k\x03\x001Q0")


def _cols3(values, width):
    a, b, c = (str(v) for v in values)
    third = width // 3
    return (a[:third].ljust(third) + b[:third].rjust(third) + c.rjust(width - 2 * third))[:width]


def _cols(left, right, width):
    left, right = str(left), str(right)
    space = width - len(right) - 1
    return (left[:space].ljust(space) + " " + right)[:width]


def _money(value):
    return f"{value:,.2f}"


def _shop_header(company, branch):
    """Name, address, phone and PIN, as the receipt prints them."""
    header = ""
    try:
        from .models import POSSettings
        header = (POSSettings.objects.filter(company=company).values_list("receipt_header", flat=True).first() or "")
    except Exception:          # noqa: BLE001
        header = ""
    rows = [("title", (company.legal_name or company.name).upper())]
    custom = [h.strip() for h in header.splitlines() if h.strip() and h.strip() != company.name]
    if custom:
        rows += [("center", h) for h in custom]
    else:
        place = branch or company
        for part in (getattr(place, "address_line1", ""), " ".join(
                p for p in (getattr(place, "postal_code", ""), getattr(place, "city", "")) if p)):
            if part:
                rows.append(("center", part.upper()))
        phone = getattr(branch, "phone", "") or company.phone
        if phone:
            rows.append(("center", phone))
    if company.tax_id:
        rows.append(("center", company.tax_id))
    return rows


def _who(user):
    return (user.get_full_name() or user.username) if user else ""


def receipt_lines(sale):
    """The receipt as structured lines (shared by ESC/POS, Star text and the
    80 mm page). With KRA eTIMS on it is a fiscal receipt in the ETR layout;
    otherwise a plain shop receipt."""
    from apps.kra.services import receipt_for
    company = sale.session.company
    rec = receipt_for("pos.POSSale", sale.pk, company)
    if rec is not None or _etims_on(company):
        return _fiscal_lines(sale, company, rec)
    return _plain_lines(sale, company)


def _plain_lines(sale, company):
    refund = sale.refund_of_id is not None
    out = _shop_header(company, sale.session.branch)
    out.append(("dots", ""))
    if refund:
        out.append(("bold", f"REFUND of {sale.refund_of.reference}"))
    for line in sale.lines.select_related("product"):
        if line.quantity == 1:
            out.append(("cols", (line.product.name, _money(line.line_total))))
        else:
            out.append(("left", line.product.name))
            out.append(("cols", (f"  {line.quantity.normalize():f} x {line.unit_price:,.2f}", _money(line.line_total))))
        if line.discount_amount:
            out.append(("cols", (f"  less {line.discount_percent.normalize():f}%", f"-{_money(line.discount_amount)}")))
    out.append(("blank", ""))
    out.append(("cols", ("Sub Total", _money(sale.subtotal))))
    if sale.discount_amount:
        out.append(("cols", ("Discount", f"-{_money(sale.discount_amount)}")))
    out.append(("cols", ("Sales Tax", _money(sale.tax_amount))))
    out.append(("dots", ""))
    out.append(("total", ("TOTAL", _money(sale.total_amount))))
    out.append(("dots", ""))
    out.append(("cols", ("Paid By:", sale.get_payment_method_display())))
    if sale.payment_method == "CASH" and sale.amount_tendered:
        out.append(("cols", ("Cash", _money(sale.amount_tendered))))
        out.append(("cols", ("Change", _money(sale.change_given or 0))))
    out.append(("blank", ""))
    out.append(("left", timezone.localtime(sale.created_at).strftime("%d/%m/%Y %H:%M")))
    out.append(("left", f"Transaction ID: {sale.reference}"))
    out.append(("left", f"Served by: {_who(sale.session.cashier)}"))
    if sale.customer_id:
        out.append(("left", f"Customer: {sale.customer.name}"))
    out.append(("blank", ""))
    out += [("center", f) for f in _footer(company)]
    return out


def _footer(company):
    try:
        from .models import POSSettings
        text = POSSettings.objects.filter(company=company).values_list("receipt_footer", flat=True).first()
    except Exception:          # noqa: BLE001
        text = ""
    return [t.strip() for t in (text or "Thank you, karibu tena!").splitlines() if t.strip()]


def _fiscal_lines(sale, company, rec):
    """The KRA fiscal receipt (ETR layout): buyer PIN, VAT group letter on
    every line, the VAT breakdown, and the control unit information with QR."""
    from decimal import Decimal
    from apps.kra.services import tax_percent, tax_type
    refund = sale.refund_of_id is not None
    out = _shop_header(company, sale.session.branch)
    out.append(("blank", ""))
    out.append(("center", "- START OF FISCAL RECEIPT -" if not refund else "- START OF FISCAL CREDIT NOTE -"))
    out.append(("blank", ""))
    out.append(("cols", ("Trans no:", sale.reference)))
    out.append(("cols", ("Date:", timezone.localtime(sale.created_at).strftime("%d-%m-%Y %H%M%S"))))
    out.append(("cols", ("Transaction By:", _who(sale.session.cashier)[:20])))
    buyer = sale.customer.tax_id if sale.customer_id else ""
    out.append(("cols", ("Customer PIN:", buyer or "")))
    if sale.customer_id:
        out.append(("cols", ("Customer:", sale.customer.name[:22])))
    out.append(("cols", ("Payment By:", sale.get_payment_method_display().upper())))
    if refund:
        out.append(("cols", ("Original Trans:", sale.refund_of.reference)))
    out.append(("blank", ""))
    groups = {}
    for i, line in enumerate(sale.lines.select_related("product", "tax_rate"), 1):
        try:
            letter = tax_type(company, line.tax_rate)
        except Exception:      # noqa: BLE001 - an unmapped rate still prints
            letter = "?"
        out.append(("left", f"{i}. {line.product.name}"))
        out.append(("cols", (f"{letter} KES{line.unit_price:,.2f} x {line.quantity.normalize():f}", _money(line.line_total))))
        if line.discount_amount:
            out.append(("cols", (f"   discount {line.discount_percent.normalize():f}%", f"-{_money(line.discount_amount)}")))
        g = groups.setdefault(letter, [Decimal("0"), Decimal("0")])
        g[0] += line.line_total - line.tax_amount       # vatable (taxable) amount
        g[1] += line.tax_amount
    out.append(("center", "--------PAYMENT DETAILS--------"))
    out.append(("cols", ("SUB Incl. TAX", _money(sale.total_amount))))
    labels = {"B": "VAT16%", "E": "VAT8%", "C": "ZERO RATED", "A": "EXEMPTED", "D": "NON-VAT"}
    for letter in ("B", "E", "C", "A", "D"):
        if letter in groups:
            pct = tax_percent(letter)
            out.append(("cols", (f"{labels[letter]} @{pct.normalize():f}%", _money(groups[letter][1]))))
    out.append(("cols", ("Rounding Adjustment", _money(Decimal("0")))))
    out.append(("total", ("Amount Due", _money(sale.total_amount))))
    paid = sale.amount_tendered if sale.payment_method == "CASH" and sale.amount_tendered else sale.total_amount
    out.append(("cols", (sale.get_payment_method_display(), _money(paid))))
    out.append(("cols", ("Total Paid:", _money(paid))))
    out.append(("cols", ("Change", _money(sale.change_given or 0))))
    out.append(("rule", ""))
    if rec is not None:
        out.append(("cols", ("Device Id:", rec.sdc_id)))
        out.append(("cols", ("Receipt No:", f"{rec.receipt_no}/{rec.total_receipt_no}" if rec.total_receipt_no else rec.receipt_no)))
    out.append(("cols3", ("VATGrp", "VATABLE AMT", "VAT AMT")))
    for letter in sorted(groups):
        pct = tax_percent(letter) if letter != "?" else Decimal("0")
        out.append(("cols3", (f"{letter} = {pct.normalize():f}%", _money(groups[letter][0]), _money(groups[letter][1]))))
    out.append(("center", "--- CONTROL UNIT INFO ---"))
    if rec is not None:
        out.append(("cols", ("CU Serial No:", rec.sdc_id)))
        out.append(("cols", ("CU Invoice No:", rec.cu_invoice_no)))
        out.append(("left", "Internal Data:"))
        out.append(("left", rec.dashed(rec.internal_data)))
        out.append(("left", "Receipt Signature:"))
        out.append(("left", rec.dashed(rec.signature)))
        if rec.mrc_no:
            out.append(("cols", ("MRC No:", rec.mrc_no)))
        if rec.signed_at:
            out.append(("cols", ("Signed:", timezone.localtime(rec.signed_at).strftime("%d-%m-%Y %H:%M:%S"))))
        if rec.qr_url:
            out.append(("qr", rec.qr_url))
    else:
        out.append(("center", "KRA signature pending"))
        out.append(("center", "(reprint once signed)"))
    out.append(("center", "- END OF FISCAL RECEIPT -" if not refund else "- END OF FISCAL CREDIT NOTE -"))
    out += [("center", f) for f in _footer(company)]
    return out


def _etims_on(company):
    try:
        from apps.kra.services import is_on
        return is_on(company)
    except Exception:          # noqa: BLE001
        return False


def receipt_bytes(sale, printer, kick=False):
    width = printer.paper_chars
    out = [INIT]
    for kind, value in receipt_lines(sale):
        if kind == "title":
            out += [CENTER, BOLD, BIG, _text(value[: width // 2]), b"\n", NORMAL, NOBOLD]
        elif kind == "center":
            out += [CENTER, _text(value[:width]), b"\n"]
        elif kind == "bold":
            out += [LEFT, BOLD, _text(value[:width]), b"\n", NOBOLD]
        elif kind == "left":
            out += [LEFT, _text(value[:width]), b"\n"]
        elif kind == "cols":
            out += [LEFT, _text(_cols(value[0], value[1], width)), b"\n"]
        elif kind == "total":
            out += [LEFT, BOLD, _text(_cols(value[0], value[1], width)), b"\n", NOBOLD]
        elif kind == "rule":
            out += [LEFT, _text("-" * width), b"\n"]
        elif kind == "dots":
            out += [LEFT, _text("." * width), b"\n"]
        elif kind == "blank":
            out += [b"\n"]
        elif kind == "cols3":
            out += [LEFT, _text(_cols3(value, width)), b"\n"]
        elif kind == "qr":
            out += [CENTER, _qr(value), b"\n"]
    out += [b"\n\n\n", CUT]
    if kick:
        out.append(drawer_pulse(printer.drawer_pin))
    return b"".join(out)


def kot_lines(ticket):
    """A kitchen ticket: big station name, table and time, then the items large."""
    o = ticket.order
    when = timezone.localtime(ticket.created_at).strftime("%H:%M")
    head = ("*** CANCEL ***" if ticket.kind == "VOID" else ticket.prep_station.name.upper())
    out = [("title", head),
           ("center", f"{ticket.number} · {when}"),
           ("bold", f"{o.table.name if o.table_id else 'No table'} · {o.reference}"),
           ("left", f"Waiter: {o.waiter.name}" + (f" · Guests: {o.covers}" if o.covers else "")),
           ("rule", "")]
    for item in ticket.items:
        out.append(("big", f"{item['qty']} x {item['name']}"))
        if item.get("note"):
            out.append(("left", f"   >> {item['note']}"))
    out.append(("rule", ""))
    return out


def kot_bytes(ticket, printer):
    width = printer.paper_chars
    out = [INIT]
    for kind, value in kot_lines(ticket):
        if kind == "title":
            out += [CENTER, BOLD, BIG, _text(value[: width // 2]), b"\n", NORMAL, NOBOLD]
        elif kind == "big":
            out += [LEFT, BOLD, GS + b"!\x01", _text(value[:width]), b"\n", NORMAL, NOBOLD]
        elif kind == "center":
            out += [CENTER, _text(value[:width]), b"\n"]
        elif kind == "bold":
            out += [LEFT, BOLD, _text(value[:width]), b"\n", NOBOLD]
        elif kind == "left":
            out += [LEFT, _text(value[:width]), b"\n"]
        elif kind == "rule":
            out += [LEFT, _text("-" * width), b"\n"]
    out += [b"\n\n\n", CUT]
    return b"".join(out)


def kot_text(ticket, width=42):
    rows = []
    for kind, value in kot_lines(ticket):
        if kind == "rule":
            rows.append("-" * width)
        elif kind in ("title", "center"):
            rows.append(str(value)[:width].center(width))
        else:
            rows.append(str(value)[:width])
    return "\n".join(rows)


def kot_printer(ticket):
    """The printer for a ticket: the station's own, for the bill's branch."""
    printer = ticket.prep_station.printer
    if printer is None or not printer.is_active:
        return None
    if printer.branch_id and printer.branch_id != ticket.order.branch_id:
        return None
    return printer


def receipt_text(sale, width=42):
    """Plain text for printers driven by text commands (Star WebPRNT)."""
    rows = []
    for kind, value in receipt_lines(sale):
        if kind in ("title", "center"):
            rows.append(str(value)[:width].center(width))
        elif kind in ("cols", "total"):
            rows.append(_cols(value[0], value[1], width))
        elif kind == "rule":
            rows.append("-" * width)
        elif kind == "dots":
            rows.append("." * width)
        elif kind == "blank":
            rows.append("")
        elif kind == "cols3":
            rows.append(_cols3(value, width))
        elif kind in ("left", "bold"):
            rows.append(str(value)[:width])
    return "\n".join(rows)


def test_bytes(printer):
    width = printer.paper_chars
    return b"".join([INIT, CENTER, BOLD, _text("Mofisi printer test"), b"\n", NOBOLD,
                     _text(printer.name[:width]), b"\n", _text(f"{printer.host}:{printer.port}"), b"\n",
                     _text(timezone.localtime().strftime("%d/%m/%Y %H:%M:%S")), b"\n\n\n", CUT])


def b64(data):
    return base64.b64encode(data).decode("ascii")


# ─────────────────────────────── the server's socket ──────────────────────────────

PRINTER_PORTS = range(9100, 9110)     # raw ESC/POS (JetDirect) ports


def _private(host):
    """A printer on the shop's LAN. Loopback only while developing (DEBUG), and
    never link-local (169.254.x: cloud metadata services live there)."""
    from django.conf import settings
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
    except (OSError, ValueError):
        return False
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return False
    if ip.is_loopback:
        return bool(settings.DEBUG)
    return ip.is_private


def send_raw(host, port, data, timeout=4):
    """For SERVER printers: open a socket to the printer and send the bytes.
    Only printer ports on a private network are allowed (never the internet)."""
    if int(port) not in PRINTER_PORTS:
        raise ValidationError(f"Port {port} is not a printer port; the server only sends to ports 9100-9109.")
    if not _private(host):
        raise ValidationError(f"{host} is not an address on a private network; the server only talks "
                              "to printers on the shop's LAN.")
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            sock.sendall(data)
    except OSError as exc:
        raise ValidationError(f"Could not reach the printer at {host}:{port} ({exc}).")


# ─────────────────────────────────── drawer log ──────────────────────────────────

def _event_branch(printer, session, sale):
    """Where the drawer is: the ticket's till, else the open till, else the printer's branch."""
    if sale is not None and sale.session_id:
        return sale.session.branch
    if session is not None:
        return session.branch
    return printer.branch if printer is not None else None


def log_drawer(company, user, reason, *, printer=None, session=None, sale=None, note="", ok=True, error=""):
    if reason not in dict(POSDrawerEvent.REASONS):
        raise ValidationError("Unknown reason for opening the drawer.")
    if reason == "NO_SALE" and not (note or "").strip():
        raise ValidationError("Say why the drawer is being opened with no sale; it goes on the day end.")
    return POSDrawerEvent.objects.create(
        company=company, branch=_event_branch(printer, session, sale),
        printer=printer, session=session, sale=sale, user=user, reason=reason, note=(note or "")[:255],
        ok=bool(ok), error=(error or "")[:255])


def printers_for(company, branch=None):
    qs = POSPrinter.objects.filter(company=company, is_active=True)
    if branch is not None:
        from django.db.models import Q
        qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
    return qs


def printer_config(printer):
    """What the browser needs to drive a printer."""
    return {"id": str(printer.pk), "name": printer.name, "connection": printer.connection,
            "host": printer.host, "port": printer.port, "bridge": printer.bridge_url.rstrip("/"),
            "pin": printer.drawer_pin, "chars": printer.paper_chars,
            "drawer_on_cash": printer.open_drawer_on_cash, "print": printer.print_receipts,
            "drawer_b64": b64(drawer_pulse(printer.drawer_pin))}
