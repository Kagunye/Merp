"""Regression checks for the purchasing, stock and production fixes.

Every check drives the real screens with a logged-in client, then reads the
ledger and the shelves back and proves the two still describe the same stock:
the inventory account equals quantity times average cost on every shelf, the
received-not-invoiced accrual equals what the orders still have standing, and
no junk typed into a box produces a server error.

Usage:
    python manage.py test_fix_procurement          # rolls the whole run back
    python manage.py test_fix_procurement --keep   # leave the test company behind

What is covered (numbers are the bug list the testers filed):
    1. an order's goods can be billed once only — a typed bill cannot name an
       order, so "Bill this order" is the one road, before or after the goods
    2. a posted bill cannot be edited; a draft can
    3. hand-entered stock movements: outflows at the shelf average, no
       negative costs, no one-legged transfers, no orphan returns
    4. a bill above or below the order price clears the accrual exactly and
       books the difference to purchase price variance (5400)
    5. a debit note on an order-matched bill leaves the accrual closed
    6. a bill with no order is charged to the account chosen, never to stock
    7. production consumes at the average and makes at what it consumed;
       short stock is a message, not a server error
    8. junk numbers and ids are refused with a message on every screen listed
    9. a bill cannot name another supplier's order or a cancelled one, nor fall
       due before it was issued
"""
import datetime
from decimal import Decimal as D

from django.db import transaction
from django.db.models import Sum
from django.test import Client
from django.test.utils import setup_test_environment, teardown_test_environment
from django.urls import reverse

from apps.core.management.commands.test_accounting import (
    Command as AccountingCommand, formset, merge,
)

Z = D("0")
CENT = D("0.01")
JUNK = ("NaN", "sNaN", "Infinity", "-Infinity", "1e999999", "abc", "1,250")


