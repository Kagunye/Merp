"""Checks for till pictures and LAN printers / cash drawers (apps/pos/hardware.py).

    python manage.py test_fix_hardware      # rolls back

    1. pictures        -> an upload is shrunk to a thumbnail and served from the
                          database; the till card carries its address; another
                          company cannot read it; a file that is not a picture is
                          refused; the back office can upload and remove one
    2. printers        -> the form takes LAN addresses only (no public or
                          link-local address, a bridge only on the till PC, the
                          server only on printer ports)
    3. ESC/POS         -> the drawer pulse is ESC p on pin 2 or 5; a receipt
                          carries the lines, a cut and (on cash) the pulse
    4. sending         -> the server refuses public, link-local and non-printer
                          ports; a SERVER printer really writes the bytes to a
                          socket; the bridge program does the same and refuses
                          the internet
    5. the drawer log  -> a no-sale opening needs a reason; every opening is
                          logged with the ticket and shows on the day end
    6. roles           -> a viewer cannot read the printers; an auditor can read
                          but not open a drawer; the cashier can
"""
import base64
import datetime
import io
import json
import socket
import threading
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand
from django.db import transaction
from django.test import Client, override_settings
from django.test.utils import setup_test_environment, teardown_test_environment
from django.urls import reverse
from django.utils import timezone

D = Decimal


