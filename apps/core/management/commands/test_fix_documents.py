"""Checks for printed documents (apps/organizations/documents.py).

    python manage.py test_fix_documents      # rolls back

    1. every document prints  -> quotation, sales order, invoice, credit note and
                                 purchase order open in the A4 layout with their
                                 number, lines and a totals box that adds up
    2. terms only where due   -> a terms page for quotation, order, invoice and PO
                                 when terms are set; never on a credit note; none
                                 when the box is left empty
    3. bank details           -> on invoices only
    4. logo                   -> uploaded, kept in the database, served to the
                                 company only; a file that is not a picture refused
    5. roles                  -> a viewer can print; another company cannot; only
                                 the admin changes the layout
"""
import datetime
import io
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand
from django.db import transaction
from django.test import Client
from django.test.utils import setup_test_environment, teardown_test_environment
from django.urls import reverse
from django.utils import timezone

D = Decimal


class Command(BaseCommand):
    help = "Checks for printed documents."

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

    def make_company(self, admin, tag):
        from apps.organizations.models import Company
        name = f"Fix Docs {tag} {self.stamp}"
        self.client_for(admin).post(reverse("organizations:company_create"), {
            "name": name, "legal_name": name, "registration_number": f"PVT-{tag}{self.stamp}", "tax_id": "P000111222Z",
            "vat_number": "", "currency": "KES", "timezone": "Africa/Nairobi", "fiscal_year_start_month": "1",
            "industry": "Trading", "phone": "+254 700 000 006", "email": f"i@{tag}{self.stamp}.fixdoc.local",
            "website": "", "address_line1": "6 Moi Avenue", "address_line2": "", "city": "Nairobi",
            "state_province": "Nairobi", "postal_code": "00100", "country": "Kenya"})
        return Company.objects.get(name=name)

    def user(self, company, name, role):
        from apps.accounts.models import User
        from apps.organizations.models import CompanyMembership
        u = User.objects.create(username=f"{name}.{self.stamp}", email=f"{name}.{self.stamp}@fixdoc.local")
        u.set_password("fix-doc-pw")
        u.save()
        CompanyMembership.objects.create(company=company, user=u, role=role,
                                         branch=company.branches.get(is_headquarters=True), is_default=True)
        return u

    def client_for(self, u):
        c = Client(raise_request_exception=False)
        c.force_login(u, backend="django.contrib.auth.backends.ModelBackend")
        return c

    def run(self):
        from PIL import Image
        from apps.accounting.models import Account, TaxRate
        from apps.accounts.models import User
        from apps.crm.models import Customer, Supplier
        from apps.inventory.models import Product, UnitOfMeasure
        from apps.organizations.documents import build, layout_for
        from apps.procurement.models import PurchaseOrder, PurchaseOrderLine
        from apps.sales.models import (Invoice, InvoiceLine, SalesOrder, SalesOrderLine, SalesQuote,
                                       SalesQuoteLine)

        self.stamp = datetime.datetime.now().strftime("%H%M%S%f")
        admin = User.objects.create(username=f"doc.admin.{self.stamp}", email=f"doc.admin.{self.stamp}@fixdoc.local")
        admin.set_password("fix-doc-pw")
        admin.save()
        co = self.make_company(admin, "A")
        other = self.make_company(admin, "B")
        viewer = self.user(co, "doc.viewer", "VIEWER")
        manager = self.user(co, "doc.fm", "FINANCE_MANAGER")
        outsider = self.user(other, "doc.outsider", "ADMIN")
        ac = self.client_for(admin)

        acct = lambda code: Account.objects.get(company=co, code=code)   # noqa: E731
        uom = (UnitOfMeasure.objects.filter(company=co).first()
               or UnitOfMeasure.objects.create(company=co, name="Each", abbreviation="EA", is_base=True))
        vat = TaxRate.objects.filter(company=co, code="VAT16").first() or TaxRate.objects.filter(company=co).first()
        panel = Product.objects.create(company=co, sku="PNL-720", name="720W solar panel", unit_of_measure=uom,
                                       cost_price=D("9000"), selling_price=D("14000"),
                                       income_account=acct("4200"), cogs_account=acct("5000"),
                                       inventory_account=acct("1400"))
        cust = Customer.objects.create(company=co, name="Thomas Sabna", tax_id="P052140103Y",
                                       address_line1="Ruiru", city="Kiambu")
        supp = Supplier.objects.create(company=co, name="Solar Imports Ltd")
        today = timezone.localdate()

        q = SalesQuote.objects.create(company=co, reference=f"QU-{self.stamp[-6:]}", customer=cust, quote_date=today,
                                      valid_until=today + datetime.timedelta(days=7), discount_percent=D("5"))
        SalesQuoteLine.objects.create(quote=q, product=panel, description="720W bifacial panel", quantity=D("10"),
                                      unit_price=D("14000"), discount_percent=D("2"), tax_rate=vat)
        q.calculate_totals()
        q.save()
        o = SalesOrder.objects.create(company=co, reference=f"SO-{self.stamp[-6:]}", customer=cust, order_date=today)
        SalesOrderLine.objects.create(order=o, product=panel, quantity=D("2"), unit_price=D("14000"), tax_rate=vat)
        o.calculate_totals()
        o.save()
        inv = Invoice.objects.create(company=co, reference=f"INV-{self.stamp[-6:]}", customer=cust, invoice_date=today,
                                     due_date=today + datetime.timedelta(days=30), subtotal=D("28000"),
                                     tax_amount=D("4480"), total_amount=D("32480"))
        InvoiceLine.objects.create(invoice=inv, product=panel, description="720W bifacial panel", quantity=D("2"),
                                   unit_price=D("14000"), tax_rate=vat)
        cn = Invoice.objects.create(company=co, reference=f"CN-{self.stamp[-6:]}", customer=cust, invoice_date=today,
                                    due_date=today, original_invoice=inv, subtotal=D("14000"), tax_amount=D("2240"),
                                    total_amount=D("16240"))
        InvoiceLine.objects.create(invoice=cn, product=panel, description="Return", quantity=D("1"),
                                   unit_price=D("14000"), tax_rate=vat)
        po = PurchaseOrder.objects.create(company=co, reference=f"PO-{self.stamp[-6:]}", supplier=supp, order_date=today,
                                          subtotal=D("90000"), tax_amount=D("14400"), total_amount=D("104400"))
        PurchaseOrderLine.objects.create(order=po, product=panel, quantity=D("10"), unit_cost=D("9000"), tax_rate=vat)

        docs = [("quotation", reverse("sales:quote_print", args=[q.pk]), q, "quote"),
                ("sales order", reverse("sales:order_print", args=[o.pk]), o, "order"),
                ("invoice", reverse("sales:invoice_print", args=[inv.pk]), inv, "invoice"),
                ("credit note", reverse("sales:invoice_print", args=[cn.pk]), cn, "credit_note"),
                ("purchase order", reverse("procurement:po_print", args=[po.pk]), po, "po")]

        self.section("1. every document prints")
        titles = {"quote": b"Quotation", "order": b"Sales Order", "invoice": b"Tax Invoice",
                  "credit_note": b"Credit Note", "po": b"Purchase Order"}
        for label, url, doc, kind in docs:
            r = ac.get(url)
            self.expect(f"the {label} prints with its title, number and line",
                        r.status_code == 200 and titles[kind] in r.content and doc.reference.encode() in r.content
                        and b"PNL-720" in r.content, f"{r.status_code}")
            ctx = build(kind, doc)
            self.expect(f"…and its totals box adds up ({label})",
                        ctx["subtotal"] - ctx["discount"] + ctx["tax"] == ctx["total"],
                        f"{ctx['subtotal']} - {ctx['discount']} + {ctx['tax']} = {ctx['total']}")
        ctx = build("quote", q)
        self.expect("a quotation's line and document discounts show as the discount",
                    # 140,000 gross, 2% off the line (2,800), 5% off the quote (6,860)
                    ctx["subtotal"] == D("140000.00") and ctx["discount"] == D("9660.00"),
                    f"gross {ctx['subtotal']}, discount {ctx['discount']}, total {q.total_amount}")
        self.expect("the customer's PIN and the PO's deliver-to box print",
                    b"P052140103Y" in ac.get(docs[2][1]).content and b"Deliver to" in ac.get(docs[4][1]).content)

        self.section("2. terms only where due")
        for label, url, doc, kind in docs:
            self.expect(f"no terms page on the {label} while no terms are set",
                        b"Terms and Conditions" not in ac.get(url).content)
        lay = layout_for(co)
        lay.terms_quote = "Quote valid for 7 days."
        lay.terms_order = "Orders ship when paid."
        lay.terms_invoice = "Payment due by the due date."
        lay.terms_po = "Deliver to the warehouse, 8am-5pm."
        lay.bank_details = "KCB Moi Avenue, account 1234567890"
        lay.save()
        for label, url, doc, kind in docs:
            body = ac.get(url).content
            want = kind != "credit_note"
            self.expect(f"{'a' if want else 'no'} terms page on the {label}",
                        (b"Terms and Conditions" in body) == want)
        body = ac.get(docs[0][1]).content
        self.expect("each document prints its own terms", b"Quote valid for 7 days." in body
                    and b"Payment due by the due date." not in body)

        self.section("3. bank details")
        for label, url, doc, kind in docs:
            body = ac.get(url).content
            self.expect(f"bank details {'on' if kind == 'invoice' else 'not on'} the {label}",
                        (b"KCB Moi Avenue" in body) == (kind == "invoice"))

        self.section("4. logo")
        buf = io.BytesIO()
        Image.new("RGBA", (1800, 600), (31, 78, 154, 255)).save(buf, "PNG")
        r = ac.post(reverse("organizations:document_layout"), {
            "tagline": "Sufficiently superior", "banner": "General Hardware", "accent": "#1F4E9A",
            "show_ship_to": "on", "terms_quote": lay.terms_quote, "terms_order": lay.terms_order,
            "terms_invoice": lay.terms_invoice, "terms_po": lay.terms_po, "bank_details": lay.bank_details,
            "footer": "Thank you", "logo_file": SimpleUploadedFile("logo.png", buf.getvalue(), content_type="image/png")})
        lay.refresh_from_db()
        self.expect("the logo is saved, print-sized, in the database",
                    r.status_code == 302 and lay.logo_data and lay.tagline == "Sufficiently superior", f"{r.status_code}")
        body = ac.get(docs[0][1]).content
        self.expect("documents show the logo, tagline and banner",
                    b"/organizations/documents/logo/" in body and b"Sufficiently superior" in body and b"General Hardware" in body)
        r = self.client_for(viewer).get(reverse("organizations:document_logo"))
        self.expect("the company's users get the logo", r.status_code == 200 and r["Content-Type"] == "image/png")
        self.expect("another company does not", self.client_for(outsider).get(reverse("organizations:document_logo")).status_code == 404)
        r = ac.post(reverse("organizations:document_layout"), {
            "accent": "#1F4E9A", "logo_file": SimpleUploadedFile("x.png", b"not an image", content_type="image/png")})
        self.expect("a file that is not a picture is refused", r.status_code == 200 and b"logo_file" in r.content
                    or r.status_code == 302 and layout_for(co).logo_data == lay.logo_data)
        r = ac.post(reverse("organizations:document_layout"), {"accent": "blue"})
        self.expect("a colour that is not #RRGGBB is refused", r.status_code == 200 and b"#1F4E9A" in r.content)

        self.section("5. roles")
        self.expect("a viewer can print an invoice", self.client_for(viewer).get(docs[2][1]).status_code == 200)
        self.expect("another company cannot print it", self.client_for(outsider).get(docs[2][1]).status_code == 404)
        r = self.client_for(manager).post(reverse("organizations:document_layout"), {"accent": "#000000"})
        self.expect("a finance manager cannot change the layout", r.status_code in (302, 403)
                    and layout_for(co).accent == "#1F4E9A", f"{r.status_code}")
        r = self.client_for(viewer).get(reverse("organizations:document_layout"))
        self.expect("a viewer cannot open the layout settings", r.status_code in (302, 403), f"{r.status_code}")
        r = ac.get(reverse("sales:invoice_detail", args=[inv.pk]))
        self.expect("the invoice page has the Print / PDF button",
                    r.status_code == 200 and reverse("sales:invoice_print", args=[inv.pk]).encode() in r.content)