class Command(AccountingCommand):
    help = "Regression checks for the procurement, stock and manufacturing fixes."

    def handle(self, *args, **opts):
        self.passed = 0
        self.failures = []
        self.notes = []
        self.today = datetime.date.today()

        setup_test_environment()
        try:
            with transaction.atomic():
                self.run()
                if not opts["keep"]:
                    transaction.set_rollback(True)
        finally:
            teardown_test_environment()

        self.stdout.write("")
        self.stdout.write(f"Checks passed: {self.passed}")
        if self.failures:
            self.stdout.write(self.style.ERROR(f"Failures: {len(self.failures)}"))
            for line in self.failures:
                self.stderr.write(self.style.ERROR(f"  FAIL {line}"))
        else:
            self.stdout.write(self.style.SUCCESS("Failures: 0"))
        if not opts["keep"]:
            self.stdout.write("Test data rolled back (use --keep to retain it).")
        if self.failures:
            raise SystemExit(1)

    # ─────────────────────────────── helpers ─────────────────────────────────

    def net(self, code):
        """Debit less credit on one account, over everything posted."""
        from apps.accounting.models import JournalEntryLine

        row = JournalEntryLine.objects.filter(
            journal_entry__company=self.company, journal_entry__status="POSTED",
            account__code=code,
        ).aggregate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        return (row["dr"] or Z) - (row["cr"] or Z)

    def valuation(self):
        from apps.inventory.models import StockLevel

        return sum((level.quantity * level.avg_cost for level in StockLevel.objects.filter(
            warehouse__company=self.company, product__track_inventory=True)), Z)

    def stock_agrees(self, label):
        gl, shelf = self.net("1400"), self.valuation()
        return self.expect(f"{label}: inventory 1400 equals the stock valuation",
                           abs(gl - shelf) < CENT, f"GL {gl} against shelves {shelf:.4f}")

    def accrual_agrees(self, label):
        from apps.accounting.gl_service import accrual_remaining
        from apps.procurement.models import PurchaseOrder

        standing = sum((accrual_remaining(po) for po in PurchaseOrder.objects.filter(
            company=self.company)), Z)
        gl = -self.net("2110")
        return self.expect(f"{label}: 2110 equals what the orders still have accrued",
                           abs(gl - standing) < CENT, f"GL {gl} against orders {standing}")

    def no_crash(self, label, response):
        return self.expect(f"{label} answers without a server error",
                           response.status_code < 500, f"HTTP {response.status_code}")

    def shelf(self, product, warehouse=None):
        from apps.inventory.models import StockLevel

        return StockLevel.objects.filter(
            product=product, warehouse=warehouse or self.warehouse).first()

    def make_product(self, client, sku, cost, track=True):
        from apps.inventory.models import Product

        data = {
            "sku": sku, "name": f"Item {sku}", "product_type": "GOODS",
            "category": self.category.pk, "unit_of_measure": self.uom.pk,
            "cost_price": cost, "selling_price": "500", "tax_rate": self.vat.pk,
            "reorder_level": "0", "reorder_quantity": "0", "valuation_method": "WAVG",
            "income_account": self.account("4200").pk,
            "cogs_account": self.account("5000").pk,
            "inventory_account": self.account("1400").pk,
            "barcode": "", "description": "",
        }
        if track:
            data["track_inventory"] = "on"
        self.post(client, f"product {sku}", reverse("inventory:product_create"), data)
        return Product.objects.get(company=self.company, sku=sku)

    def order(self, client, ref, lines, supplier=None, send=True):
        """A purchase order through the order screens: header, lines, send."""
        from apps.procurement.models import PurchaseOrder

        self.post(client, f"order {ref}", reverse("procurement:po_create"), {
            "reference": ref, "supplier": (supplier or self.supplier).pk,
            "order_date": str(self.today), "expected_delivery_date": "",
            "warehouse": self.warehouse.pk, "notes": "",
        })
        po = PurchaseOrder.objects.get(company=self.company, reference=ref)
        for product, qty, cost, taxed in lines:
            self.post(client, f"{ref} line", reverse("procurement:po_line_add", args=[po.pk]), {
                "product": product.pk, "quantity": str(qty), "unit_cost": str(cost),
                "tax_rate": self.vat.pk if taxed else "", "description": ""})
        if send:
            self.post(client, f"send {ref}", reverse("procurement:po_send", args=[po.pk]), {})
        po.refresh_from_db()
        return po

    def receive(self, client, po, quantities):
        data = {"warehouse": self.warehouse.pk}
        for line, qty in quantities:
            data[f"receive_qty_{line.pk}"] = str(qty)
        return self.post(client, f"receive on {po.reference}",
                         reverse("procurement:grn_record", args=[po.pk]), data)

    def bill_order(self, client, po, post=True):
        """'Bill this order', then post the draft it raised."""
        self.post(client, f"bill {po.reference}", reverse("procurement:po_to_bill", args=[po.pk]), {})
        bill = po.bills.order_by("-created_at").first()
        if bill and post:
            self.post(client, f"post {bill.reference}",
                      reverse("procurement:bill_post", args=[bill.pk]), {})
            bill.refresh_from_db()
        return bill

    def bill_form(self, ref, **extra):
        data = {
            "reference": ref, "supplier": self.supplier.pk,
            "expense_account": self.account("6000").pk,
            "supplier_reference": f"INV-{ref}", "bill_date": str(self.today),
            "due_date": str(self.today + datetime.timedelta(days=30)), "currency": "KES",
            "subtotal": "1000", "tax_amount": "160", "total_amount": "1160", "notes": "",
        }
        data.update({k: ("" if v is None else str(v)) for k, v in extra.items()})
        return data

    def adjust(self, client, kind, qty, cost="", product=None):
        return client.post(reverse("inventory:adjustment_create"), {
            "product": (product or self.raw).pk, "warehouse": self.warehouse.pk,
            "movement_type": kind, "quantity": str(qty), "unit_cost": str(cost),
            "movement_date": str(self.today), "reference": "FIX-ADJ", "notes": "",
        })

    # ─────────────────────────────── the run ─────────────────────────────────

    def run(self):
        from apps.accounts.models import User
        from apps.crm.models import Supplier

        stamp = datetime.datetime.now().strftime("%H%M%S%f")
        self.stamp = stamp
        user = User.objects.create(
            username=f"proc.{stamp}", email=f"proc.{stamp}@smoke.local",
            first_name="Paul", last_name="Buyer",
        )
        user.set_password("smoke-test-pw")
        user.save()
        self.user = user
        self.login_email = user.email
        client = Client(raise_request_exception=False)
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")

        self.section("Company and products")
        self.company = self.create_company(client, user, stamp)
        self.setup_master_data()
        self.other_supplier, _ = Supplier.objects.get_or_create(
            company=self.company, name="Thika Steel Ltd")
        self.raw = self.make_product(client, "RAW-1", "50")
        self.service = self.make_product(client, "SVC-1", "10", track=False)

        self.section("1. An order's goods are billed once")
        self.check_single_billing(client)
        self.section("2. A posted bill cannot be edited")
        self.check_posted_bill_locked(client)
        self.section("3. Hand-entered stock movements")
        self.check_adjustments(client)
        self.section("4. Bill price against order price")
        self.check_price_variance(client)
        self.section("5. Debit notes on order-matched bills")
        self.check_debit_notes(client)
        self.section("6. Bills with no order are charged where they belong")
        self.check_unordered_bills(client)
        self.section("7. Manufacturing at actual cost")
        self.check_manufacturing(client)
        self.section("8. Junk input is refused, not a server error")
        self.check_junk(client)
        self.section("9. Bill links and dates")
        self.check_links(client)

        self.section("The book at the end")
        self.stock_agrees("after the whole run")
        self.accrual_agrees("after the whole run")
        dr = self.net("1400")  # touch to keep the figure in the log
        self.expect("work in progress is empty once every order is complete",
                    self.net("1450") == Z, str(self.net("1450")))
        self.expect("the ledger still balances", self.book_balanced(), "")
        self.stdout.write(f"  1400 ends at {dr}")

    def book_balanced(self):
        from apps.accounting.models import JournalEntryLine

        row = JournalEntryLine.objects.filter(
            journal_entry__company=self.company, journal_entry__status="POSTED",
        ).aggregate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        return (row["dr"] or Z) == (row["cr"] or Z)

    # ── 1 ──
    def check_single_billing(self, client):
        from apps.procurement.models import Bill

        po = self.order(client, "PO-FIX-1", [(self.raw, 10, "50", True)])
        line = po.lines.first()
        self.receive(client, po, [(line, 10)])

        count = Bill.objects.filter(company=self.company).count()
        resp = client.post(reverse("procurement:bill_create"),
                           merge(self.bill_form("BILL-FIX-1M", subtotal="500", tax_amount="80",
                                                total_amount="580"),
                                 {"purchase_order": po.pk}))
        self.expect("a typed bill naming an order is refused",
                    resp.status_code == 200
                    and Bill.objects.filter(company=self.company).count() == count,
                    f"HTTP {resp.status_code}")
        form = resp.context.get("form") if resp.context else None
        self.expect("the refusal points at Bill this order",
                    form is not None and "Bill this order" in " ".join(form.non_field_errors()),
                    str(form.non_field_errors() if form is not None else "no form"))

        bill = self.bill_order(client, po)
        self.expect("Bill this order bills the goods once", bill is not None
                    and bill.total_amount == D("580.00") and bill.status == "RECEIVED",
                    f"{bill and bill.total_amount} {bill and bill.status}")
        resp = client.post(reverse("procurement:po_to_bill", args=[po.pk]), {})
        po.refresh_from_db()
        self.expect("a second Bill this order finds nothing left",
                    po.bills.count() == 1 and po.status == "BILLED",
                    f"{po.bills.count()} bills, {po.status}")
        self.expect_flash(client, resp, "nothing left to bill", "and says so")
        self.stock_agrees("after a single billing")
        self.accrual_agrees("after a single billing")

        # Bill before goods: the typed bill is refused and the order cannot be
        # billed until something has arrived, so stock is counted once.
        po2 = self.order(client, "PO-FIX-1B", [(self.raw, 5, "100", True)])
        resp = client.post(reverse("procurement:bill_create"),
                           merge(self.bill_form("BILL-FIX-1B", subtotal="500", tax_amount="80",
                                                total_amount="580"),
                                 {"purchase_order": po2.pk}))
        self.expect("a bill ahead of the goods cannot name the order",
                    resp.status_code == 200 and not po2.bills.exists(), f"HTTP {resp.status_code}")
        resp = client.post(reverse("procurement:po_to_bill", args=[po2.pk]), {})
        self.expect("nor can the order be billed before anything arrives",
                    not po2.bills.exists(), str(po2.bills.count()))
        self.receive(client, po2, [(po2.lines.first(), 5)])
        bill2 = self.bill_order(client, po2)
        self.expect("the bill after the goods clears the accrual, not stock",
                    bill2 is not None
                    and self.line_total(bill2.journal_entry, "2110", "debit") == D("500.00")
                    and self.line_total(bill2.journal_entry, "1400", "debit") == Z,
                    f"2110 {bill2 and self.line_total(bill2.journal_entry, '2110', 'debit')}")
        self.stock_agrees("after bill-after-goods")
        self.accrual_agrees("after bill-after-goods")

    # ── 2 ──
    def check_posted_bill_locked(self, client):
        from apps.procurement.models import Bill

        self.post(client, "unordered bill", reverse("procurement:bill_create"),
                  self.bill_form("BILL-FIX-2"))
        bill = Bill.objects.get(company=self.company, reference="BILL-FIX-2")
        self.post(client, "part payment", reverse("procurement:bill_pay", args=[bill.pk]),
                  {"amount": "500", "gl_account": self.account("1200").pk})
        resp = client.get(reverse("procurement:bill_edit", args=[bill.pk]))
        self.expect("the edit screen is closed to a posted bill",
                    resp.status_code == 302, f"HTTP {resp.status_code}")
        resp = client.post(reverse("procurement:bill_edit", args=[bill.pk]),
                           self.bill_form("BILL-FIX-2", subtotal="300", tax_amount="48",
                                          total_amount="348"))
        bill.refresh_from_db()
        self.expect("a posted, part-paid bill keeps its figures",
                    bill.total_amount == D("1160.00") and bill.outstanding_amount == D("660.00"),
                    f"{bill.total_amount} / {bill.outstanding_amount}")
        self.expect_flash(client, resp, "debit note", "the refusal points at a debit note")

        # A draft raised from an order is still editable.
        po = self.order(client, "PO-FIX-2", [(self.raw, 2, "50", False)])
        self.receive(client, po, [(po.lines.first(), 2)])
        draft = self.bill_order(client, po, post=False)
        resp = client.post(reverse("procurement:bill_edit", args=[draft.pk]), merge(
            self.bill_form(draft.reference, subtotal="100", tax_amount="0", total_amount="100",
                           supplier_reference="SUP-DRAFT-2"),
            {"purchase_order": ""}))
        draft.refresh_from_db()
        self.expect("a draft can still be corrected before posting",
                    resp.status_code == 302 and draft.supplier_reference == "SUP-DRAFT-2",
                    f"HTTP {resp.status_code}")
        self.expect("and it stays on its order, with no account of its own",
                    draft.purchase_order_id == po.pk and draft.expense_account_id is None,
                    f"{draft.purchase_order_id} / {draft.expense_account_id}")
        self.post(client, "post the corrected draft",
                  reverse("procurement:bill_post", args=[draft.pk]), {})

    # ── 3 ──
    def check_adjustments(self, client):
        from apps.inventory.models import StockMovement

        level = self.shelf(self.raw)
        avg = level.avg_cost
        before = StockMovement.objects.filter(product=self.raw).count()
        stock0 = self.net("1400")
        resp = self.adjust(client, "DELIVERY", 5, "999")
        issue = StockMovement.objects.filter(product=self.raw, movement_type="DELIVERY").first()
        self.expect("an issue with a typed cost is still taken at the shelf average",
                    resp.status_code == 302 and issue is not None and issue.unit_cost == avg,
                    f"HTTP {resp.status_code}, {issue and issue.unit_cost} against {avg}")
        self.expect("and credits stock with quantity × average",
                    stock0 - self.net("1400") == (5 * avg).quantize(CENT),
                    f"{stock0 - self.net('1400')}")
        self.stock_agrees("after an issue with a typed cost")

        refused = [
            ("DELIVERY", 2, "-50", "an issue at a negative cost"),
            ("ADJUSTMENT_PLUS", 3, "-10", "a surplus at a negative cost"),
            ("TRANSFER_IN", 10, "50", "half a transfer, in"),
            ("TRANSFER_OUT", 4, "", "half a transfer, out"),
            ("RETURN_OUT", 2, "", "a supplier return with no bill"),
            ("PRODUCTION_IN", 2, "50", "production output with no work order"),
            ("OPENING", 2, "50", "opening stock on the adjustment screen"),
            ("RECEIPT", 2, "0", "a receipt with no cost"),
            ("ADJUSTMENT_MINUS", 100000, "", "taking out more than is on the shelf"),
            ("ADJUSTMENT_PLUS", "99999999.9999", "99999999999.9999", "a huge quantity × cost"),
            ("ADJUSTMENT_PLUS", 0, "50", "a quantity of zero"),
        ]
        for kind, qty, cost, label in refused:
            moves = StockMovement.objects.filter(product=self.raw).count()
            resp = self.adjust(client, kind, qty, cost)
            self.expect(f"{label} is refused",
                        resp.status_code == 200
                        and StockMovement.objects.filter(product=self.raw).count() == moves,
                        f"HTTP {resp.status_code}")
        resp = self.adjust(client, "ADJUSTMENT_PLUS", 1, "50", product=self.service)
        self.expect("a product that is not stock-tracked cannot be adjusted",
                    resp.status_code == 200, f"HTTP {resp.status_code}")

        level.refresh_from_db()
        resp = self.adjust(client, "ADJUSTMENT_PLUS", 3, "")
        found = StockMovement.objects.filter(
            product=self.raw, movement_type="ADJUSTMENT_PLUS").first()
        self.expect("a surplus with no cost is valued at the shelf average",
                    resp.status_code == 302 and found is not None
                    and found.unit_cost == level.avg_cost,
                    f"{found and found.unit_cost} against {level.avg_cost}")
        self.adjust(client, "ADJUSTMENT_MINUS", 1, "12345")
        self.stock_agrees("after every adjustment")
        self.expect("the refused movements wrote nothing",
                    StockMovement.objects.filter(product=self.raw).count() == before + 3,
                    str(StockMovement.objects.filter(product=self.raw).count() - before))

        # The model guards the same rules for code that bypasses the form.
        from django.core.exceptions import ValidationError
        try:
            with transaction.atomic():
                StockMovement.objects.create(
                    product=self.raw, warehouse=self.warehouse, movement_type="ADJUSTMENT_PLUS",
                    quantity=D("1"), unit_cost=D("-5"), movement_date=self.today)
            blocked = False
        except ValidationError:
            blocked = True
        self.expect("the stock model refuses a negative cost on its own", blocked)

    # ── 4 ──
    def check_price_variance(self, client):
        def priced(ref, product, qty, cost, sub, tax, total):
            po = self.order(client, ref, [(product, qty, cost, True)])
            self.receive(client, po, [(po.lines.first(), qty)])
            draft = self.bill_order(client, po, post=False)
            self.post(client, f"{ref} invoice figures",
                      reverse("procurement:bill_edit", args=[draft.pk]),
                      self.bill_form(draft.reference, subtotal=sub, tax_amount=tax,
                                     total_amount=total, supplier_reference=f"SUP-{ref}"))
            self.post(client, f"post {ref} bill",
                      reverse("procurement:bill_post", args=[draft.pk]), {})
            draft.refresh_from_db()
            po.refresh_from_db()
            return po, draft

        from apps.accounting.gl_service import accrual_remaining

        steel = self.make_product(client, "STEEL-4", "40")
        po, bill = priced("PO-FIX-4H", steel, 10, "50", "550", "88", "638")
        je = bill.journal_entry
        self.expect("a dearer bill clears exactly the accrual",
                    self.line_total(je, "2110", "debit") == D("500.00")
                    and accrual_remaining(po) == Z,
                    f"2110 {self.line_total(je, '2110', 'debit')}, left {accrual_remaining(po)}")
        self.expect("and books the extra 50 as price variance, not stock",
                    self.line_total(je, "5400", "debit") == D("50.00")
                    and self.line_total(je, "1400", "debit") == Z,
                    f"5400 {self.line_total(je, '5400', 'debit')}")
        self.expect("the shelf keeps the price the goods came in at",
                    self.shelf(steel).avg_cost == D("50.0000"), str(self.shelf(steel).avg_cost))
        self.stock_agrees("after a dearer bill")

        po, bill = priced("PO-FIX-4L", steel, 10, "50", "450", "72", "522")
        je = bill.journal_entry
        self.expect("a cheaper bill clears exactly the accrual too",
                    self.line_total(je, "2110", "debit") == D("500.00")
                    and accrual_remaining(po) == Z and po.status == "BILLED",
                    f"left {accrual_remaining(po)}, {po.status}")
        self.expect("and credits the 50 saved to price variance",
                    self.line_total(je, "5400", "credit") == D("50.00"),
                    str(self.line_total(je, "5400", "credit")))
        dr, cr, ok = self.balanced(je)
        self.expect("the variance entry balances", ok, f"Dr {dr} / Cr {cr}")
        self.stock_agrees("after a cheaper bill")
        self.accrual_agrees("after bills above and below the order")

        # A price with more places than money: the receipt and the bill round
        # differently, and the last bill still closes the accrual to the cent.
        po = self.order(client, "PO-FIX-4R", [(steel, 3, "33.3333", False)])
        self.receive(client, po, [(po.lines.first(), 3)])
        self.bill_order(client, po)
        self.expect("an awkward price leaves no cent on the accrual",
                    accrual_remaining(po) == Z, str(accrual_remaining(po)))

        # Two part bills against one order.
        po = self.order(client, "PO-FIX-4P", [(steel, 10, "50", False)])
        line = po.lines.first()
        self.receive(client, po, [(line, 4)])
        first = self.bill_order(client, po)
        self.receive(client, po, [(line, 6)])
        second = self.bill_order(client, po)
        self.expect("each part bill clears what its own quantities accrued",
                    first.accrued_cleared == D("200.00") and second.accrued_cleared == D("300.00")
                    and accrual_remaining(po) == Z,
                    f"{first.accrued_cleared} + {second.accrued_cleared}, left {accrual_remaining(po)}")
        self.stock_agrees("after part bills")
        self.accrual_agrees("after part bills")

    # ── 5 ──
    def check_debit_notes(self, client):
        from apps.accounting.gl_service import accrual_remaining
        from apps.procurement.models import Bill

        po = self.order(client, "PO-FIX-5", [(self.raw, 10, "50", True)])
        self.receive(client, po, [(po.lines.first(), 10)])
        bill = self.bill_order(client, po)
        accrual0 = self.net("2110")
        ppv0 = self.net("5400")
        vat0 = self.net("2210")
        self.post(client, "debit note on an order bill",
                  reverse("procurement:bill_debit_note", args=[bill.pk]),
                  {"amount": "116", "reason": "Two returned"})
        note = Bill.objects.filter(original_bill=bill).first()
        if not self.expect("the debit note was raised", note is not None):
            return
        self.expect("the accrual stays closed on a billed order",
                    self.net("2110") == accrual0 and accrual_remaining(po) == Z,
                    f"2110 moved {self.net('2110') - accrual0}")
        self.expect("the reduction lands on price variance",
                    ppv0 - self.net("5400") == D("100.00"), str(ppv0 - self.net("5400")))
        self.expect("and the VAT comes back at the note's own tax",
                    vat0 - self.net("2210") == note.tax_amount == D("16.00"),
                    f"{vat0 - self.net('2210')} / {note.tax_amount}")
        dr, cr, ok = self.balanced(note.journal_entry)
        self.expect("the debit note entry balances", ok, f"Dr {dr} / Cr {cr}")
        self.stock_agrees("after a debit note")
        self.accrual_agrees("after a debit note")

        # A note against a bill that was below the order price.
        cheap = Bill.objects.filter(company=self.company, purchase_order__reference="PO-FIX-4L").first()
        vat0 = self.net("2210")
        self.post(client, "debit note on a cheaper bill",
                  reverse("procurement:bill_debit_note", args=[cheap.pk]), {"amount": "116"})
        note = Bill.objects.filter(original_bill=cheap).first()
        self.expect("a note on a below-price bill still returns the tax it carries",
                    note is not None and vat0 - self.net("2210") == note.tax_amount,
                    f"{vat0 - self.net('2210')} / {note and note.tax_amount}")
        self.stock_agrees("after a note on a cheaper bill")
        self.accrual_agrees("after a note on a cheaper bill")

    # ── 6 ──
    def check_unordered_bills(self, client):
        from apps.procurement.models import Bill

        form = client.get(reverse("procurement:bill_create")).context["form"]
        choices = list(form.fields["expense_account"].queryset.values_list("code", flat=True))
        self.expect("the new-bill screen defaults to an expense account",
                    str(form.fields["expense_account"].initial) == str(self.account("6000").pk),
                    str(form.fields["expense_account"].initial))
        self.expect("and does not offer the stock account at all",
                    "1400" not in choices and "1450" not in choices and "6600" in choices,
                    ", ".join(choices[:12]))
        self.expect("an order cannot be picked on a new bill",
                    "purchase_order" not in form.fields)

        stock0 = self.net("1400")
        self.post(client, "service bill", reverse("procurement:bill_create"),
                  self.bill_form("BILL-FIX-6", expense_account=self.account("6600").pk,
                                 subtotal="300", tax_amount="48", total_amount="348"))
        bill = Bill.objects.get(company=self.company, reference="BILL-FIX-6")
        je = bill.journal_entry
        self.expect("a service bill is charged to the account chosen",
                    je is not None and self.line_total(je, "6600", "debit") == D("300.00")
                    and self.line_total(je, "1400", "debit") == Z,
                    f"6600 {je and self.line_total(je, '6600', 'debit')}")
        self.expect("and inventory does not move", self.net("1400") == stock0,
                    f"{self.net('1400')} against {stock0}")
        self.reject(client, "a bill coded straight to inventory",
                    reverse("procurement:bill_create"),
                    self.bill_form("BILL-FIX-6B", expense_account=self.account("1400").pk))
        self.stock_agrees("after a service bill")

        # Goods taken in by hand with no order wait on 2110; the supplier's bill,
        # coded to that account, clears them instead of leaving them there.
        accrual0 = self.net("2110")
        resp = self.adjust(client, "RECEIPT", 2, "60")
        self.expect("a receipt with no order accrues on 2110",
                    resp.status_code == 302 and accrual0 - self.net("2110") == D("120.00"),
                    f"HTTP {resp.status_code}, {accrual0 - self.net('2110')}")
        self.post(client, "bill for the unordered goods", reverse("procurement:bill_create"),
                  self.bill_form("BILL-FIX-6R", expense_account=self.account("2110").pk,
                                 subtotal="120", tax_amount="19.20", total_amount="139.20"))
        unordered = Bill.objects.get(company=self.company, reference="BILL-FIX-6R")
        self.expect("its bill clears the accrual and says so",
                    self.net("2110") == accrual0 and unordered.accrued_cleared == D("120.00"),
                    f"2110 {self.net('2110')} against {accrual0}, cleared {unordered.accrued_cleared}")
        self.stock_agrees("after an unordered receipt and its bill")

        # A debit note on it gives the expense back.
        exp0 = self.net("6600")
        self.post(client, "note on the service bill",
                  reverse("procurement:bill_debit_note", args=[bill.pk]), {"amount": "116"})
        self.expect("a note on a service bill credits the same expense",
                    exp0 - self.net("6600") == D("100.00"), str(exp0 - self.net("6600")))

    # ── 7 ──
    def check_manufacturing(self, client):
        from apps.inventory.models import StockMovement
        from apps.manufacturing.models import BillOfMaterials, WorkOrder

        part = self.make_product(client, "PART-7", "50")
        made = self.make_product(client, "FG-7", "500")
        po = self.order(client, "PO-FIX-7", [(part, 10, "80", False)])
        self.receive(client, po, [(po.lines.first(), 10)])
        self.bill_order(client, po)
        self.post(client, "recipe", reverse("manufacturing:bom_create"), merge({
            "name": f"FG recipe {self.stamp}", "product": made.pk, "version": "1",
            "status": "ACTIVE", "production_quantity": "1", "notes": "",
        }, formset("lines", [
            {"component": part.pk, "quantity": "2", "unit_of_measure": "", "scrap_percent": "0"},
        ], blank={"component": "", "quantity": "", "unit_of_measure": "", "scrap_percent": "0"})))
        bom = BillOfMaterials.objects.get(company=self.company, product=made)
        self.post(client, "work order", reverse("manufacturing:work_order_create"), {
            "reference": f"WO-FIX-{self.stamp}", "bom": bom.pk, "planned_quantity": "3",
            "planned_start": str(self.today), "planned_end": "",
            "warehouse": self.warehouse.pk, "notes": ""})
        wo = WorkOrder.objects.get(company=self.company, reference=f"WO-FIX-{self.stamp}")
        wip0 = self.net("1450")
        self.post(client, "complete the work order",
                  reverse("manufacturing:work_order_complete", args=[wo.pk]),
                  {"actual_quantity": "3", "actual_end": str(self.today)})
        wo.refresh_from_db()
        issue = StockMovement.objects.filter(product=part, movement_type="PRODUCTION_OUT").first()
        self.expect("components are drawn at the shelf average, not the card",
                    issue is not None and issue.unit_cost == D("80.0000"),
                    f"{issue and issue.unit_cost}")
        self.expect("the finished goods come in at what they consumed",
                    self.shelf(made).quantity == D("3.0000")
                    and self.shelf(made).avg_cost == D("160.0000"),
                    f"{self.shelf(made).quantity} @ {self.shelf(made).avg_cost}")
        self.expect("work in progress is empty again", self.net("1450") == wip0,
                    f"{self.net('1450')} against {wip0}")
        self.expect("the order records its cost", wo.status == "COMPLETED" and wo.cost == D("480.00"),
                    f"{wo.status} / {wo.cost}")
        self.stock_agrees("after production")

        # A batch whose cost does not divide evenly still empties 1450.
        self.post(client, "odd work order", reverse("manufacturing:work_order_create"), {
            "reference": f"WO-FIX-ODD-{self.stamp}", "bom": bom.pk, "planned_quantity": "1",
            "planned_start": str(self.today), "planned_end": "",
            "warehouse": self.warehouse.pk, "notes": ""})
        odd = WorkOrder.objects.get(company=self.company, reference=f"WO-FIX-ODD-{self.stamp}")
        self.post(client, "complete it as three",
                  reverse("manufacturing:work_order_complete", args=[odd.pk]),
                  {"actual_quantity": "3", "actual_end": str(self.today)})
        self.expect("an uneven batch leaves no cents in work in progress",
                    self.net("1450") == wip0, f"{self.net('1450')}")
        self.stock_agrees("after an uneven batch")

        self.post(client, "a work order bigger than the shelf",
                  reverse("manufacturing:work_order_create"), {
                      "reference": f"WO-FIX-BIG-{self.stamp}", "bom": bom.pk,
                      "planned_quantity": "50", "planned_start": str(self.today),
                      "planned_end": "", "warehouse": self.warehouse.pk, "notes": ""})
        big = WorkOrder.objects.get(company=self.company, reference=f"WO-FIX-BIG-{self.stamp}")
        moves = StockMovement.objects.filter(reference=big.reference).count()
        resp = client.post(reverse("manufacturing:work_order_complete", args=[big.pk]),
                           {"actual_quantity": "50", "actual_end": str(self.today)})
        big.refresh_from_db()
        self.expect("completing with short stock is a message, not a server error",
                    resp.status_code == 302 and big.status == "DRAFT" and moves == 0
                    and not StockMovement.objects.filter(reference=big.reference).exists(),
                    f"HTTP {resp.status_code}, {big.status}")
        self.expect_flash(client, resp, "insufficient stock", "the message names the shortage")
        self.post(client, "a small work order", reverse("manufacturing:work_order_create"), {
            "reference": f"WO-FIX-ZERO-{self.stamp}", "bom": bom.pk, "planned_quantity": "1",
            "planned_start": str(self.today), "planned_end": "",
            "warehouse": self.warehouse.pk, "notes": ""})
        small = WorkOrder.objects.get(company=self.company, reference=f"WO-FIX-ZERO-{self.stamp}")
        for junk in ("0", "-1", "NaN", "1e999999"):
            resp = client.post(reverse("manufacturing:work_order_complete", args=[small.pk]),
                               {"actual_quantity": junk, "actual_end": str(self.today)})
            self.expect(f"an output of '{junk}' is refused with a message",
                        resp.status_code == 302
                        and WorkOrder.objects.get(pk=small.pk).status == "DRAFT"
                        and not StockMovement.objects.filter(reference=small.reference).exists(),
                        f"HTTP {resp.status_code}")
        self.expect_flash(client, resp, "not completed", "the refusal says the order was not completed")

    # ── 8 ──
    def check_junk(self, client):
        from apps.inventory.models import StockLevel, StockMovement
        from apps.organizations.models import Warehouse
        from apps.procurement.models import Bill, PurchaseOrderLine

        bill = Bill.objects.get(company=self.company, reference="BILL-FIX-2")
        for junk in JUNK:
            resp = client.post(reverse("procurement:bill_debit_note", args=[bill.pk]),
                               {"amount": junk})
            self.no_crash(f"a debit note of '{junk}'", resp)
        self.expect("no junk debit note was written",
                    not Bill.objects.filter(original_bill=bill).exists())

        po = self.order(client, "PO-FIX-8", [], send=False)
        for junk in JUNK:
            self.no_crash(f"an order line quantity of '{junk}'", client.post(
                reverse("procurement:po_line_add", args=[po.pk]),
                {"product": self.raw.pk, "quantity": junk, "unit_cost": "5"}))
            self.no_crash(f"an order line cost of '{junk}'", client.post(
                reverse("procurement:po_line_add", args=[po.pk]),
                {"product": self.raw.pk, "quantity": "1", "unit_cost": junk}))
        self.no_crash("an order line for product 'zzz'", client.post(
            reverse("procurement:po_line_add", args=[po.pk]),
            {"product": "zzz", "quantity": "1", "unit_cost": "5"}))
        self.no_crash("an order line with tax rate 'zzz'", client.post(
            reverse("procurement:po_line_add", args=[po.pk]),
            {"product": self.raw.pk, "quantity": "1", "unit_cost": "5", "tax_rate": "zzz"}))
        self.expect("no junk line reached the order",
                    not PurchaseOrderLine.objects.filter(order=po).exists(),
                    str(PurchaseOrderLine.objects.filter(order=po).count()))

        self.post(client, "a real line", reverse("procurement:po_line_add", args=[po.pk]),
                  {"product": self.raw.pk, "quantity": "2", "unit_cost": "50", "tax_rate": ""})
        self.post(client, "send it", reverse("procurement:po_send", args=[po.pk]), {})
        line = po.lines.first()
        for junk in JUNK + ("-5",):
            self.no_crash(f"a receipt of '{junk}'", client.post(
                reverse("procurement:grn_record", args=[po.pk]), {f"receive_qty_{line.pk}": junk}))
        self.no_crash("a receipt into warehouse 'zzz'", client.post(
            reverse("procurement:grn_record", args=[po.pk]),
            {f"receive_qty_{line.pk}": "1", "warehouse": "zzz"}))
        line.refresh_from_db()
        self.expect("no junk receipt was taken in", line.received_quantity == Z,
                    str(line.received_quantity))

        other = Warehouse.objects.create(company=self.company, name="Fix Depot", code=f"FX{self.stamp[-4:]}")
        transfer = reverse("inventory:stock_transfer")
        base = {"from_warehouse": self.warehouse.pk, "to_warehouse": other.pk,
                "product": self.raw.pk, "quantity": "1", "reference": ""}
        moves = StockMovement.objects.filter(product=self.raw).count()
        for junk in JUNK:
            self.no_crash(f"a transfer of '{junk}'", client.post(transfer, merge(base, {"quantity": junk})))
        for junk in ("", "zzz"):
            self.no_crash(f"a transfer from warehouse '{junk}'",
                          client.post(transfer, merge(base, {"from_warehouse": junk})))
            self.no_crash(f"a transfer to warehouse '{junk}'",
                          client.post(transfer, merge(base, {"to_warehouse": junk})))
        self.no_crash("a transfer of product 'zzz'", client.post(transfer, merge(base, {"product": "zzz"})))
        self.no_crash("a transfer dated 'garbage'",
                      client.post(transfer, merge(base, {"movement_date": "garbage"})))
        self.expect("no junk transfer moved stock",
                    StockMovement.objects.filter(product=self.raw).count() == moves)
        avg = self.shelf(self.raw).avg_cost
        self.post(client, "a real transfer", transfer, merge(base, {"quantity": "2"}))
        away = self.shelf(self.raw, other)
        self.expect("a transfer arrives at the sending shelf's average",
                    away is not None and away.quantity == D("2.0000") and away.avg_cost == avg,
                    f"{away and away.quantity} @ {away and away.avg_cost} against {avg}")
        self.stock_agrees("after a transfer")

        level = StockLevel.objects.get(product=self.raw, warehouse=self.warehouse)
        count = reverse("inventory:inventory_count", args=[self.warehouse.pk])
        for junk in JUNK:
            resp = client.post(count, {f"count_{level.pk}": junk, "movement_date": str(self.today)})
            self.no_crash(f"a count of '{junk}'", resp)
            self.expect_flash(client, resp, "count not posted", f"a count of '{junk}' is reported")
        self.no_crash("a count dated 'garbage'", client.post(
            count, {f"count_{level.pk}": "1", "movement_date": "garbage"}))
        self.expect("no junk count moved stock",
                    StockMovement.objects.filter(product=self.raw).count() == moves + 2)

        resp = self.adjust(client, "ADJUSTMENT_PLUS", "99999999.9999", "99999999999.9999")
        self.no_crash("an adjustment worth more than the ledger holds", resp)
        for junk in JUNK:
            self.no_crash(f"an adjustment quantity of '{junk}'", self.adjust(client, "ADJUSTMENT_PLUS", junk, "5"))
            self.no_crash(f"an adjustment cost of '{junk}'", self.adjust(client, "ADJUSTMENT_PLUS", 1, junk))

        payable = Bill.objects.get(company=self.company, reference="BILL-FIX-2")
        paid = payable.paid_amount
        for junk in ("abc", "zzz", "00000000-0000-0000-0000-000000000000"):
            resp = client.post(reverse("procurement:bill_pay", args=[payable.pk]),
                               {"amount": "10", "gl_account": junk})
            self.no_crash(f"a payment from account '{junk}'", resp)
        payable.refresh_from_db()
        self.expect("no payment was taken from a junk account", payable.paid_amount == paid,
                    f"{payable.paid_amount} against {paid}")
        # A typed bill is posted as it is saved, so a draft comes from an order.
        po2 =self.order(client, "PO-FIX-8B", [(self.raw, 1, "50", False)])
        self.receive(client, po2, [(po2.lines.first(), 1)])
        draft = self.bill_order(client, po2, post=False)
        client.post(reverse("procurement:bill_pay", args=[draft.pk]),
                    {"amount": "10", "gl_account": "abc"})
        draft.refresh_from_db()
        self.expect("a junk account does not post the draft on the way",
                    draft.journal_entry_id is None and draft.status == "DRAFT", draft.status)
        self.post(client, "post it after all", reverse("procurement:bill_post", args=[draft.pk]), {})

    # ── 9 ──
    def check_links(self, client):
        from apps.procurement.models import Bill

        po = self.order(client, "PO-FIX-9", [(self.raw, 1, "50", False)])
        self.receive(client, po, [(po.lines.first(), 1)])
        draft = self.bill_order(client, po, post=False)
        self.reject(client, "an order's draft moved to another supplier",
                    reverse("procurement:bill_edit", args=[draft.pk]),
                    self.bill_form(draft.reference, supplier=self.other_supplier.pk,
                                   subtotal="50", tax_amount="0", total_amount="50"))
        draft.refresh_from_db()
        self.expect("the draft stays with the order's supplier",
                    draft.supplier_id == self.supplier.pk)
        self.post(client, "post it", reverse("procurement:bill_post", args=[draft.pk]), {})

        cancelled = self.order(client, "PO-FIX-9C", [(self.raw, 1, "50", False)])
        self.post(client, "cancel the order", reverse("procurement:po_cancel", args=[cancelled.pk]), {})
        cancelled.refresh_from_db()
        self.expect("the order is cancelled", cancelled.status == "CANCELLED", cancelled.status)
        self.reject(client, "a bill naming a cancelled order",
                    reverse("procurement:bill_create"),
                    merge(self.bill_form("BILL-FIX-9C"), {"purchase_order": cancelled.pk}))
        self.reject(client, "a bill for one supplier naming another's order",
                    reverse("procurement:bill_create"),
                    merge(self.bill_form("BILL-FIX-9X", supplier=self.other_supplier.pk),
                          {"purchase_order": po.pk}))
        resp = self.reject(client, "a bill due before it was issued",
                           reverse("procurement:bill_create"),
                           self.bill_form("BILL-FIX-9D",
                                          due_date=str(self.today - datetime.timedelta(days=10))))
        form = resp.context.get("form") if resp.context else None
        self.expect("the due date refusal says why",
                    form is not None and "before the bill date" in " ".join(form.errors.get("due_date", [])),
                    str(form.errors if form is not None else "no form"))
        self.expect("none of the refused bills exist",
                    not Bill.objects.filter(company=self.company,
                                            reference__in=["BILL-FIX-9C", "BILL-FIX-9X",
                                                           "BILL-FIX-9D"]).exists())

        received = self.order(client, "PO-FIX-9R", [(self.raw, 4, "50", False)])
        self.receive(client, received, [(received.lines.first(), 2)])
        resp = self.post(client, "cancel a part-received order",
                         reverse("procurement:po_cancel", args=[received.pk]), {})
        received.refresh_from_db()
        self.expect("an order with goods received cannot be cancelled",
                    received.status == "PARTIALLY_RECEIVED", received.status)
        self.bill_order(client, received)