def png(w=900, h=600, color=(200, 30, 30)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


class Listener:
    """A fake receipt printer: accepts one connection on 127.0.0.1 and keeps the bytes."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.sock.settimeout(5)
        self.port = self.sock.getsockname()[1]
        self.data = b""
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            conn, _ = self.sock.accept()
            conn.settimeout(3)
            with conn:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    self.data += chunk
        except OSError:
            pass
        finally:
            self.sock.close()

    def wait(self):
        self.thread.join(6)
        return self.data


class Command(BaseCommand):
    help = "Checks for till pictures, LAN receipt printers and cash drawers."

    def handle(self, *args, **opts):
        self.passed, self.failures = 0, []
        setup_test_environment()
        try:
            with transaction.atomic():
                self.run()
                transaction.set_rollback(True)
        finally:
            teardown_test_environment()
        self.stdout.write("")
        self.stdout.write(f"Checks passed: {self.passed}")
        if self.failures:
            self.stdout.write(self.style.ERROR(f"Failures: {len(self.failures)}"))
            for f in self.failures:
                self.stderr.write(self.style.ERROR(f"  FAIL {f}"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("Failures: 0"))

    def section(self, t):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"-- {t}"))

    def expect(self, label, cond, detail=""):
        if cond:
            self.passed += 1
            self.stdout.write(f"  ok   {label}" + (f"  ({detail})" if detail else ""))
            return True
        self.failures.append(f"{label} — {detail}" if detail else label)
        self.stdout.write(self.style.ERROR(f"  FAIL {label} {detail}"))
        return False

    def account(self, company, code):
        from apps.accounting.models import Account
        return Account.objects.get(company=company, code=code)

    def make_company(self, admin, tag):
        from apps.organizations.models import Company
        name = f"Fix Hardware {tag} {self.stamp}"
        self.client_for(admin).post(reverse("organizations:company_create"), {
            "name": name, "legal_name": name, "registration_number": f"PVT-{tag}{self.stamp}", "tax_id": "",
            "vat_number": "", "currency": "KES", "timezone": "Africa/Nairobi", "fiscal_year_start_month": "1",
            "industry": "Trading", "phone": "+254 700 000 005", "email": f"i@{tag}{self.stamp}.fixhw.local",
            "website": "", "address_line1": "5 Moi Avenue", "address_line2": "", "city": "Nairobi",
            "state_province": "Nairobi", "postal_code": "00100", "country": "Kenya"})
        return Company.objects.get(name=name)

    def user(self, company, name, role):
        from apps.accounts.models import User
        from apps.organizations.models import CompanyMembership
        u = User.objects.create(username=f"{name}.{self.stamp}", email=f"{name}.{self.stamp}@fixhw.local",
                                first_name=name.title(), last_name="Test")
        u.set_password("fix-hw-pw")
        u.save()
        CompanyMembership.objects.create(company=company, user=u, role=role,
                                         branch=company.branches.get(is_headquarters=True), is_default=True)
        return u

    def client_for(self, u):
        c = Client(raise_request_exception=False, enforce_csrf_checks=False)
        c.force_login(u, backend="django.contrib.auth.backends.ModelBackend")
        return c

    def run(self):
        from apps.accounts.models import User
        from apps.inventory.models import Product, StockMovement, UnitOfMeasure
        from apps.organizations.models import Warehouse
        from apps.pos import hardware, restaurant, services as pos
        from apps.pos.forms_backoffice import PrinterForm
        from apps.pos.models import (POSDrawerEvent, POSMenu, POSMenuItem, POSMenuSection, POSPrinter,
                                     ProductPicture)

        self.stamp = datetime.datetime.now().strftime("%H%M%S%f")
        admin = User.objects.create(username=f"hw.admin.{self.stamp}", email=f"hw.admin.{self.stamp}@fixhw.local")
        admin.set_password("fix-hw-pw")
        admin.save()
        co = self.make_company(admin, "A")
        other = self.make_company(admin, "B")
        hq = co.branches.get(is_headquarters=True)
        cashier = self.user(co, "hw.cashier", "POS_CASHIER")
        manager = self.user(co, "hw.manager", "SALES_MANAGER")
        auditor = self.user(co, "hw.auditor", "AUDITOR")
        viewer = self.user(co, "hw.viewer", "VIEWER")
        outsider = self.user(other, "hw.outsider", "POS_CASHIER")

        uom = (UnitOfMeasure.objects.filter(company=co).first()
               or UnitOfMeasure.objects.create(company=co, name="Pieces", abbreviation="PCS", is_base=True))
        wh = Warehouse.objects.get(company=co, is_default=True)
        soda = Product.objects.create(company=co, sku="SODA", name="Soda", unit_of_measure=uom,
                                      cost_price=D("40"), selling_price=D("80"),
                                      income_account=self.account(co, "4200"), cogs_account=self.account(co, "5000"),
                                      inventory_account=self.account(co, "1400"))
        StockMovement.objects.create(product=soda, warehouse=wh, movement_type="RECEIPT", quantity=D("50"),
                                     unit_cost=D("40"), movement_date=timezone.localdate(), reference="GRN")

        # ── 1 ──
        self.section("1. pictures")
        pic = hardware.save_picture(soda, SimpleUploadedFile("soda.png", png(), content_type="image/png"))
        self.expect("an upload is shrunk to a thumbnail (longest side 320)",
                    max(pic.width, pic.height) <= 320 and len(bytes(pic.data)) < 60000,
                    f"{pic.width}x{pic.height}, {len(bytes(pic.data))} bytes, {pic.content_type}")
        url = hardware.picture_url(soda)
        self.expect("its address is versioned", url.startswith(f"/pos/pictures/{soda.pk}/") and "?v=" in url, url)
        cc = self.client_for(cashier)
        r = cc.get(url)
        self.expect("the till's user gets the picture, cacheable for good",
                    r.status_code == 200 and r["Content-Type"] == pic.content_type
                    and "immutable" in r.get("Cache-Control", ""), f"{r.status_code}")
        self.expect("another company cannot read it", self.client_for(outsider).get(url).status_code == 404)
        card = pos.product_card(soda) if hasattr(pos, "product_card") else {}
        self.expect("the till's product card carries the picture", card.get("image") == url, str(card.get("image")))
        try:
            hardware.save_picture(soda, SimpleUploadedFile("x.png", b"not a picture", content_type="image/png"))
            refused = False
        except Exception as exc:          # noqa: BLE001
            refused = "not a picture" in str(exc)
        self.expect("a file that is not a picture is refused", refused)

        menu = POSMenu.objects.create(company=co, name="Main")
        sec = POSMenuSection.objects.create(menu=menu, name="Drinks", sort_order=1)
        item = POSMenuItem.objects.create(section=sec, product=soda)
        ProductPicture.objects.filter(product=soda).delete()
        mc = self.client_for(manager)
        r = mc.post(reverse("pos:bo_item_picture", args=[item.pk]),
                    {"picture": SimpleUploadedFile("s.png", png(400, 400, (0, 120, 0)), content_type="image/png")})
        self.expect("the back office uploads a menu item's picture",
                    r.status_code == 302 and ProductPicture.objects.filter(product=soda).exists(), f"{r.status_code}")
        r = self.client_for(viewer).post(reverse("pos:bo_item_picture", args=[item.pk]), {"action": "remove"})
        self.expect("a viewer cannot remove it", ProductPicture.objects.filter(product=soda).exists(), f"{r.status_code}")
        mc.post(reverse("pos:bo_item_picture", args=[item.pk]), {"action": "remove"})
        self.expect("the manager can remove it", not ProductPicture.objects.filter(product=soda).exists())

        # ── 2 ──
        self.section("2. printers")

        def form(**kw):
            data = {"name": "Front till", "branch": "", "connection": "BRIDGE", "host": "192.168.1.50",
                    "port": "9100", "bridge_url": "http://127.0.0.1:9180", "drawer_pin": "2",
                    "paper_chars": "42", "open_drawer_on_cash": "on", "print_receipts": "on", "is_active": "on"}
            data.update(kw)
            return PrinterForm(data=data, company=co)

        f = form()
        self.expect("a printer on the LAN is accepted", f.is_valid(), f.errors.as_text())
        self.expect("a public address is refused", not form(host="8.8.8.8").is_valid())
        self.expect("a link-local address is refused", not form(host="169.254.169.254").is_valid())
        self.expect("a bridge that is not on the till PC is refused",
                    not form(bridge_url="http://192.168.1.9:9180").is_valid())
        self.expect("a server printer only on a printer port", not form(connection="SERVER", port="6379").is_valid()
                    and form(connection="SERVER", port="9100").is_valid())

        # ── 3 ──
        self.section("3. ESC/POS")
        self.expect("the drawer pulse is ESC p on pin 2 / pin 5",
                    hardware.drawer_pulse(2) == b"\x1bp\x00\x19\xfa" and hardware.drawer_pulse(5) == b"\x1bp\x01\x19\xfa")
        session = pos.open_session(co, cashier, hq, D("1000"))
        sale = pos.checkout(co, cashier, {soda.pk: D("2")}, session=session, payment_method="CASH",
                            tendered=D("200"), token="hwtoken000000001")
        printer = POSPrinter.objects.create(company=co, name="Front till", connection="BRIDGE",
                                            host="192.168.1.50", port=9100, drawer_pin=2, paper_chars=42)
        raw = hardware.receipt_bytes(sale, printer, kick=True)
        self.expect("a receipt starts with ESC @, names the ticket and the item, and is cut",
                    raw.startswith(b"\x1b@") and sale.reference.encode() in raw and b"Soda" in raw
                    and b"\x1dV\x42\x03" in raw, f"{len(raw)} bytes")
        self.expect("on cash it ends with the drawer pulse", raw.endswith(hardware.drawer_pulse(2)))
        self.expect("without the kick there is no pulse",
                    hardware.drawer_pulse(2) not in hardware.receipt_bytes(sale, printer, kick=False))
        r = cc.get(reverse("pos:hw_receipt", args=[sale.pk]) + f"?printer={printer.pk}&kick=1")
        body = r.json() if r.status_code == 200 else {}
        self.expect("the till fetches the receipt bytes and text",
                    base64.b64decode(body.get("data", "")) == raw and sale.reference in body.get("text", ""),
                    f"{r.status_code}")
        r = self.client_for(outsider).get(reverse("pos:hw_receipt", args=[sale.pk]) + f"?printer={printer.pk}")
        self.expect("another company cannot print our ticket", r.status_code == 404, f"{r.status_code}")

        # ── 4 ──
        self.section("4. sending")
        for host, port, why in (("8.8.8.8", 9100, "a public address"), ("169.254.169.254", 9100, "link-local"),
                                ("192.168.1.50", 6379, "a non-printer port")):
            try:
                hardware.send_raw(host, port, b"x", timeout=1)
                ok = False
            except Exception as exc:          # noqa: BLE001
                ok = "Could not reach" not in str(exc)    # refused before any connection
            self.expect(f"the server refuses {why}", ok)
        with override_settings(DEBUG=False):
            try:
                hardware.send_raw("127.0.0.1", 9100, b"x", timeout=1)
                ok = False
            except Exception as exc:          # noqa: BLE001
                ok = "private network" in str(exc)
        self.expect("in production the server will not talk to itself (loopback)", ok)

        listener = Listener()          # a fake printer on a spare port, allowed for this check only
        srv = POSPrinter.objects.create(company=co, name="Server till", connection="SERVER",
                                        host="127.0.0.1", port=listener.port, drawer_pin=5)
        ports, hardware.PRINTER_PORTS = hardware.PRINTER_PORTS, [listener.port]
        try:
            with override_settings(DEBUG=True):
                r = self.client_for(cashier).post(reverse("pos:hw_server_send"),
                                                  {"printer": srv.pk, "action": "drawer"})
        finally:
            hardware.PRINTER_PORTS = ports
        got = listener.wait()
        self.expect("a SERVER printer gets the drawer pulse over the socket",
                    r.status_code == 200 and r.json().get("ok") and got == hardware.drawer_pulse(5),
                    f"{r.status_code} {got!r}")

        self.check_bridge()

        # ── 5 ──
        self.section("5. the drawer log")
        r = cc.post(reverse("pos:hw_log"), {"printer": printer.pk, "reason": "NO_SALE", "note": ""})
        self.expect("a no-sale opening with no reason is refused", r.status_code == 400 and not r.json().get("ok"))
        r = cc.post(reverse("pos:hw_log"), {"printer": printer.pk, "reason": "NO_SALE", "note": "Change for a customer"})
        self.expect("with a reason it is logged", r.status_code == 200 and r.json().get("ok"))
        cc.post(reverse("pos:hw_log"), {"printer": printer.pk, "reason": "SALE", "sale": sale.pk})
        ev = POSDrawerEvent.objects.filter(company=co, sale=sale).first()
        self.expect("a sale opening is logged against the ticket, the till and the branch",
                    ev and ev.session_id == session.pk and ev.branch_id == hq.pk and ev.user_id == cashier.pk)
        r = cc.post(reverse("pos:hw_log"), {"printer": printer.pk, "reason": "SALE", "sale": sale.pk,
                                            "ok": "0", "error": "Printer offline"})
        self.expect("a failed opening is logged as failed",
                    POSDrawerEvent.objects.filter(company=co, ok=False, error="Printer offline").exists())
        rep = restaurant.day_report(co, hq, timezone.localdate())
        self.expect("the day end lists every opening and counts the no-sales",
                    len(rep["drawer"]) == 3 and rep["no_sale"] == 1
                    and any(d["ticket"] == sale.reference for d in rep["drawer"]),
                    f"{len(rep['drawer'])} openings, {rep['no_sale']} no-sale")
        r = mc.get(reverse("pos:bo_day_end"))
        self.expect("the day end page shows the drawer openings",
                    r.status_code == 200 and b"Cash drawer openings" in r.content and b"Change for a customer" in r.content,
                    f"{r.status_code}")

        # ── 6 ──
        self.section("6. roles")
        r = cc.get(reverse("pos:hw_config"))
        ids = [p["id"] for p in r.json().get("printers", [])] if r.status_code == 200 else []
        self.expect("the cashier's till gets the company's printers with the drawer pulse",
                    str(printer.pk) in ids and all(p.get("drawer_b64") for p in r.json()["printers"]))
        self.expect("a viewer cannot read the printers", self.client_for(viewer).get(reverse("pos:hw_config")).status_code in (302, 403))
        ac = self.client_for(auditor)
        self.expect("an auditor can read the printers", ac.get(reverse("pos:hw_config")).status_code == 200)
        r = ac.post(reverse("pos:hw_log"), {"printer": printer.pk, "reason": "NO_SALE", "note": "peek"})
        self.expect("…but cannot open a drawer", r.status_code in (302, 403)
                    and not POSDrawerEvent.objects.filter(note="peek").exists(), f"{r.status_code}")
        r = self.client_for(outsider).get(reverse("pos:hw_config"))
        self.expect("another company's till does not see our printers",
                    r.status_code == 200 and str(printer.pk) not in [p["id"] for p in r.json().get("printers", [])])
        r = cc.get(reverse("pos:sale_detail", args=[sale.pk]) + "?rung=1")
        self.expect("the ticket page loads the printer script", r.status_code == 200 and b"pos/hardware.js" in r.content)
        r = cc.get(reverse("pos:terminal"))
        self.expect("the till has the Open Drawer key", r.status_code == 200 and b'data-fn="drawer"' in r.content)

    def check_bridge(self):
        """The bridge program, run in a thread, writes the bytes to a LAN socket."""
        import importlib.util
        from http.server import ThreadingHTTPServer
        from pathlib import Path
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        from django.conf import settings

        path = Path(settings.BASE_DIR) / "tools" / "mofisi-bridge" / "mofisi_bridge.py"
        spec = importlib.util.spec_from_file_location("mofisi_bridge", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.Bridge.allow_local, mod.Bridge.origins, mod.Bridge.token = True, ["https://shop.example"], ""
        server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Bridge)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def post(body, origin="https://shop.example"):
            req = Request(base + "/send", data=json.dumps(body).encode(), method="POST",
                          headers={"Content-Type": "application/json", "Origin": origin})
            try:
                with urlopen(req, timeout=8) as resp:
                    return resp.status, json.loads(resp.read())
            except HTTPError as exc:
                return exc.code, json.loads(exc.read() or b"{}")

        try:
            with urlopen(base + "/health", timeout=5) as resp:
                self.expect("the bridge answers its health check", json.loads(resp.read()).get("ok"))
            listener = Listener()
            pulse = b"\x1bp\x00\x19\xfa"
            code, res = post({"host": "127.0.0.1", "port": listener.port, "data": base64.b64encode(pulse).decode()})
            self.expect("the bridge writes the bytes to the printer's socket",
                        code == 200 and res.get("ok") and listener.wait() == pulse, f"{code} {res}")
            code, res = post({"host": "8.8.8.8", "port": 9100, "data": base64.b64encode(pulse).decode()})
            self.expect("the bridge refuses an internet address", code == 400 and not res.get("ok"))
            code, res = post({"host": "127.0.0.1", "port": 9100, "data": "eA=="}, origin="https://evil.example")
            self.expect("the bridge refuses a site it was not told to trust", code == 403)
        finally:
            server.shutdown()
            server.server_close()
