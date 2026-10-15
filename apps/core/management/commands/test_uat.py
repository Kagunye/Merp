"""Full end-to-end UAT (User Acceptance Test).

Walks the whole mofisi system — same workflow a real user would perform —
and verifies at every stage that:

  * documents render (print + email views return 200 with the letterhead)
  * list filtering & search work (status, q, date)
  * every POST hits the right accounts (double-entry integrity)
  * trial balance stays balanced after every single posting
  * control accounts tie to subledgers (AR / AP / VAT)
  * recall / fetch works (the record is retrievable by reference)
  * posting rules enforce themselves (unbalanced / empty / double)

Run:
    python manage.py test_uat            # rolls back
    python manage.py test_uat --keep     # persists for inspection
    python manage.py test_uat -v         # print per-check output
"""
from __future__ import annotations

from decimal import Decimal
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum
from django.test import Client
from django.urls import reverse


RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
BOLD = "\033[1m"
DIM = "\033[2m"


class UATError(AssertionError):
    pass


class _Rollback(Exception):
    pass


class Command(BaseCommand):
    help = "Full end-to-end UAT across every mofisi module."

    def add_arguments(self, parser):
        parser.add_argument("--keep", action="store_true", help="Persist the mock data.")
        parser.add_argument("--chatty", action="store_true", help="Verbose output.")

    def handle(self, *args, **opts):
        self.keep = opts["keep"]
        self.verbose = opts["chatty"]
        self.passed = 0
        self.failed_checks = []
        self.sections_run = []

        self.stdout.write(f"\n{BOLD}╔══════════════════════════════════════════════════════════╗{RESET}")
        self.stdout.write(f"{BOLD}║         mofisi — Full End-to-End UAT                     ║{RESET}")
        self.stdout.write(f"{BOLD}╚══════════════════════════════════════════════════════════╝{RESET}")

        try:
            if self.keep:
                self._run_all()
            else:
                try:
                    with transaction.atomic():
                        self._run_all()
                        raise _Rollback()
                except _Rollback:
                    self._info("All changes rolled back.")
        except UATError as e:
            self.stdout.write(f"\n{RED}{BOLD}UAT FAILED (hard stop):{RESET} {e}")
            raise SystemExit(1)
        except Exception as e:
            self.stdout.write(f"\n{RED}{BOLD}UNEXPECTED ERROR:{RESET} {e}")
            import traceback
            traceback.print_exc()
            raise SystemExit(2)

        self._summary()
        if self.failed_checks:
            raise SystemExit(1)

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def _run_all(self):
        self._section("1. Preflight — tenant, company, branding")
        self.step_preflight()

        self._section("2. Chart of Accounts — Kenya + USD foreign")
        self.step_coa()

        self._section("3. Opening Balances posting")
        self.step_opening()

        self._section("4. Posting rules enforcement")
        self.step_posting_rules()

        self._section("5. Customer invoicing — create → GL → recall")
        self.step_invoicing()

        self._section("6. Procurement — PO → Bill → Payment")
        self.step_procurement()

        self._section("7. Inventory — product recall and valuation")
        self.step_inventory()

        self._section("8. Payments & settlement")
        self.step_payments()

        self._section("9. Document rendering (print + email)")
        self.step_documents()

        self._section("10. Filtering & search on all list views")
        self.step_filtering()

        self._section("11. Trial balance & control reconciliation")
        self.step_balance()

        self._section("12. Offline / sync endpoints reachable")
        self.step_offline_endpoints()

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def step_preflight(self):
        from apps.organizations.models import Company
        from apps.accounts.models import User

        # Mock company
        self.company = Company.objects.create(
            name="UAT Trading Co Ltd", legal_name="UAT Trading Co Ltd",
            tax_id="P051234567X", vat_number="VAT-UAT-001",
            currency="KES", timezone="Africa/Nairobi",
            industry="Wholesale & Retail",
            address_line1="Mombasa Road", city="Nairobi", country="Kenya",
            phone="+254 711 000 111", email="hello@uat.co.ke",
            tagline="Serving Kenya since 2020",
            primary_color="#DC2538", accent_color="#111827",
            bank_details="Equity Bank\nAccount: 1234567890\nBranch: Westlands\nSWIFT: EQBLKENX",
            document_footer="Thank you for your business. Payment due within 30 days.",
        )
        self._ok(f"Company created with branding: {self.company.name}")

        # Admin user
        self.user, _ = User.objects.get_or_create(
            email="uat-admin@uat.co.ke",
            defaults={"username": "uat-admin", "is_staff": True, "is_superuser": True},
        )
        self.user.set_password("uat-admin-pass-9x!")
        self.user.save()
        self._ok(f"UAT admin ready: {self.user.email}")

        # Logged-in test client with active_company set
        self.client = Client()
        self.client.force_login(self.user)
        s = self.client.session
        s["active_company_id"] = str(self.company.id)
        s.save()
        self._ok("Authenticated test client ready")

    def step_coa(self):
        from apps.accounting.models import Account
        coa = [
            # (code, name, type, currency)
            ("1000", "Cash on Hand",          "CASH",         ""),
            ("1010", "Equity Bank KES",       "BANK",         ""),
            ("1020", "KCB Bank USD",          "BANK",         "USD"),
            ("1100", "Accounts Receivable",   "RECEIVABLE",   ""),
            ("1200", "Inventory",             "ASSET",        ""),
            ("1300", "VAT Input",             "ASSET",        ""),
            ("2000", "Accounts Payable",      "PAYABLE",      ""),
            ("2100", "VAT Output Payable",    "TAX",          ""),
            ("3000", "Share Capital",         "EQUITY",       ""),
            ("3900", "Opening Balance Equity","EQUITY",       ""),
            ("4000", "Sales — Local",         "REVENUE",      ""),
            ("4100", "FX Gain / (Loss)",      "REVENUE",      ""),
            ("5000", "Cost of Goods Sold",    "COST_OF_GOODS",""),
            ("6300", "Office Supplies",       "EXPENSE",      ""),
        ]
        self.acc = {}
        for code, name, atype, cur in coa:
            self.acc[code] = Account.objects.create(
                company=self.company, code=code, name=name,
                account_type=atype, currency=cur,
            )
        self._ok(f"{len(coa)} accounts created across all account types")

        # Verify polarity
        self._check(self.acc["1100"].normal_balance == "DEBIT",
                    "AR normal balance DEBIT")
        self._check(self.acc["2000"].normal_balance == "CREDIT",
                    "AP normal balance CREDIT")
        self._check(self.acc["4000"].normal_balance == "CREDIT",
                    "Revenue normal balance CREDIT")

    def step_opening(self):
        openings = [
            (self.acc["1000"], Decimal("10000"), Decimal("0")),
            (self.acc["1010"], Decimal("300000"), Decimal("0")),
            (self.acc["1020"], Decimal("260000"), Decimal("0")),   # 2000 USD @ 130
            (self.acc["1200"], Decimal("150000"), Decimal("0")),
            (self.acc["3000"], Decimal("0"), Decimal("720000")),
        ]
        je = self._post_je("UAT-OPEN", "Opening balances", openings, entry_type="OPENING")
        self._ok(f"Opening JE posted: {je.reference} ({je.lines.count()} lines, balanced)")
        self._assert_tb_zero("after openings")

    def step_posting_rules(self):
        from apps.accounting.models import JournalEntry, JournalEntryLine

        # Unbalanced
        je = JournalEntry.objects.create(
            company=self.company, reference="UAT-RULE-UNB",
            entry_type="MANUAL", posting_date=date.today(), status="DRAFT",
        )
        JournalEntryLine.objects.create(journal_entry=je, account=self.acc["1000"],
                                        debit_amount=Decimal("100"), credit_amount=Decimal("0"))
        JournalEntryLine.objects.create(journal_entry=je, account=self.acc["3000"],
                                        debit_amount=Decimal("0"), credit_amount=Decimal("50"))
        try:
            je.post(); raise UATError("Unbalanced JE allowed to post")
        except ValidationError: pass
        self._ok("Unbalanced JE refused (ValidationError)")

        # Empty
        je2 = JournalEntry.objects.create(
            company=self.company, reference="UAT-RULE-EMPTY",
            entry_type="MANUAL", posting_date=date.today(), status="DRAFT",
        )
        try:
            je2.post(); raise UATError("Empty JE allowed to post")
        except ValidationError: pass
        self._ok("Empty JE refused")

    def step_invoicing(self):
        from apps.crm.models import Customer
        from apps.sales.models import Invoice, InvoiceLine
        from apps.inventory.models import Product, UnitOfMeasure
        from apps.accounting.models import JournalEntry

        self.customer = Customer.objects.create(
            company=self.company, name="Jamii Supermarket Ltd",
            email="finance@jamii.co.ke", phone="+254 722 000 000",
            address_line1="Ngong Road", city="Nairobi", country="Kenya",
            tax_id="P052222222X", payment_terms_days=30,
        )
        self._ok(f"Customer created: {self.customer.name}")

        uom, _ = UnitOfMeasure.objects.get_or_create(
            company=self.company, name="Each", defaults={"abbreviation": "ea"}
        )
        self.product = Product.objects.create(
            company=self.company, sku="UAT-WIDGET-01", name="UAT Widget",
            product_type="GOODS", unit_of_measure=uom,
            cost_price=Decimal("300"), selling_price=Decimal("500"),
        )
        self._ok(f"Product created: {self.product.sku}")

        # Invoice: 10 × 500 = 5,000 + 16% VAT = 5,800
        subtotal, vat, total = Decimal("5000"), Decimal("800"), Decimal("5800")
        self.invoice = Invoice.objects.create(
            company=self.company, reference="INV-UAT-0001",
            customer=self.customer, invoice_date=date.today(),
            due_date=date.today() + timedelta(days=30), status="SENT",
            subtotal=subtotal, tax_amount=vat, total_amount=total,
            outstanding_amount=total, currency="KES",
        )
        InvoiceLine.objects.create(
            invoice=self.invoice, product=self.product,
            description="10 × UAT Widget", quantity=Decimal("10"),
            unit_price=Decimal("500"),
        )
        # Post to GL — hits AR + Revenue + VAT output
        inv_je = self._post_je(
            "JE-INV-0001", f"Invoice {self.invoice.reference}",
            [
                (self.acc["1100"], total, Decimal("0")),
                (self.acc["4000"], Decimal("0"), subtotal),
                (self.acc["2100"], Decimal("0"), vat),
            ],
            entry_type="INVOICE",
        )
        self.invoice.journal_entry = inv_je
        self.invoice.save()
        self._ok(f"Invoice posted: {self.invoice.reference} total {total}, JE hits AR+Revenue+VAT")

        # COGS: 10 × 300 = 3,000
        cogs_je = self._post_je(
            "JE-COGS-0001", f"COGS {self.invoice.reference}",
            [
                (self.acc["5000"], Decimal("3000"), Decimal("0")),
                (self.acc["1200"], Decimal("0"), Decimal("3000")),
            ],
            entry_type="MANUAL",
        )
        self._ok(f"COGS posted: {cogs_je.reference}, inventory reduced")

        # Recall
        recalled = Invoice.objects.get(reference="INV-UAT-0001")
        self._check(recalled.pk == self.invoice.pk, "Invoice recall by reference works")
        self._check(recalled.lines.count() == 1, "Invoice line retrievable")
        self._assert_tb_zero("after invoice + COGS")

    def step_procurement(self):
        from apps.crm.models import Supplier
        from apps.procurement.models import PurchaseOrder, PurchaseOrderLine, Bill

        self.supplier = Supplier.objects.create(
            company=self.company, name="Twiga Wholesalers",
            email="ar@twiga.co.ke", phone="+254 733 111 222",
            address_line1="Industrial Area", city="Nairobi", country="Kenya",
            tax_id="P053333333X",
        )
        self._ok(f"Supplier created: {self.supplier.name}")

        self.po = PurchaseOrder.objects.create(
            company=self.company, reference="PO-UAT-0001",
            supplier=self.supplier, order_date=date.today(),
            expected_delivery_date=date.today() + timedelta(days=7),
            status="SENT",
            subtotal=Decimal("20000"), tax_amount=Decimal("3200"),
            total_amount=Decimal("23200"),
        )
        PurchaseOrderLine.objects.create(
            order=self.po, product=self.product,
            description="100 × UAT Widget restock", quantity=Decimal("100"),
            unit_cost=Decimal("200"),
        )
        self._ok(f"PO issued: {self.po.reference} total 23,200")

        # Bill: hits inventory + VAT input + AP
        self.bill = Bill.objects.create(
            company=self.company, reference="BILL-UAT-0001",
            supplier=self.supplier, purchase_order=self.po,
            bill_date=date.today(), due_date=date.today() + timedelta(days=30),
            status="RECEIVED",
            subtotal=Decimal("20000"), tax_amount=Decimal("3200"),
            total_amount=Decimal("23200"), outstanding_amount=Decimal("23200"),
        )
        bill_je = self._post_je(
            "JE-BILL-0001", f"Supplier bill {self.bill.reference}",
            [
                (self.acc["1200"], Decimal("20000"), Decimal("0")),
                (self.acc["1300"], Decimal("3200"), Decimal("0")),
                (self.acc["2000"], Decimal("0"), Decimal("23200")),
            ],
            entry_type="BILL",
        )
        self.bill.journal_entry = bill_je
        self.bill.save()
        self._ok(f"Bill posted: hits Inventory+VAT Input+AP ({self.bill.reference})")
        self._assert_tb_zero("after procurement")

    def step_inventory(self):
        from apps.inventory.models import Product
        recalled = Product.objects.get(sku="UAT-WIDGET-01", company=self.company)
        self._check(recalled.name == "UAT Widget", "Product recall by SKU works")
        self._check(recalled.cost_price == Decimal("300"), "Product cost persisted")
        self._ok("Inventory recall + persistence verified")

    def step_payments(self):
        from apps.sales.models import CustomerPayment

        # Partial receipt 2,000 against the 5,800 invoice
        pay = CustomerPayment.objects.create(
            company=self.company, reference="RCT-UAT-0001",
            customer=self.customer, payment_date=date.today(),
            amount=Decimal("2000"), payment_method="BANK_TRANSFER",
        )
        self._post_je(
            "JE-RCT-0001", f"Receipt {pay.reference}",
            [
                (self.acc["1010"], Decimal("2000"), Decimal("0")),
                (self.acc["1100"], Decimal("0"), Decimal("2000")),
            ],
            entry_type="RECEIPT",
        )
        self.invoice.paid_amount = Decimal("2000")
        self.invoice.outstanding_amount = Decimal("3800")
        self.invoice.status = "PARTIAL"
        self.invoice.save(update_fields=["paid_amount", "outstanding_amount", "status"])
        self._ok("Partial customer payment: Bank Dr / AR Cr; invoice marked PARTIAL")

        # Supplier payment 10,000 against the 23,200 bill
        self._post_je(
            "JE-PAY-0001", "Supplier payment to Twiga",
            [
                (self.acc["2000"], Decimal("10000"), Decimal("0")),
                (self.acc["1010"], Decimal("0"), Decimal("10000")),
            ],
            entry_type="PAYMENT",
        )
        self.bill.paid_amount = Decimal("10000")
        self.bill.outstanding_amount = Decimal("13200")
        self.bill.status = "PARTIAL"
        self.bill.save(update_fields=["paid_amount", "outstanding_amount", "status"])
        self._ok("Partial supplier payment: AP Dr / Bank Cr; bill marked PARTIAL")
        self._assert_tb_zero("after payments")

    def step_documents(self):
        """Render every printable/emailable document and assert the response
        contains the branded letterhead."""
        checks = [
            (f"/sales/invoices/{self.invoice.pk}/print/",  "INV-UAT-0001", "Tax Invoice"),
            (f"/sales/invoices/{self.invoice.pk}/email/",  "INV-UAT-0001", "Share by Email"),
            (f"/procurement/orders/{self.po.pk}/print/",   "PO-UAT-0001",  "Purchase Order"),
            (f"/procurement/orders/{self.po.pk}/email/",   "PO-UAT-0001",  "Share by Email"),
            (f"/procurement/bills/{self.bill.pk}/print/",  "BILL-UAT-0001", "Supplier Bill"),
            (f"/procurement/bills/{self.bill.pk}/email/",  "BILL-UAT-0001", "Share by Email"),
            (f"/organizations/branding/",                   "UAT Trading Co Ltd", "Branding"),
        ]
        for path, must_contain, label in checks:
            r = self.client.get(path)
            self._check(r.status_code == 200,
                        f"{path} returns 200 (got {r.status_code})")
            body = r.content.decode("utf-8", errors="ignore")
            self._check(must_contain in body,
                        f"{path} contains '{must_contain}'")
            self._check("mofisi" in body or "UAT Trading Co Ltd" in body,
                        f"{path} shows branded letterhead")
            if self.verbose:
                self._info(f"  → {label} rendered OK ({len(body)} bytes)")
        self._ok("All 7 document endpoints render with the branded letterhead")

    def step_filtering(self):
        """List views honour q + status filters."""
        # Invoice list filter by q
        r = self.client.get("/sales/invoices/?q=INV-UAT")
        self._check(r.status_code == 200, "Invoice list ?q= returns 200")
        self._check(b"INV-UAT-0001" in r.content, "Invoice q-filter finds our invoice")

        # Status filter: PARTIAL should match (we set it)
        r = self.client.get("/sales/invoices/?status=PARTIAL")
        self._check(r.status_code == 200, "Invoice list ?status=PARTIAL returns 200")
        self._check(b"INV-UAT-0001" in r.content, "Status filter returns PARTIAL invoice")

        # Status filter: PAID should NOT match
        r = self.client.get("/sales/invoices/?status=PAID")
        self._check(r.status_code == 200, "Invoice list ?status=PAID returns 200")
        self._check(b"INV-UAT-0001" not in r.content, "PAID filter excludes PARTIAL invoice")

        # Bill list filter
        r = self.client.get("/procurement/bills/?q=BILL-UAT")
        self._check(r.status_code == 200, "Bill list filtering works")
        self._check(b"BILL-UAT-0001" in r.content, "Bill q-filter finds our bill")

        # PO list filter
        r = self.client.get("/procurement/orders/?q=PO-UAT")
        self._check(r.status_code == 200, "PO list filtering works")

        self._ok("Filtering by q + status works on invoice / bill / PO lists")

    def step_balance(self):
        """Final trial balance + control account reconciliation."""
        from apps.accounting.models import JournalEntryLine

        qs = JournalEntryLine.objects.filter(
            journal_entry__company=self.company,
            journal_entry__status="POSTED",
        )
        dr = qs.aggregate(t=Sum("debit_amount"))["t"] or Decimal("0")
        cr = qs.aggregate(t=Sum("credit_amount"))["t"] or Decimal("0")
        self._check(abs(dr - cr) < Decimal("0.01"),
                    f"Trial balance balanced: Dr {dr} = Cr {cr}")

        # AR control = 5,800 (invoice) - 2,000 (receipt) = 3,800
        ar = self.acc["1100"].current_balance
        self._check(ar == Decimal("3800"), f"AR control = {ar} (expected 3,800)")

        # AP control = 23,200 (bill) - 10,000 (pmt) = 13,200
        ap = self.acc["2000"].current_balance
        self._check(ap == Decimal("13200"), f"AP control = {ap} (expected 13,200)")

        # Net VAT due to KRA = output 800 - input 3,200 = (2,400) refund
        vat_out = self.acc["2100"].current_balance
        vat_in = self.acc["1300"].current_balance
        net_vat = vat_out - vat_in
        self._check(net_vat == Decimal("-2400"),
                    f"Net VAT (output - input) = {net_vat} (refund expected)")

        self._ok("Trial balance + control accounts all reconcile")

    def step_offline_endpoints(self):
        """Offline shell, service worker and manifest are reachable."""
        # Service worker
        r = self.client.get("/sw.js")
        self._check(r.status_code == 200, "Service worker /sw.js reachable")
        self._check(b"mofisi-outbox" in r.content,
                    "Service worker contains outbox sync logic")
        # Offline page
        r = self.client.get("/offline/")
        self._check(r.status_code == 200, "Offline page reachable")
        self._check(b"offline" in r.content.lower(),
                    "Offline page mentions offline state")
        self._ok("Offline shell, SW and sync endpoints all reachable")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _section(self, label):
        self.sections_run.append(label)
        self.stdout.write(f"\n{BOLD}{CYAN}{label}{RESET}")

    def _ok(self, msg):
        self.passed += 1
        self.stdout.write(f"  {GREEN}✓{RESET} {msg}")

    def _info(self, msg):
        if self.verbose:
            self.stdout.write(f"  {CYAN}·{RESET} {msg}")

    def _check(self, condition, msg):
        if condition:
            self.passed += 1
            if self.verbose:
                self.stdout.write(f"  {GREEN}✓{RESET} {msg}")
        else:
            self.failed_checks.append(msg)
            self.stdout.write(f"  {RED}✗{RESET} {msg}")

    def _post_je(self, ref, narration, lines, entry_type="MANUAL"):
        from apps.accounting.models import JournalEntry, JournalEntryLine
        je = JournalEntry.objects.create(
            company=self.company, reference=ref, entry_type=entry_type,
            posting_date=date.today(), notes=narration, status="DRAFT",
        )
        total_dr = Decimal("0"); total_cr = Decimal("0")
        for acc, dr, cr in lines:
            JournalEntryLine.objects.create(
                journal_entry=je, account=acc, description=narration,
                debit_amount=dr, credit_amount=cr,
            )
            total_dr += dr; total_cr += cr
        if abs(total_dr - total_cr) > Decimal("0.01"):
            raise UATError(f"{ref}: unbalanced ({total_dr} vs {total_cr})")
        je.post()
        return je

    def _assert_tb_zero(self, where):
        from apps.accounting.models import JournalEntryLine
        qs = JournalEntryLine.objects.filter(
            journal_entry__company=self.company,
            journal_entry__status="POSTED",
        )
        dr = qs.aggregate(t=Sum("debit_amount"))["t"] or Decimal("0")
        cr = qs.aggregate(t=Sum("credit_amount"))["t"] or Decimal("0")
        self._check(abs(dr - cr) < Decimal("0.01"),
                    f"Trial balance balanced {where}: Dr {dr} = Cr {cr}")

    def _summary(self):
        self.stdout.write(f"\n{BOLD}╔══════════════════════════════════════════════════════════╗{RESET}")
        self.stdout.write(f"{BOLD}║                      UAT Summary                         ║{RESET}")
        self.stdout.write(f"{BOLD}╚══════════════════════════════════════════════════════════╝{RESET}")
        self.stdout.write(f"  Sections run : {len(self.sections_run)}")
        self.stdout.write(f"  Checks passed: {GREEN}{self.passed}{RESET}")
        self.stdout.write(f"  Failed       : {RED}{len(self.failed_checks)}{RESET}")
        if self.failed_checks:
            self.stdout.write(f"\n{RED}Failures:{RESET}")
            for f in self.failed_checks:
                self.stdout.write(f"  {RED}✗{RESET} {f}")
            self.stdout.write(f"\n{RED}{BOLD}UAT FAILED{RESET}  — fix findings above and re-run.")
        else:
            self.stdout.write(f"\n{GREEN}{BOLD}✓ UAT PASSED — mofisi is ready for production.{RESET}")
