"""Checks for tags and labels (apps/tagging).

    python manage.py test_fix_tagging      # rolls back

    1. numbering   -> two batches on one prefix never share a number; a start
                      number already issued is refused; another prefix counts
                      on its own; prefixes and padding make the codes
    2. QR content  -> the QR decodes to the tag's code and details, never a
                      URL to the site (OpenCV or pyzbar decode it when
                      installed, otherwise the encoded payload is checked);
                      Code128 symbols are well formed
    3. print pages -> every label size renders with its exact @page size and
                      grid; reprints keep the same codes and number nothing
                      new; void tags are left out; ZPL comes out
    4. lookup      -> a scanner typing the code (or the whole QR text) opens
                      the tag; voiding one tag and a range
    5. companies   -> another company cannot see, find, print or void them
    6. roles       -> who may issue, print, void and look up
"""
import datetime
import re

from django.core.management.base import BaseCommand
from django.db import transaction
from django.test import Client
from django.test.utils import setup_test_environment, teardown_test_environment
from django.urls import reverse


class Command(BaseCommand):
    help = "Checks for tags and labels."

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

    def client_for(self, u):
        c = Client(raise_request_exception=False)
        c.force_login(u, backend="django.contrib.auth.backends.ModelBackend")
        return c

    def user(self, name, role, company=None):
        from apps.accounts.models import User
        from apps.organizations.models import CompanyMembership
        company = company or self.co
        u = User.objects.create(username=f"{name}.{self.stamp}", email=f"{name}.{self.stamp}@fixtag.local")
        u.set_password("fix-tag-pw")
        u.save()
        CompanyMembership.objects.create(company=company, user=u, role=role,
                                         branch=company.branches.get(is_headquarters=True), is_default=True)
        return u

    def company(self, client, name):
        from apps.organizations.models import Company
        client.post(reverse("organizations:company_create"), {
            "name": name, "legal_name": name, "registration_number": f"PVT-{name[-6:]}", "tax_id": "",
            "vat_number": "", "currency": "KES", "timezone": "Africa/Nairobi", "fiscal_year_start_month": "1",
            "industry": "Trading", "phone": "+254 700 000 008", "email": f"i@{self.stamp}.fixtag.local",
            "website": "", "address_line1": "8 Tom Mboya St", "address_line2": "", "city": "Nairobi",
            "state_province": "Nairobi", "postal_code": "00100", "country": "Kenya"})
        return Company.objects.get(name=name)

    def batch_post(self, client, **over):
        data = {"title": "Office laptops", "purpose": "FREE", "subject_text": "Property of HQ", "prefix": "TAG-",
                "padding": "5", "start": "", "quantity": "10", "label_size": "50x25", "show_logo": "on",
                "show_company": "on", "show_code": "on", "code_type": "QR", "qr_content": "DETAILS",
                "line1": "Call 0700 000 000 if found", "line2": ""}
        data.update({k: v for k, v in over.items() if v is not None})
        for k in [k for k, v in over.items() if v is None]:
            data.pop(k, None)
        return client.post(reverse("tagging:batch_create"), data)

    def decode_qr(self, text):
        """Decode the QR made for `text`: (decoded text, how)."""
        from apps.tagging import render as r
        img = r.qr_png(text)
        try:
            from pyzbar.pyzbar import decode
            found = decode(img)
            if found:
                return found[0].data.decode("utf-8"), "pyzbar"
        except ImportError:
            pass
        try:
            import cv2
            import numpy as np
            got = cv2.QRCodeDetector().detectAndDecode(np.array(img.convert("L")))[0]
            if got:
                return got, "opencv"
        except ImportError:
            pass
        return None, "none"

    def run(self):
        from apps.accounts.models import User
        from apps.core.models import SequenceCounter
        from apps.inventory.models import Product, UnitOfMeasure
        from apps.tagging import render as r
        from apps.tagging import services
        from apps.tagging.models import SIZES, Tag, TagBatch

        self.stamp = datetime.datetime.now().strftime("%H%M%S%f")
        admin = User.objects.create(username=f"tag.admin.{self.stamp}", email=f"tag.admin.{self.stamp}@fixtag.local")
        admin.set_password("fix-tag-pw")
        admin.save()
        self.ac = self.client_for(admin)
        self.co = self.company(self.ac, f"Fix Tag {self.stamp}")

        # ── 1 ──
        self.section("1. numbering")
        r1 = self.batch_post(self.ac, quantity="10")
        b1 = TagBatch.objects.filter(company=self.co).first()
        self.expect("a batch issues its tags", r1.status_code == 302 and b1 and b1.tags.count() == 10,
                    f"{r1.status_code}")
        self.expect("codes run from the prefix and start", b1 and b1.first_code == "TAG-00001"
                    and b1.last_code == "TAG-00010", b1 and f"{b1.first_code}..{b1.last_code}")
        r2 = self.batch_post(self.ac, quantity="5", title="Second")
        b2 = TagBatch.objects.filter(company=self.co, title="Second").first()
        self.expect("the second batch carries on where the first stopped",
                    b2 and b2.first_code == "TAG-00011" and b2.last_code == "TAG-00015")
        codes = list(Tag.objects.filter(company=self.co).values_list("code", flat=True))
        self.expect("no number repeats across the two batches", len(codes) == len(set(codes)) == 15)
        self.expect("batches are numbered too", b1.number != b2.number and b1.number.startswith("TB-"))
        r3 = self.batch_post(self.ac, quantity="3", start="12", title="Reuse")
        self.expect("a start number already issued is refused", r3.status_code == 200
                    and b"has been issued already" in r3.content
                    and not TagBatch.objects.filter(company=self.co, title="Reuse").exists())
        r4 = self.batch_post(self.ac, quantity="3", start="100", title="Jump")
        b4 = TagBatch.objects.filter(company=self.co, title="Jump").first()
        self.expect("a later start number is allowed", b4 and b4.first_code == "TAG-00100")
        r5 = self.batch_post(self.ac, quantity="2", title="After jump")
        b5 = TagBatch.objects.filter(company=self.co, title="After jump").first()
        self.expect("and the next batch follows it", b5 and b5.first_code == "TAG-00103")
        try:
            services.issue_batch(TagBatch(company=self.co, prefix="TAG-", quantity=2, padding=5), start=50)
            refused = False
        except Exception:     # noqa: BLE001
            refused = True
        self.expect("the service itself refuses a used number", refused)
        self.batch_post(self.ac, quantity="4", prefix="ast/", padding="3", title="Assets")
        b6 = TagBatch.objects.filter(company=self.co, title="Assets").first()
        self.expect("another prefix counts on its own (and is upper-cased)",
                    b6 and b6.first_code == "AST/001" and b6.last_code == "AST/004")
        self.expect("the counter is claimed per company and prefix",
                    SequenceCounter.objects.get(company=self.co, prefix=services.counter_key("TAG-")).last_number == 104)
        bad = self.batch_post(self.ac, prefix="TAG <x>", title="Bad prefix")
        self.expect("a prefix with odd characters is refused", bad.status_code == 200
                    and not TagBatch.objects.filter(company=self.co, title="Bad prefix").exists())
        self.batch_post(self.ac, title="Bars", code_type="BARCODE", quantity="2", prefix="BC-")
        bars = TagBatch.objects.filter(company=self.co, title="Bars").first()
        self.expect("a batch is either QR or barcode", bars and bars.show_barcode and not bars.show_qr
                    and b1.show_qr and not b1.show_barcode)
        html = self.ac.get(reverse("tagging:batch_print", args=[bars.pk])).content.decode()
        self.expect("barcode tags print bars and no QR", html.count('class="bc"') == 2 and 'class="qr"' not in html)
        html = self.ac.get(reverse("tagging:batch_print", args=[b1.pk]), {"preview": 1}).content.decode()
        self.expect("QR tags print a QR and no bars", 'class="qr"' in html and 'class="bc"' not in html)
        odd = self.batch_post(self.ac, title="Odd type", code_type="BOTH")
        self.expect("there is no 'both' type", odd.status_code == 200
                    and not TagBatch.objects.filter(company=self.co, title="Odd type").exists())

        uom = UnitOfMeasure.objects.filter(company=self.co).first() or UnitOfMeasure.objects.create(
            company=self.co, name="Piece", abbreviation="pc")
        prod = Product.objects.create(company=self.co, sku="SKU-77", name="Steel shelf", unit_of_measure=uom,
                                      selling_price=4500)
        self.batch_post(self.ac, purpose="PRODUCT", product=str(prod.pk), quantity="2", prefix="SH-",
                        title="Shelves", show_price="on", code_type="BARCODE", subject_text="")
        b7 = TagBatch.objects.filter(company=self.co, title="Shelves").first()
        self.expect("product tags describe the product", b7 and b7.subject_text == "SKU-77 Steel shelf")
        noprod = self.batch_post(self.ac, purpose="PRODUCT", title="No product")
        self.expect("product tags need the product", noprod.status_code == 200
                    and not TagBatch.objects.filter(company=self.co, title="No product").exists())

        # ── 2 ──
        self.section("2. QR content")
        t1 = b1.tags.get(number=1)
        pl = r.payload(t1)
        self.expect("the payload starts with the code and carries the details",
                    pl.startswith("TAG-00001 | ") and self.co.name in pl and "Property of HQ" in pl, pl)
        self.expect("the payload holds no URL", "http" not in pl.lower() and "/tagging" not in pl and "://" not in pl)
        got, how = self.decode_qr(pl)
        if how == "none":
            import qrcode
            q = qrcode.QRCode()
            q.add_data(pl)
            q.make(fit=True)
            self.expect("the QR encodes the payload (no decoder installed)",
                        q.data_list and q.data_list[0].data.decode("utf-8") == pl)
        else:
            self.expect(f"the QR decodes to the code and details ({how})", got == pl, str(got))
            self.expect("and the code read off it finds the tag", r.code_from_scan(got) == t1.code)
        b1.qr_content = "CODE"
        self.expect("'code only' QR holds just the code", r.payload(t1, b1) == "TAG-00001")
        b1.qr_content = "DETAILS"
        svg = r.qr_svg(pl)
        self.expect("the QR is an inline SVG on white", svg.startswith("<svg") and 'fill="#ffffff"' in svg)
        page = self.ac.get(reverse("tagging:tag_detail", args=[t1.pk])).content.decode()
        self.expect("no page on the module puts the site's address in a QR",
                    "QR holds" in page and not re.search(r"QR holds</dt><dd[^>]*>\s*https?://", page))
        widths_ok = all(sum(map(int, p)) == 11 for p in r._C128[:106]) and sum(map(int, r._C128[106])) == 13
        self.expect("Code128 symbols are 11 modules (stop 13) and all different",
                    widths_ok and len(set(r._C128)) == 107)
        vals = r.code128_values("TAG-00001")
        self.expect("Code128 starts in set B and switches to C for the digits",
                    vals[0] == r.START_B and r.CODE_C in vals
                    and vals[vals.index(r.CODE_C) + 1:vals.index(r.CODE_C) + 3] == [0, 0], str(vals))
        check = (vals[0] + sum(v * i for i, v in enumerate(vals[1:-1], 1))) % 103
        self.expect("and ends with the right check symbol", vals[-1] == check)
        mods = r.code128_modules("TAG-00001")
        self.expect("the bars end with the stop pattern", mods.endswith("1100011101011"))

        # ── 3 ──
        self.section("3. print pages")
        before = Tag.objects.filter(company=self.co).count()
        counter_before = SequenceCounter.objects.get(company=self.co, prefix=services.counter_key("TAG-")).last_number
        for key, spec in SIZES.items():
            resp = self.ac.get(reverse("tagging:batch_print", args=[b1.pk]), {"size": key})
            html = resp.content.decode()
            ok = (resp.status_code == 200 and f"size: {spec[1]}mm {spec[2]}mm" in html
                  and f"repeat({spec[3]}, {spec[5]}mm)" in html and html.count('class="tg"') == 10)
            self.expect(f"{spec[0]} renders", ok, f"{resp.status_code}")
        printed_before = b1.tags.get(number=3).print_count
        resp = self.ac.get(reverse("tagging:batch_print", args=[b1.pk]), {"from": 3, "to": 4})
        html = resp.content.decode()
        self.expect("a range reprint prints just those codes", html.count('class="tg"') == 2
                    and "TAG-00003" in html and "TAG-00004" in html and "TAG-00005" not in html)
        self.expect("reprinting makes no new tags and moves no counter",
                    Tag.objects.filter(company=self.co).count() == before
                    and SequenceCounter.objects.get(company=self.co, prefix=services.counter_key("TAG-")).last_number
                    == counter_before)
        self.expect("reprints are counted on the tag", b1.tags.get(number=3).print_count == printed_before + 1)
        resp = self.ac.get(reverse("tagging:batch_print", args=[b1.pk]), {"from": 1, "to": 1, "copies": 3})
        self.expect("copies repeat the same code", resp.content.decode().count('class="tg"') == 3)
        resp = self.ac.get(reverse("tagging:batch_print", args=[b1.pk]), {"size": "A4_3x8", "skip": 5})
        self.expect("skip leaves used labels on a part-used sheet", resp.content.decode().count('<div class="cell"></div>') == 5)
        resp = self.ac.get(reverse("tagging:batch_print", args=[b7.pk]))
        html = resp.content.decode()
        self.expect("product labels carry the price and a barcode", "KES 4,500.00" in html and 'class="bc"' in html)
        z = self.ac.get(reverse("tagging:batch_zpl", args=[b1.pk]), {"from": 1, "to": 2})
        body = z.content.decode()
        self.expect("ZPL comes out for a label printer", z.status_code == 200 and body.count("^XA") == 2
                    and "^FDMA,TAG-00001 | " in body and "attachment" in z["Content-Disposition"])

        # ── 4 ──
        self.section("4. lookup and void")
        resp = self.ac.get(reverse("tagging:lookup"), {"q": "TAG-00002"})
        t2 = b1.tags.get(number=2)
        self.expect("a typed code opens the tag", resp.status_code == 302
                    and resp["Location"] == reverse("tagging:tag_detail", args=[t2.pk]))
        resp = self.ac.get(reverse("tagging:lookup"), {"q": r.payload(t2)})
        self.expect("so does the whole QR text a scanner types", resp.status_code == 302
                    and resp["Location"] == reverse("tagging:tag_detail", args=[t2.pk]))
        resp = self.ac.get(reverse("tagging:lookup"), {"q": "tag-00002"})
        self.expect("case does not matter", resp.status_code == 302)
        resp = self.ac.get(reverse("tagging:lookup"), {"q": "0000"})
        self.expect("a part of a code lists the matches", resp.status_code == 200 and b"TAG-00001" in resp.content)
        resp = self.ac.post(reverse("tagging:batch_void", args=[b1.pk]), {"from": 2, "to": 2, "reason": "Lost"})
        t2.refresh_from_db()
        self.expect("one tag is voided with its reason", t2.status == "VOID" and t2.void_reason == "Lost")
        self.ac.post(reverse("tagging:batch_void", args=[b1.pk]), {"from": 5, "to": 7, "reason": "Damaged"})
        self.expect("a range is voided", b1.tags.filter(status="VOID").count() == 4)
        self.ac.post(reverse("tagging:batch_void", args=[b1.pk]), {"from": 8, "to": 8, "reason": ""})
        self.expect("voiding needs a reason", b1.tags.get(number=8).status == "ACTIVE")
        html = self.ac.get(reverse("tagging:batch_print", args=[b1.pk])).content.decode()
        self.expect("void tags are never printed", html.count('class="tg"') == 6 and "TAG-00002" not in html)
        self.batch_post(self.ac, quantity="1", title="After void")
        b8 = TagBatch.objects.filter(company=self.co, title="After void").first()
        self.expect("a voided number is not issued again", b8 and b8.first_code == "TAG-00105")
        self.expect("the tag page says it is void", b"Void" in self.ac.get(
            reverse("tagging:tag_detail", args=[t2.pk])).content)

        # names the client gives each tag
        self.batch_post(self.ac, quantity="3", prefix="LAP-", title="Named",
                        names="Reception laptop\n\nBoardroom projector")
        named = TagBatch.objects.filter(company=self.co, title="Named").first()
        n1, n2, n3 = named.tags.order_by("number")
        self.expect("names typed at issue go to the tags in order (a blank line skips one)",
                    (n1.name, n2.name, n3.name) == ("Reception laptop", "", "Boardroom projector"))
        too_many = self.batch_post(self.ac, quantity="1", prefix="LAP-", title="Too many names", names="A\nB")
        self.expect("more names than tags is refused", too_many.status_code == 200
                    and not TagBatch.objects.filter(company=self.co, title="Too many names").exists())
        pl = r.payload(n1)
        self.expect("the QR carries the tag's name right after the code",
                    pl.startswith("LAP-00001 | Reception laptop | "), pl)
        got, how = self.decode_qr(pl)
        if how != "none":
            self.expect(f"and it decodes back with the name ({how})", got == pl)
        html = self.ac.get(reverse("tagging:batch_print", args=[named.pk]), {"preview": 1}).content.decode()
        self.expect("the name prints on the label", html.count('class="tg-name"') == 2 and "Boardroom projector" in html)
        self.ac.post(reverse("tagging:tag_rename", args=[n2.pk]), {"name": "Finance laptop"})
        n2.refresh_from_db()
        self.expect("a tag can be renamed", n2.name == "Finance laptop" and "Finance laptop" in r.payload(n2))
        self.ac.post(reverse("tagging:batch_names", args=[named.pk]), {"from": n2.number, "names": "Desk 2\nDesk 3"})
        self.expect("several tags can be named at once from a number",
                    list(named.tags.order_by("number").values_list("name", flat=True))
                    == ["Reception laptop", "Desk 2", "Desk 3"])
        self.expect("renaming keeps the code and makes no new numbers",
                    named.tags.count() == 3 and named.tags.get(number=2).code == "LAP-00002")
        resp = self.ac.get(reverse("tagging:lookup"), {"q": "reception laptop"})
        self.expect("typing a tag's name finds it", resp.status_code == 302
                    and resp["Location"] == reverse("tagging:tag_detail", args=[n1.pk]))
        resp = self.ac.get(reverse("tagging:lookup"), {"q": "Desk"})
        self.expect("part of a name lists the matches", resp.status_code == 200
                    and b"Desk 2" in resp.content and b"Desk 3" in resp.content)
        z = self.ac.get(reverse("tagging:batch_zpl", args=[named.pk]), {"from": 1, "to": 1}).content.decode()
        self.expect("ZPL carries the name too", "Reception laptop" in z)

        # ── 5 ──
        self.section("5. companies")
        admin2 = User.objects.create(username=f"tag.admin2.{self.stamp}", email=f"tag.admin2.{self.stamp}@fixtag.local")
        admin2.set_password("fix-tag-pw")
        admin2.save()
        other = self.client_for(admin2)
        co2 = self.company(other, f"Other Tag {self.stamp}")
        self.expect("another company cannot open the batch",
                    other.get(reverse("tagging:batch_detail", args=[b1.pk])).status_code == 404)
        self.expect("nor print it", other.get(reverse("tagging:batch_print", args=[b1.pk])).status_code == 404)
        self.expect("nor download its ZPL", other.get(reverse("tagging:batch_zpl", args=[b1.pk])).status_code == 404)
        self.expect("nor open a tag", other.get(reverse("tagging:tag_detail", args=[t1.pk])).status_code == 404)
        resp = other.get(reverse("tagging:lookup"), {"q": "TAG-00001"})
        self.expect("nor find it by its code", resp.status_code == 200 and b"No tag TAG-00001" in resp.content
                    and reverse("tagging:tag_detail", args=[t1.pk]).encode() not in resp.content)
        resp = other.post(reverse("tagging:batch_void", args=[b1.pk]), {"from": 1, "to": 1, "reason": "x"})
        self.expect("nor void it", resp.status_code == 404 and b1.tags.get(number=1).status == "ACTIVE")
        resp = other.post(reverse("tagging:tag_rename", args=[n1.pk]), {"name": "Mine now"})
        self.expect("nor rename one", resp.status_code == 404 and Tag.objects.get(pk=n1.pk).name == "Reception laptop")
        resp = other.post(reverse("tagging:batch_names", args=[named.pk]), {"from": 1, "names": "Mine"})
        self.expect("nor name them in bulk", resp.status_code == 404 and Tag.objects.get(pk=n1.pk).name == "Reception laptop")
        resp = other.get(reverse("tagging:lookup"), {"q": "Reception laptop"})
        self.expect("nor find one by its name", resp.status_code == 200 and b"Nothing in this company matches" in resp.content
                    and reverse("tagging:tag_detail", args=[n1.pk]).encode() not in resp.content)
        self.expect("its home page lists none of them", reverse("tagging:batch_detail", args=[b1.pk]).encode()
                    not in other.get(reverse("tagging:home")).content)
        self.batch_post(other, quantity="2", title="Theirs")
        theirs = TagBatch.objects.filter(company=co2, title="Theirs").first()
        self.expect("and its own numbers start at 1 (counters are per company)",
                    theirs and theirs.first_code == "TAG-00001")

        # ── 6 ──
        self.section("6. roles")
        clerk = self.client_for(self.user("tag.stores", "WAREHOUSE_MANAGER"))
        buyer = self.client_for(self.user("tag.buyer", "PROCUREMENT"))
        acct = self.client_for(self.user("tag.acct", "ACCOUNTANT"))
        viewer = self.client_for(self.user("tag.viewer", "VIEWER"))
        cashier = self.client_for(self.user("tag.cashier", "POS_CASHIER"))
        emp = self.client_for(self.user("tag.emp", "EMPLOYEE"))
        r_ = self.batch_post(clerk, quantity="1", title="By stores")
        self.expect("the stores may issue tags", r_.status_code == 302)
        r_ = self.batch_post(acct, quantity="1", title="By finance")
        self.expect("finance may issue tags", r_.status_code == 302)
        r_ = self.batch_post(viewer, quantity="1", title="By viewer")
        self.expect("a viewer may not", r_.status_code == 403
                    and not TagBatch.objects.filter(title="By viewer").exists())
        self.expect("a viewer may print (it is reading)",
                    viewer.get(reverse("tagging:batch_print", args=[b1.pk])).status_code == 200)
        self.expect("a cashier cannot open the module",
                    cashier.get(reverse("tagging:home")).status_code == 403)
        self.expect("a cashier cannot print",
                    cashier.get(reverse("tagging:batch_print", args=[b1.pk])).status_code == 403)
        self.expect("but anyone may look a scanned tag up", cashier.get(reverse("tagging:lookup"), {"q": "TAG-00001"})
                    .status_code == 302 and emp.get(reverse("tagging:tag_detail", args=[t1.pk])).status_code == 200)
        r_ = viewer.post(reverse("tagging:tag_rename", args=[n1.pk]), {"name": "Viewer's"})
        self.expect("a viewer may not rename a tag", r_.status_code == 403 and Tag.objects.get(pk=n1.pk).name == "Reception laptop")
        r_ = clerk.post(reverse("tagging:tag_rename", args=[n1.pk]), {"name": "Front desk laptop"})
        self.expect("the stores may", r_.status_code == 302 and Tag.objects.get(pk=n1.pk).name == "Front desk laptop")
        r_ = buyer.post(reverse("tagging:batch_void", args=[b1.pk]), {"from": 9, "to": 9, "reason": "x"})
        self.expect("a buyer may not void", r_.status_code == 403 and b1.tags.get(number=9).status == "ACTIVE")
        r_ = clerk.post(reverse("tagging:batch_void", args=[b1.pk]), {"from": 9, "to": 9, "reason": "Torn"})
        self.expect("the warehouse manager may", r_.status_code == 302 and b1.tags.get(number=9).status == "VOID")
        self.expect("the sidebar shows the module to the stores",
                    b'href="/tagging/"' in clerk.get(reverse("tagging:home")).content)
        self.expect("and hides it from a cashier",
                    b'href="/tagging/"' not in cashier.get(reverse("tagging:lookup")).content)
        for name, args in [("home", []), ("batch_create", []), ("batch_detail", [b7.pk]), ("tag_detail", [t1.pk]),
                           ("lookup", [])]:
            resp = self.ac.get(reverse(f"tagging:{name}", args=args))
            self.expect(f"{name} renders", resp.status_code in (200, 302), str(resp.status_code))
