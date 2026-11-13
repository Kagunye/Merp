"""Checks for the KRA eTIMS integration, against the built-in KRA simulator.

Usage:
    python manage.py test_fix_kra          # rolls the whole run back

    1. off by default        -> with eTIMS off nothing is queued or sent
    2. device                -> initialising stores KRA's key, SCU id and MRC
    3. the till              -> a ticket is signed by KRA (invcNo 1, VAT-inclusive
                                buckets equal the ticket); its refund is a credit
                                note pointing at invcNo 1
    4. invoices              -> posting an invoice signs it; a credit note
                                references it; numbers run without a gap
    5. items and stock       -> a new product is registered with a KRA item code;
                                a goods receipt sends a stock movement then the
                                stock on hand, in that order
    6. KRA down              -> the sale still goes through; the invoice waits and
                                is sent when KRA is back; a resend is not doubled
    7. a bad tax rate        -> a rate KRA has no letter for is reported on the
                                queue, never breaks the sale
    8. the checklist         -> all KRA scenario groups pass on the simulator
"""
import datetime
from decimal import Decimal
from unittest import mock

from django.core.management.base import BaseCommand
from django.db import transaction
from django.test import Client
from django.test.utils import setup_test_environment, teardown_test_environment
from django.urls import reverse
from django.utils import timezone

D = Decimal


def _now(fn, using=None, robust=False):
    fn()


