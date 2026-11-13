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


def _cols(left, right, width):
    left, right = str(left), str(right)
    space = width - len(right) - 1
    return (left[:space].ljust(space) + " " + right)[:width]


def receipt_lines(sale):
    """The receipt as structured lines (shared by ESC/POS and Star text)."""
    from apps.kra.services import receipt_for
    company = sale.session.company
    header = ""
    try:
        from .models import POSSettings
        header = (POSSettings.objects.filter(company=company).values_list("receipt_header", flat=True).first()
                  or "")
    except Exception:          # noqa: BLE001
        header = ""
    lines = [("title", company.name)]
    for h in header.splitlines():
        if h.strip() and h.strip() != company.name:
            lines.append(("center", h.strip()))
    if company.tax_id:
        lines.append(("center", f"PIN: {company.tax_id}"))
    lines.append(("center", sale.session.branch.name))
    lines.append(("rule", ""))
    refund = sale.refund_of_id is not None
    lines.append(("bold", ("REFUND " if refund else "RECEIPT ") + sale.reference))
    lines.append(("cols", ("Date", timezone.localtime(sale.created_at).strftime("%d/%m/%Y %H:%M"))))
    who = sale.session.cashier
    lines.append(("cols", ("Cashier", who.get_full_name() or who.username)))
    if sale.customer_id:
        lines.append(("cols", ("Customer", sale.customer.name)))
    lines.append(("rule", ""))
    for line in sale.lines.select_related("product"):
        lines.append(("left", line.product.name))
        lines.append(("cols", (f"  {line.quantity.normalize():f} x {line.unit_price:,.2f}", f"{line.line_total:,.2f}")))
        if line.discount_amount:
            lines.append(("cols", (f"  less {line.discount_percent.normalize():f}%", f"-{line.discount_amount:,.2f}")))
    lines.append(("rule", ""))
    lines.append(("cols", ("Subtotal", f"{sale.subtotal:,.2f}")))
    if sale.discount_amount:
        lines.append(("cols", ("Discount", f"-{sale.discount_amount:,.2f}")))
    lines.append(("cols", ("VAT", f"{sale.tax_amount:,.2f}")))
    lines.append(("total", ("TOTAL KES", f"{sale.total_amount:,.2f}")))
    lines.append(("cols", (sale.get_payment_method_display(), f"{sale.amount_tendered:,.2f}")))
    if sale.change_given:
        lines.append(("cols", ("Change", f"{sale.change_given:,.2f}")))
    rec = receipt_for("pos.POSSale", sale.pk, company)
    if rec is not None:
        lines += [("rule", ""), ("center", "SCU INFORMATION"),
                  ("cols", ("Date", timezone.localtime(rec.signed_at).strftime("%d/%m/%Y %H:%M:%S") if rec.signed_at else "")),
                  ("cols", ("SCU ID", rec.sdc_id)), ("cols", ("CU INVOICE NO.", rec.cu_invoice_no)),
                  ("left", "Internal Data:"), ("left", rec.dashed(rec.internal_data)),
                  ("left", "Receipt Signature:"), ("left", rec.dashed(rec.signature))]
        if rec.qr_url:
            lines.append(("qr", rec.qr_url))
    elif _etims_on(company):
        lines += [("rule", ""), ("center", "KRA signature pending")]
    lines += [("rule", ""), ("center", "Thank you, karibu tena!")]
    return lines


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
        elif kind == "qr":
            out += [CENTER, _qr(value), b"\n"]
    out += [b"\n\n\n", CUT]
    if kick:
        out.append(drawer_pulse(printer.drawer_pin))
    return b"".join(out)


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