class Command(BaseCommand):
    help = "Checks for the KRA eTIMS integration (simulator)."

    def handle(self, *args, **opts):
        self.passed, self.failures = 0, []
        setup_test_environment()
        try:
            # Hooks run on commit; the run is one rolled-back transaction, so
            # commit callbacks are run at once instead.
            with mock.patch("django.db.transaction.on_commit", _now), transaction.atomic():
                self.run()
                transaction.set_rollback(True)
        finally:
            teardown_test_environment()
        self.stdout.write("")
        self.stdout.write(f"Checks passed: {self.passed}")
        if self.failures:
            self.stdout.write(self.style.ERROR(f"Failures: {len(self.failures)}"))
            for line in self.failures:
                self.stderr.write(self.style.ERROR(f"  FAIL {line}"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("Failures: 0"))

    def section(self, title):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"-- {title}"))

    def expect(self, label, cond, detail=""):
        if cond:
            self.passed += 1
            self.stdout.write(f"  ok   {label}" + (f"  ({detail})" if detail else ""))
            return True
        self.failures.append(f"{label} — {detail}" if detail else label)
        self.stdout.write(self.style.ERROR(f"  FAIL {label} {detail}"))
        return False

    def account(self, code):
        from apps.accounting.models import Account
        return Account.objects.get(company=self.company, code=code)

    def make_user(self, name, role):
        from apps.accounts.models import User
        from apps.organizations.models import CompanyMembership
        u = User.objects.create(username=f"{name}.{self.stamp}", email=f"{name}.{self.stamp}@fixkra.local",
                                first_name=name.title(), last_name="Test")
        u.set_password("fix-kra-pw")
        u.save()
        CompanyMembership.objects.create(company=self.company, user=u, role=role, branch=self.hq, is_default=True)
        return u

    def login(self, user):
        c = Client(raise_request_exception=False)
        c.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        return c

    def setup(self):
        from apps.accounting.models import TaxRate
        from apps.accounts.models import User
        from apps.crm.models import Customer
        from apps.inventory.models import Product, StockMovement, UnitOfMeasure
        from apps.organizations.models import Company, Warehouse
        self.stamp = datetime.datetime.now().strftime("%H%M%S%f")
        admin = User.objects.create(username=f"kra.admin.{self.stamp}", email=f"kra.admin.{self.stamp}@fixkra.local")
        admin.set_password("fix-kra-pw")
        admin.save()
        name = f"Fix KRA Traders {self.stamp}"
        self.login(admin).post(reverse("organizations:company_create"), {
            "name": name, "legal_name": f"{name} Ltd", "registration_number": f"PVT-{self.stamp}",
            "tax_id": "P051234567A", "vat_number": "VAT", "currency": "KES", "timezone": "Africa/Nairobi",
            "fiscal_year_start_month": "1", "industry": "Trading", "phone": "+254 700 000 003",
            "email": f"info@{self.stamp}.fixkra.local", "website": "", "address_line1": "3 Moi Avenue",
            "address_line2": "", "city": "Nairobi", "state_province": "Nairobi", "postal_code": "00100",
            "country": "Kenya"})
        self.company = Company.objects.get(name=name)
        self.hq = self.company.branches.get(is_headquarters=True)
        self.wh = Warehouse.objects.get(company=self.company, is_default=True)
        self.uom = (UnitOfMeasure.objects.filter(company=self.company, abbreviation="PCS").first()
                    or UnitOfMeasure.objects.create(company=self.company, name="Pieces", abbreviation="PCS", is_base=True))
        self.vat = TaxRate.objects.filter(company=self.company, rate=D("16")).first()
        self.soda = Product.objects.create(
            company=self.company, sku="SODA", name="Soda 500ml", unit_of_measure=self.uom,
            cost_price=D("50"), selling_price=D("100"), tax_rate=self.vat,
            income_account=self.account("4200"), cogs_account=self.account("5000"),
            inventory_account=self.account("1400"))
        StockMovement.objects.create(product=self.soda, warehouse=self.wh, movement_type="RECEIPT",
                                     quantity=D("100"), unit_cost=D("50"), movement_date=timezone.localdate(),
                                     reference="GRN-SODA", performed_by=admin)
        self.customer = Customer.objects.create(company=self.company, name="Buyer Ltd", tax_id="P051111111B")
        self.cashier = self.make_user("kra.cashier", "POS_CASHIER")
        self.accountant = self.make_user("kra.acct", "ACCOUNTANT")
        self.fm = self.make_user("kra.fm", "FINANCE_MANAGER")

    def run(self):
        from apps.kra import services
        from apps.kra.models import EtimsDevice, EtimsReceipt, EtimsSubmission
        from apps.pos import services as pos
        self.section("0. setup")
        self.setup()
        c = self.company

        self.section("1. off by default")
        sale0 = None
        till = pos.open_session(c, self.cashier, self.hq, D("0"))
        sale0 = pos.checkout(c, self.cashier, {str(self.soda.pk): "1"}, session=till, tendered="", token=pos.new_token())
        self.expect("with eTIMS off a till ticket queues nothing",
                    not EtimsSubmission.objects.filter(company=c).exists())

        self.section("2. device")
        conf = services.settings_for(c)
        conf.enabled, conf.backend = True, "SIMULATOR"
        conf.save()
        dev = EtimsDevice.objects.create(company=c, branch=self.hq, bhf_id="00", device_serial="MERPTEST01")
        services.initialize_device(dev)
        dev.refresh_from_db()
        self.expect("initialising stores the cmcKey, SCU id and MRC", dev.ready and dev.sdc_id and dev.mrc_no,
                    f"{dev.sdc_id} {dev.mrc_no}")

        self.section("3. the till")
        sale = pos.checkout(c, self.cashier, {str(self.soda.pk): "2"}, session=till, tendered="", token=pos.new_token())
        rec = services.receipt_for("pos.POSSale", sale.pk)
        self.expect("the ticket is signed by KRA as invcNo 1", rec is not None and rec.invoice_no == 1,
                    rec and rec.cu_invoice_no)
        sub = rec.submission if rec else None
        self.expect("VAT-inclusive totals equal the ticket (2 x 116 = 232, VAT 32 in B)",
                    sub and sub.payload["totAmt"] == float(sale.total_amount) == 232.0
                    and sub.payload["taxAmtB"] == 32.0, sub and f"{sub.payload['totAmt']} {sub.payload['taxAmtB']}")
        self.expect("the receipt has a signature, internal data and a KRA QR link",
                    rec and rec.signature and rec.internal_data and rec.qr_url.startswith("https://etims"))
        refund = pos.refund(sale, self.cashier, {str(sale.lines.first().pk): "1"}, reason="Broken bottle",
                            token=pos.new_token())
        rrec = services.receipt_for("pos.POSSale", refund.pk)
        self.expect("the refund is a credit note pointing at invcNo 1",
                    rrec and rrec.receipt_type == "R" and rrec.original_invoice_no == 1 and rrec.invoice_no == 2,
                    rrec and f"{rrec.receipt_type} org {rrec.original_invoice_no} no {rrec.invoice_no}")

        self.section("4. invoices")
        from apps.sales.models import Invoice, InvoiceLine
        inv = Invoice.objects.create(company=c, reference=f"INV-K{self.stamp[-5:]}", customer=self.customer,
                                     invoice_date=timezone.localdate(),
                                     due_date=timezone.localdate() + datetime.timedelta(days=30))
        InvoiceLine.objects.create(invoice=inv, product=self.soda, description="Soda 500ml",
                                   quantity=D("10"), unit_price=D("100"), tax_rate=self.vat)
        inv.subtotal, inv.tax_amount, inv.total_amount = D("1000"), D("160"), D("1160")
        inv.save()
        resp = self.login(self.accountant).post(reverse("sales:invoice_post", args=[inv.pk]), {})
        inv.refresh_from_db()
        irec = services.receipt_for("sales.Invoice", inv.pk)
        self.expect("posting the invoice gets it signed (invcNo 3)", irec is not None and irec.invoice_no == 3,
                    f"HTTP {resp.status_code} status {inv.status} rec {irec and irec.invoice_no}")
        isub = irec.submission if irec else None
        self.expect("the buyer's PIN and credit payment are on it",
                    isub and isub.payload["custTin"] == "P051111111B" and isub.payload["pmtTyCd"] == "02")
        note = Invoice.objects.create(company=c, reference=f"CN-K{self.stamp[-5:]}", customer=self.customer,
                                      invoice_date=timezone.localdate(), due_date=timezone.localdate(),
                                      status="CREDIT_NOTE", original_invoice=inv)
        InvoiceLine.objects.create(invoice=note, product=self.soda, description="Soda 500ml",
                                   quantity=D("2"), unit_price=D("100"), tax_rate=self.vat)
        # (post_credit_note calls the same hook; queued directly here.)
        services.queue_invoice(note)
        services.process(c)
        note.refresh_from_db()
        nrec = services.receipt_for("sales.Invoice", note.pk)
        self.expect("the credit note references the invoice's invcNo",
                    nrec and nrec.receipt_type == "R" and nrec.original_invoice_no == 3,
                    nrec and f"org {nrec.original_invoice_no}")
        nums = list(EtimsReceipt.objects.filter(device=dev).order_by("invoice_no").values_list("invoice_no", flat=True))
        self.expect("invoice numbers run 1, 2, 3, 4 with no gap", nums == [1, 2, 3, 4], str(nums))

        self.section("5. items and stock")
        from apps.inventory.models import Product, StockMovement
        juice = Product.objects.create(company=c, sku="JUICE", name="Juice 1L", unit_of_measure=self.uom,
                                       cost_price=D("80"), selling_price=D("150"), tax_rate=self.vat)
        isub = EtimsSubmission.objects.filter(company=c, kind="ITEM", source_id=str(juice.pk)).first()
        self.expect("a new product is registered with KRA", isub and isub.status == "SENT"
                    and juice.etims_item.item_cd.startswith("KE2NTU"), isub and juice.etims_item.item_cd)
        m = StockMovement.objects.create(product=juice, warehouse=self.wh, movement_type="RECEIPT",
                                         quantity=D("24"), unit_cost=D("80"), movement_date=timezone.localdate(),
                                         reference="GRN-JUICE")
        io = EtimsSubmission.objects.filter(company=c, kind="STOCK_IO", source_id=str(m.pk)).first()
        master = EtimsSubmission.objects.filter(depends_on=io, kind="STOCK_MASTER").first()
        self.expect("the receipt sends a stock movement (purchase, 02) then the stock on hand (24)",
                    io and io.status == "SENT" and io.payload["sarTyCd"] == "02" and master
                    and master.status == "SENT" and master.payload["rsdQty"] == 24.0,
                    io and f"{io.payload['sarTyCd']} {master and master.payload['rsdQty']}")

        self.section("6. KRA down")
        from apps.kra.backends import EtimsTransportError, SimulatorBackend
        import apps.kra.services as svc
        real = svc.backend_for

        class Down(SimulatorBackend):
            def call(self, operation, payload):
                raise EtimsTransportError("network down (test)")
        svc.backend_for = lambda conf, dev=None: Down(conf, dev)
        try:
            offline = pos.checkout(c, self.cashier, {str(self.soda.pk): "1"}, session=till, tendered="", token=pos.new_token())
        finally:
            svc.backend_for = real
        osub = EtimsSubmission.objects.filter(source_type="pos.POSSale", source_id=str(offline.pk)).first()
        self.expect("the sale still goes through while KRA is down", offline.pk and osub and osub.status == "PENDING",
                    osub and osub.last_error)
        EtimsSubmission.objects.filter(company=c).update(next_try_at=None)
        services.process(c)
        osub.refresh_from_db()
        self.expect("it is sent when KRA is back", osub.status == "SENT")
        stuck = EtimsSubmission.objects.filter(company=c, status__in=("PENDING", "HELD", "FAILED"))
        self.expect("…and the stock movement that waited behind it follows, in order",
                    not stuck.exists(), str(list(stuck.values_list("kind", "sequence", "status"))))
        EtimsSubmission.objects.filter(pk=osub.pk).update(status="PENDING")
        services.process(c)
        osub.refresh_from_db()
        self.expect("a resend KRA already has (994) is accepted without a second receipt",
                    osub.status == "SENT" and osub.result_code == "994"
                    and EtimsReceipt.objects.filter(submission=osub).count() == 1)

        self.section("7. a bad tax rate")
        from apps.accounting.models import TaxRate
        odd = TaxRate.objects.create(company=c, name="Odd 14%", code="ODD14", rate=D("14"),
                                     account=self.vat.account, effective_from=datetime.date(2020, 1, 1))
        weird = Product.objects.create(company=c, sku="ODD", name="Odd item", unit_of_measure=self.uom,
                                       cost_price=D("10"), selling_price=D("20"), tax_rate=odd,
                                       track_inventory=False, product_type="SERVICE")
        ok_sale = pos.checkout(c, self.cashier, {str(weird.pk): "1"}, session=till, tendered="", token=pos.new_token())
        self.expect("a tax rate KRA has no letter for never breaks the sale", ok_sale.pk is not None)
        self.expect("…and nothing half-fiscalised is left for it",
                    not EtimsReceipt.objects.filter(source_id=str(ok_sale.pk)).exists())

        self.section("8. the checklist")
        from apps.kra import selftest
        # As in real use: the checklist runs in its own rolled-back transaction
        # and the document hooks wait for a commit that never comes.
        with mock.patch("django.db.transaction.on_commit", lambda fn, using=None, robust=False: None):
            res = selftest.run(c)
        self.expect(f"KRA checklist on the simulator: {res['passed']} passed, {res['failed']} failed",
                    res["failed"] == 0 and res["passed"] >= 40,
                    "; ".join(f'{r["label"]}: {r["detail"]}' for r in res["results"] if not r["ok"])[:600])
        fm_code = self.login(self.fm).get(reverse("kra:home")).status_code
        cashier_code = self.login(self.cashier).get(reverse("kra:home")).status_code
        self.expect("the eTIMS screens are open to finance, closed to a cashier",
                    fm_code == 200 and cashier_code == 403, f"finance {fm_code}, cashier {cashier_code}")
        self.expect("the scheduled run refuses a call without the token",
                    Client().get(reverse("kra:cron")).status_code == 403)
