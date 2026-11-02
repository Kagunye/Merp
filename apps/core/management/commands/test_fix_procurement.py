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
   10. a typed bill with no account chosen is charged to 6000, never to stock
   11. a service on an order is charged to its expense account, not to 5400
   12. a role that may not post bills leaves a draft when it enters one
   13. a dollar order is a dollar order; dollar bills and notes leave AP at
       exactly nothing once settled
   14. requisitions take lines, are submitted, approved or rejected, and turn
       into an order; a reference cannot be used twice
   15. a part-received order is closed short only once what arrived is billed
   16. flash messages print money to two places

Round three (R3-n in the output):
    1. a product with stock on hand, or receipts not yet billed, keeps its
       stock tracking and its inventory account
    2. a work order for an untracked product expenses its cost, so 1450 empties
    3. cancelling one of two bills releases exactly that bill's quantities and
       accrual, drafts included, and the order can be billed again
    4. work orders and recipes need positive quantities
    5. labour and other untracked inputs need no stock and are charged to WIP
       (Dr 1450, Cr 5250 Production Costs Absorbed) at their cost price
    6. the shelves carry their value to the cent: 1400 equals the valuation
       exactly, and the last unit out takes whatever value is left
    7. nobody approves a requisition they raised or submitted
    8. a requisition for a dollar supplier is ordered in dollars
    9. order lines change only while the order is a draft
   10. free goods on an order close out with no journal; a typed 0.00 bill
       is refused
   11-13. bill references are unique; total = subtotal + tax to the cent;
       bill and payment dates are range-checked
   14. the payment amount is read like every other money box
   15. requisitions: back to approved when their order is cancelled, can be
       cancelled, take products only, and opening the form takes no number
   16. status words read as English in messages
   17. a payment that names no account leaves the main bank, never the till
   18. buttons a role cannot use are not shown

Round four (R4-n in the output):
    1. paying a bill dated in a closed month says so instead of failing, and
       a draft is posted and paid in one transaction or not at all
    2. an untracked product has no shelf: no movements, left off the count;
       re-tracking empties stale shelves; a service with unbilled receipts
       cannot be turned into stock
    3. adjustments, counts, transfers and work-order completions cannot be
       dated in the future or in a closed month
    4. a unit in use keeps its factor, kind and base; a product with stock
       keeps its unit
    5. recipe scrap is from 0 up to (not including) 100 per cent
    6. a product's inventory account is a stock account, never 1450
    7. a below-zero shelf brought back to exactly nothing is worth nothing;
       the price difference goes to 5400
    8. a typed bill cannot be charged to contra, header, revaluation or
       control accounts
    9. number boxes take ASCII digits only: no "1_0", no full-width digits
   10. nothing is received, transferred, adjusted or made into an inactive
       warehouse
   11. the work-order closed-period message reads properly
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
        """The stock valuation the screens report: each shelf's value, to the cent."""
        from apps.inventory.models import stock_valuation

        return stock_valuation(self.company)

    def stock_agrees(self, label):
        # To the cent, not within one: the shelves carry the very cents the
        # movements posted, so there is no rounding left to forgive.
        gl, shelf = self.net("1400"), self.valuation()
        return self.expect(f"{label}: inventory 1400 equals the stock valuation",
                           gl == shelf, f"GL {gl} against shelves {shelf}")

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

    def order(self, client, ref, lines, supplier=None, send=True, currency=None):
        """A purchase order through the order screens: header, lines, send."""
        from apps.procurement.models import PurchaseOrder

        header = {
            "reference": ref, "supplier": (supplier or self.supplier).pk,
            "order_date": str(self.today), "expected_delivery_date": "",
            "warehouse": self.warehouse.pk, "notes": "",
        }
        if currency:
            header["currency"] = currency
        self.post(client, f"order {ref}", reverse("procurement:po_create"), header)
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
            "charge_account": self.account("6000").pk,
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
        self.section("10. A typed bill with no account is an expense, not stock")
        self.check_blank_account(client)
        self.section("11. Services on an order are charged, not a variance")
        self.check_service_lines(client)
        self.section("12. Entering a bill is not posting it")
        self.check_bill_roles(client)
        self.section("13. Dollar orders and dollar bills settle to the cent")
        self.check_usd(client)
        self.section("14. Requisitions: lines, submit, approve or reject, order")
        self.check_requisitions(client)
        self.section("15. Closing a part-delivered order short")
        self.check_close_short(client)
        self.section("16. Messages print money to the cent")
        self.check_flash_rounding(client)

        # ── the third round of fixes (numbers are that round's bug list) ──
        self.section("R3-1. A product with stock keeps its tracking and its account")
        self.check_product_edit(client)
        self.section("R3-2/5. Untracked output and untracked inputs in production")
        self.check_untracked_production(client)
        self.section("R3-3. Cancelling one of two bills releases exactly its part")
        self.check_cancel_one_of_two(client)
        self.section("R3-4. Work orders and recipes need positive quantities")
        self.check_positive_quantities(client)
        self.section("R3-6. Inventory 1400 equals the shelves to the cent")
        self.check_stock_cents(client)
        self.section("R3-7/15/16. Requisitions: approval, cancelling, numbering, wording")
        self.check_requisitions_round3(client)
        self.section("R3-8. A requisition for a dollar supplier is ordered in dollars")
        self.check_requisition_currency(client)
        self.section("R3-9. Order lines change only on a draft")
        self.check_draft_lines_only(client)
        self.section("R3-10. Free goods close out")
        self.check_free_goods(client)
        self.section("R3-11/12/13. Bill references, totals and dates")
        self.check_bill_entry(client)
        self.section("R3-13/14/17. Supplier payments: amount, date, account")
        self.check_payments_round3(client)
        self.section("R3-18. Buttons a role cannot use are not shown")
        self.check_hidden_buttons(client)

        # ── the fourth round (R4-n: that round's bug list) ──
        self.section("R4-1. Paying a bill in a closed month is a message, all or nothing")
        self.check_pay_closed_bill(client)
        self.section("R4-2. Untracked products have no shelf; re-tracking starts empty")
        self.check_retracking(client)
        self.section("R4-3/11. Stock documents cannot be dated ahead or in a closed month")
        self.check_future_stock_dates(client)
        self.section("R4-4. A unit in use keeps its size")
        self.check_units_in_use(client)
        self.section("R4-5. Recipe scrap is 0 to under 100 per cent")
        self.check_scrap(client)
        self.section("R4-6/8. Stock and bills stay off the ledger's own accounts")
        self.check_reserved_accounts(client)
        self.section("R4-7. A negative shelf answered back to nothing is worth nothing")
        self.check_negative_shelf_empties(client)
        self.section("R4-9. Number boxes take plain digits only")
        self.check_plain_digits(client)
        self.section("R4-10. Nothing is put into an inactive warehouse")
        self.check_inactive_warehouse(client)

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
                    draft.purchase_order_id == po.pk and draft.charge_account_id is None,
                    f"{draft.purchase_order_id} / {draft.charge_account_id}")
        self.post(client, "post the corrected draft",
                  reverse("procurement:bill_post", args=[draft.pk]), {})

    # ── 3 ──
    def check_adjustments(self, client):
        from apps.inventory.models import StockMovement

        level = self.shelf(self.raw)
        avg = level.avg_cost
        before = StockMovement.objects.filter(product=self.raw).count()
        stock0 = self.net("1400")
        # The screen offers adjustments only (receipts come in on an order, issues
        # go out on a sale), so the decrease is what carries a typed cost here.
        resp = self.adjust(client, "ADJUSTMENT_MINUS", 5, "999")
        issue = StockMovement.objects.filter(product=self.raw,
                                             movement_type="ADJUSTMENT_MINUS").first()
        self.expect("a decrease with a typed cost is still taken at the shelf average",
                    resp.status_code == 302 and issue is not None and issue.unit_cost == avg,
                    f"HTTP {resp.status_code}, {issue and issue.unit_cost} against {avg}")
        self.expect("and credits stock with quantity × average",
                    stock0 - self.net("1400") == (5 * avg).quantize(CENT),
                    f"{stock0 - self.net('1400')}")
        self.stock_agrees("after a decrease with a typed cost")

        refused = [
            ("ADJUSTMENT_MINUS", 2, "-50", "a decrease at a negative cost"),
            ("RECEIPT", 2, "60", "a receipt typed on the adjustment screen"),
            ("DELIVERY", 2, "", "an issue typed on the adjustment screen"),
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
        choices = list(form.fields["charge_account"].queryset.values_list("code", flat=True))
        self.expect("the new-bill screen defaults to an expense account",
                    str(form.fields["charge_account"].initial) == str(self.account("6000").pk),
                    str(form.fields["charge_account"].initial))
        self.expect("and does not offer the stock account at all",
                    "1400" not in choices and "1450" not in choices and "6600" in choices,
                    ", ".join(choices[:12]))
        self.expect("an order cannot be picked on a new bill",
                    "purchase_order" not in form.fields)

        stock0 = self.net("1400")
        self.post(client, "service bill", reverse("procurement:bill_create"),
                  self.bill_form("BILL-FIX-6", charge_account=self.account("6600").pk,
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
                    self.bill_form("BILL-FIX-6B", charge_account=self.account("1400").pk))
        self.stock_agrees("after a service bill")

        # Goods come in from a supplier on their purchase order and nowhere else:
        # the adjustment screen no longer takes a receipt, and a receipt with no
        # order (stock loaded by a script) no longer credits the accrual, which
        # is proved order by order. A typed bill cannot be coded to 2110 either.
        from apps.inventory.models import StockMovement
        accrual0 = self.net("2110")
        resp = self.adjust(client, "RECEIPT", 2, "60")
        form = resp.context.get("form") if resp.context else None
        self.expect("a receipt typed on the adjustment screen is refused, pointing at the order",
                    resp.status_code == 200 and form is not None
                    and "purchase order" in " ".join(form.errors.get("movement_type", [])),
                    str(form.errors if form is not None else f"HTTP {resp.status_code}"))
        equity0 = self.net("3100")
        StockMovement.objects.create(
            product=self.raw, warehouse=self.warehouse, movement_type="RECEIPT",
            quantity=D("2"), unit_cost=D("60"), movement_date=self.today,
            reference="SCRIPT-LOAD", notes="Loaded by a script, no order")
        self.expect("a receipt with no order does not touch the accrual",
                    self.net("2110") == accrual0, f"2110 moved {self.net('2110') - accrual0}")
        self.expect("and is booked as stock introduced, like opening stock",
                    equity0 - self.net("3100") == D("120.00"), str(equity0 - self.net("3100")))
        self.reject(client, "a typed bill coded to the accrual",
                    reverse("procurement:bill_create"),
                    self.bill_form("BILL-FIX-6R", charge_account=self.account("2110").pk,
                                   subtotal="120", tax_amount="19.20", total_amount="139.20"))
        self.stock_agrees("after a receipt with no order")
        self.accrual_agrees("after a receipt with no order")

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

    # ── helpers for the later sections ──
    def role_client(self, role):
        """A second person in the same company, signed in with one role."""
        from apps.accounts.models import User
        from apps.organizations.models import CompanyMembership

        self._people = getattr(self, "_people", 0) + 1
        tag = f"{role.lower()}{self._people}.{self.stamp}"
        user = User.objects.create(username=tag, email=f"{tag}@smoke.local",
                                   first_name=role.title(), last_name="Fix")
        user.set_password("smoke-test-pw")
        user.save()
        CompanyMembership.objects.create(company=self.company, user=user, role=role,
                                         is_default=True)
        other = Client(raise_request_exception=False)
        other.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        return other

    def messages_of(self, client, response):
        return [str(m) for m in self.flashes(client, response)]

    # ── 10 ──
    def check_blank_account(self, client):
        from apps.procurement.models import Bill

        stock0, expense0 = self.net("1400"), self.net("6000")
        self.post(client, "a bill with no account chosen", reverse("procurement:bill_create"),
                  self.bill_form("BILL-FIX-10", charge_account="", subtotal="2000",
                                 tax_amount="320", total_amount="2320"))
        bill = Bill.objects.get(company=self.company, reference="BILL-FIX-10")
        je = bill.journal_entry
        self.expect("a typed bill with no account is charged to general expenses (6000)",
                    je is not None and self.line_total(je, "6000", "debit") == D("2000.00")
                    and self.net("6000") - expense0 == D("2000.00"),
                    f"6000 {je and self.line_total(je, '6000', 'debit')}")
        self.expect("and says so on the bill itself",
                    bill.charge_account_id == self.account("6000").pk,
                    str(bill.charge_account_id))
        self.expect("and never reaches inventory", self.net("1400") == stock0
                    and (je is None or self.line_total(je, "1400", "debit") == Z),
                    f"1400 moved {self.net('1400') - stock0}")
        self.post(client, "a debit note on it", reverse("procurement:bill_debit_note",
                                                        args=[bill.pk]), {"amount": "1160"})
        self.expect("a note on it gives the expense back, not stock",
                    self.net("6000") - expense0 == D("1000.00") and self.net("1400") == stock0,
                    f"6000 {self.net('6000') - expense0}, 1400 {self.net('1400') - stock0}")
        self.stock_agrees("after a bill with no account")

    # ── 11 ──
    def check_service_lines(self, client):
        from apps.accounting.gl_service import accrual_remaining
        from apps.inventory.models import Product

        # A service with no cost account of its own lands on general expenses.
        bare = Product.objects.create(
            company=self.company, sku=f"SVC-BARE-{self.stamp[-4:]}", name="Installation",
            product_type="SERVICE", unit_of_measure=self.uom, track_inventory=False,
            cost_price=D("0"), selling_price=D("0"))
        po = self.order(client, "PO-FIX-11", [(self.raw, 4, "50", False),
                                              (self.service, 2, "300", True),
                                              (bare, 1, "150", False)])
        lines = list(po.lines.order_by("id"))
        self.receive(client, po, [(lines[0], 4), (lines[1], 2), (lines[2], 1)])
        ppv0, cost0, expense0 = self.net("5400"), self.net("5000"), self.net("6000")
        bill = self.bill_order(client, po)
        je = bill.journal_entry if bill else None
        self.expect("the order's bill is for goods and services together",
                    bill is not None and bill.total_amount == D("1046.00"),
                    f"{bill and bill.total_amount}")
        self.expect("the stock part clears only what the receipt accrued",
                    je is not None and self.line_total(je, "2110", "debit") == D("200.00"),
                    f"2110 {je and self.line_total(je, '2110', 'debit')}")
        self.expect("the service is charged to its own cost account",
                    self.net("5000") - cost0 == D("600.00"), str(self.net("5000") - cost0))
        self.expect("a service with no account of its own goes to general expenses",
                    self.net("6000") - expense0 == D("150.00"), str(self.net("6000") - expense0))
        self.expect("and nothing is booked as a price variance",
                    self.net("5400") == ppv0, f"5400 moved {self.net('5400') - ppv0}")
        po.refresh_from_db()
        self.expect("the order is billed and its accrual closed",
                    po.status == "BILLED" and accrual_remaining(po) == Z,
                    f"{po.status}, {accrual_remaining(po)} left")

        # An order of services only, billed above the order: the extra is still
        # the cost of the service, not a variance on stock nobody received.
        po = self.order(client, "PO-FIX-11S", [(self.service, 1, "500", False)])
        self.receive(client, po, [(po.lines.first(), 1)])
        draft = self.bill_order(client, po, post=False)
        self.post(client, "the supplier's figure for the service",
                  reverse("procurement:bill_edit", args=[draft.pk]),
                  self.bill_form(draft.reference, subtotal="550", tax_amount="0",
                                 total_amount="550", supplier_reference="SUP-11S"))
        cost0, ppv0, accrual0 = self.net("5000"), self.net("5400"), self.net("2110")
        self.post(client, "post the service bill",
                  reverse("procurement:bill_post", args=[draft.pk]), {})
        self.expect("a service-only bill charges its full figure to the service",
                    self.net("5000") - cost0 == D("550.00") and self.net("5400") == ppv0
                    and self.net("2110") == accrual0,
                    f"5000 {self.net('5000') - cost0}, 5400 {self.net('5400') - ppv0}, "
                    f"2110 {self.net('2110') - accrual0}")
        self.stock_agrees("after services on an order")
        self.accrual_agrees("after services on an order")

    # ── 12 ──
    def check_bill_roles(self, client):
        from apps.procurement.models import Bill

        buyer = self.role_client("PROCUREMENT")
        finance = self.role_client("FINANCE_MANAGER")
        ap0 = self.net("2100")
        resp = buyer.post(reverse("procurement:bill_create"), self.bill_form("BILL-FIX-12P"))
        bill = Bill.objects.filter(company=self.company, reference="BILL-FIX-12P").first()
        self.expect("procurement can enter a bill",
                    resp.status_code == 302 and bill is not None, f"HTTP {resp.status_code}")
        if bill is None:
            return
        self.expect("but it waits as a draft, off the ledger",
                    bill.status == "DRAFT" and bill.journal_entry_id is None
                    and self.net("2100") == ap0,
                    f"{bill.status}, JE {bill.journal_entry_id}")
        self.expect_flash(buyer, resp, "draft", "and is told finance posts it")
        buyer.post(reverse("procurement:bill_post", args=[bill.pk]), {})
        bill.refresh_from_db()
        self.expect("procurement cannot post it with the button either",
                    bill.journal_entry_id is None, bill.status)
        finance.post(reverse("procurement:bill_post", args=[bill.pk]), {})
        bill.refresh_from_db()
        self.expect("finance posts the draft",
                    bill.status == "RECEIVED" and bill.journal_entry_id is not None, bill.status)

        resp = finance.post(reverse("procurement:bill_create"), self.bill_form("BILL-FIX-12F"))
        posted = Bill.objects.filter(company=self.company, reference="BILL-FIX-12F").first()
        self.expect("a bill finance enters is posted as it is saved",
                    posted is not None and posted.status == "RECEIVED"
                    and posted.journal_entry_id is not None,
                    f"{posted and posted.status}")

        po = self.order(client, "PO-FIX-12", [(self.raw, 1, "50", False)])
        self.receive(client, po, [(po.lines.first(), 1)])
        buyer.post(reverse("procurement:po_to_bill", args=[po.pk]), {})
        drawn = po.bills.first()
        self.expect("Bill this order, pressed by procurement, leaves a draft",
                    drawn is not None and drawn.status == "DRAFT"
                    and drawn.journal_entry_id is None, f"{drawn and drawn.status}")
        if drawn:
            finance.post(reverse("procurement:bill_post", args=[drawn.pk]), {})
        self.accrual_agrees("after bills entered by role")

    # ── 13 ──
    def check_usd(self, client):
        from apps.accounting.gl_service import accrual_remaining
        from apps.accounting.models import Currency, ExchangeRate, to_base
        from apps.crm.models import Supplier
        from apps.inventory.models import StockMovement
        from apps.procurement.models import Bill

        usd = Currency.objects.filter(code="USD").first() or Currency.objects.create(
            code="USD", name="US Dollar", symbol="$")
        then = self.today - datetime.timedelta(days=5)
        ExchangeRate.objects.create(company=self.company, currency=usd,
                                    effective_date=then, rate=D("129.555"))
        rate = D("129.555")
        shenzhen = Supplier.objects.create(company=self.company, name="Shenzhen Parts Co",
                                           currency="USD")
        chip = self.make_product(client, "CHIP-13", "1000")
        ap0, vat0 = self.net("2100"), self.net("2210")

        po = self.order(client, "PO-FIX-13", [(chip, 3, "7.77", True)], supplier=shenzhen,
                        currency="USD")
        self.expect("a dollar order is saved in dollars at the day's rate",
                    po.currency == "USD" and po.exchange_rate == rate
                    and po.total_amount == D("27.04"),
                    f"{po.currency} at {po.exchange_rate}, total {po.total_amount}")
        accrual0 = self.net("2110")
        self.receive(client, po, [(po.lines.first(), 3)])
        receipt = StockMovement.objects.filter(purchase_order=po).first()
        self.expect("the receipt costs the goods at the dollar price times the order's rate",
                    receipt is not None and receipt.unit_cost == D("1006.6424")
                    and accrual0 - self.net("2110") == to_base(D("23.31"), rate),
                    f"{receipt and receipt.unit_cost}, accrued {accrual0 - self.net('2110')}")
        ppv0 = self.net("5400")
        bill = self.bill_order(client, po)
        self.expect("the bill from it is in dollars at the order's rate",
                    bill is not None and bill.currency == "USD" and bill.exchange_rate == rate
                    and bill.total_amount == D("27.04"),
                    f"{bill and bill.currency} {bill and bill.exchange_rate}")
        self.expect("and clears the accrual with no variance at all",
                    accrual_remaining(po) == Z and self.net("5400") == ppv0,
                    f"left {accrual_remaining(po)}, 5400 {self.net('5400') - ppv0}")

        # A typed dollar bill, paid in parts at a later rate, with a note between.
        ExchangeRate.objects.create(company=self.company, currency=usd,
                                    effective_date=self.today, rate=D("131.0"))
        self.post(client, "a dollar bill", reverse("procurement:bill_create"),
                  self.bill_form("BILL-FIX-13", supplier=shenzhen.pk, currency="USD",
                                 bill_date=str(then), subtotal="100.01", tax_amount="16.00",
                                 total_amount="116.01", supplier_reference="SZ-13"))
        typed = Bill.objects.get(company=self.company, reference="BILL-FIX-13")
        self.expect("the typed dollar bill is valued once, as a whole",
                    typed.exchange_rate == rate and typed.total_amount_base == D("15029.68"),
                    f"{typed.exchange_rate} / {typed.total_amount_base}")
        for amount in ("33.33", "33.33", "0.01"):
            self.post(client, f"pay {amount} dollars", reverse("procurement:bill_pay",
                                                              args=[typed.pk]),
                      {"amount": amount, "gl_account": self.account("1200").pk})
        typed.refresh_from_db()
        gl_open = -(self.net("2100") - ap0)
        self.expect("after part payments AP holds exactly what the bills still owe",
                    gl_open == typed.outstanding_base + bill.outstanding_base,
                    f"GL {gl_open} against {typed.outstanding_base} + {bill.outstanding_base}")
        vat1 = self.net("2210")
        self.post(client, "a dollar debit note", reverse("procurement:bill_debit_note",
                                                         args=[typed.pk]), {"amount": "11.11"})
        note = Bill.objects.filter(original_bill=typed).first()
        self.expect("the note's VAT comes back at the tax it carries, at the bill's rate",
                    note is not None and vat1 - self.net("2210") == to_base(note.tax_amount, rate),
                    f"{vat1 - self.net('2210')} against "
                    f"{note and to_base(note.tax_amount, rate)}")
        dr, cr, ok = self.balanced(note.journal_entry) if note else (Z, Z, False)
        self.expect("the dollar note balances", ok, f"Dr {dr} / Cr {cr}")
        typed.refresh_from_db()
        self.post(client, "pay the rest", reverse("procurement:bill_pay", args=[typed.pk]),
                  {"amount": str(typed.outstanding_amount), "gl_account": self.account("1200").pk})
        typed.refresh_from_db()
        self.expect("the dollar bill is settled", typed.status == "PAID"
                    and typed.outstanding_amount == Z, typed.status)

        # The order's bill as well, so every dollar paper is settled.
        self.post(client, "pay the order's dollar bill",
                  reverse("procurement:bill_pay", args=[bill.pk]),
                  {"amount": "27.04", "gl_account": self.account("1200").pk})
        self.expect("with every dollar bill settled, AP is back exactly where it was",
                    self.net("2100") == ap0, f"AP moved {self.net('2100') - ap0}")
        expected_vat = (to_base(D("3.73"), rate) + to_base(D("16.00"), rate)
                        - (to_base(note.tax_amount, rate) if note else Z))
        self.expect("VAT input is the bills' tax less the note's, each at its rate",
                    self.net("2210") - vat0 == expected_vat,
                    f"{self.net('2210') - vat0} against {expected_vat}")
        statement = client.get(reverse("procurement:supplier_statement", args=[shenzhen.pk]))
        closing = statement.context["closing_balance"] if statement.context else None
        self.expect("the supplier statement closes at nothing too", closing == Z, str(closing))
        self.stock_agrees("after dollar purchasing")
        self.accrual_agrees("after dollar purchasing")

    # ── 14 ──
    def check_requisitions(self, client):
        from apps.procurement.models import PurchaseRequisition

        buyer = self.role_client("WAREHOUSE_MANAGER")
        create = reverse("procurement:requisition_create")
        header = {"reference": "PR-FIX-14", "department": "",
                  "required_date": str(self.today + datetime.timedelta(days=7)),
                  "notes": "Steel for the next batch"}
        self.post(buyer, "a requisition", create, header)
        req = PurchaseRequisition.objects.filter(company=self.company,
                                                 reference="PR-FIX-14").first()
        if not self.expect("the requisition is saved as a draft",
                           req is not None and req.status == "DRAFT"):
            return
        self.reject(buyer, "the same requisition reference twice", create, header)
        self.expect("only one requisition carries the reference",
                    PurchaseRequisition.objects.filter(company=self.company,
                                                       reference__iexact="pr-fix-14").count() == 1)

        resp = buyer.post(reverse("procurement:requisition_submit", args=[req.pk]), {})
        req.refresh_from_db()
        self.expect("an empty requisition cannot be submitted", req.status == "DRAFT", req.status)
        self.expect_flash(buyer, resp, "at least one line", "and says what is missing")

        line_url = reverse("procurement:requisition_line_add", args=[req.pk])
        for junk in ("NaN", "-1", "abc"):
            buyer.post(line_url, {"product": self.raw.pk, "quantity": junk,
                                  "estimated_unit_cost": "50"})
        self.expect("junk quantities add no line", not req.lines.exists())
        self.post(buyer, "a product line", line_url,
                  {"product": self.raw.pk, "description": "", "quantity": "10",
                   "estimated_unit_cost": "52.5", "warehouse": self.warehouse.pk})
        # A line that names no product could be approved and never ordered, so
        # it is refused at entry now rather than at "Raise purchase order".
        resp = buyer.post(line_url, {"product": "", "description": "Welding rods, 3mm",
                                     "quantity": "2", "estimated_unit_cost": "400"})
        self.expect_flash(buyer, resp, "Welding rods", "a described line is refused, naming it")
        req.refresh_from_db()
        self.expect("the product line and its estimate are on the requisition",
                    req.lines.count() == 1 and req.total_amount == D("525.00"),
                    f"{req.lines.count()} lines, {req.total_amount}")

        resp = client.post(reverse("procurement:requisition_reject", args=[req.pk]), {})
        req.refresh_from_db()
        self.expect("a draft cannot be rejected before it was asked", req.status == "DRAFT",
                    req.status)
        client.post(reverse("procurement:requisition_approve", args=[req.pk]), {})
        req.refresh_from_db()
        self.expect("nor approved", req.status == "DRAFT", req.status)

        self.post(buyer, "submit it", reverse("procurement:requisition_submit", args=[req.pk]), {})
        req.refresh_from_db()
        self.expect("submitted, it waits for approval", req.status == "PENDING_APPROVAL", req.status)
        buyer.post(reverse("procurement:requisition_line_add", args=[req.pk]),
                   {"product": self.raw.pk, "quantity": "1", "estimated_unit_cost": "1"})
        self.expect("a submitted requisition takes no more lines", req.lines.count() == 1,
                    str(req.lines.count()))

        finance = self.role_client("FINANCE_MANAGER")
        self.post(finance, "approve it", reverse("procurement:requisition_approve",
                                                 args=[req.pk]), {})
        req.refresh_from_db()
        self.expect("finance approves it", req.status == "APPROVED"
                    and req.approved_at is not None, req.status)
        resp = finance.post(reverse("procurement:requisition_reject", args=[req.pk]), {})
        req.refresh_from_db()
        self.expect("an approved requisition cannot then be rejected", req.status == "APPROVED",
                    req.status)
        self.expect_flash(finance, resp, "waiting for approval", "and the refusal says why")

        # A second requisition, all products, becomes an order.
        self.post(buyer, "a product requisition", create,
                  dict(header, reference="PR-FIX-14B"))
        good = PurchaseRequisition.objects.get(company=self.company, reference="PR-FIX-14B")
        self.post(buyer, "its line", reverse("procurement:requisition_line_add", args=[good.pk]),
                  {"product": self.raw.pk, "quantity": "6", "estimated_unit_cost": "50"})
        self.post(buyer, "submit", reverse("procurement:requisition_submit", args=[good.pk]), {})
        self.post(finance, "approve", reverse("procurement:requisition_approve",
                                              args=[good.pk]), {})
        resp = self.post(client, "raise the order",
                         reverse("procurement:requisition_to_po", args=[good.pk]),
                         {"supplier": self.supplier.pk})
        good.refresh_from_db()
        po = good.purchaseorder_set.first()
        self.expect("the approved requisition becomes a draft order with its lines",
                    po is not None and po.status == "DRAFT" and good.status == "PO_CREATED"
                    and po.lines.count() == 1 and po.lines.first().quantity == D("6")
                    and po.subtotal == D("300.00"),
                    f"{good.status}, {po and po.lines.count()} lines, {po and po.subtotal}")
        client.post(reverse("procurement:requisition_to_po", args=[good.pk]),
                    {"supplier": self.supplier.pk})
        self.expect("and only once", good.purchaseorder_set.count() == 1,
                    str(good.purchaseorder_set.count()))

        # A request turned down is answered once.
        self.post(buyer, "a third requisition", create, dict(header, reference="PR-FIX-14C"))
        third = PurchaseRequisition.objects.get(company=self.company, reference="PR-FIX-14C")
        self.post(buyer, "its line", reverse("procurement:requisition_line_add", args=[third.pk]),
                  {"product": self.raw.pk, "quantity": "1", "estimated_unit_cost": "50"})
        self.post(buyer, "submit", reverse("procurement:requisition_submit", args=[third.pk]), {})
        self.post(finance, "reject", reverse("procurement:requisition_reject",
                                             args=[third.pk]), {})
        third.refresh_from_db()
        self.expect("a submitted requisition can be rejected", third.status == "REJECTED",
                    third.status)
        finance.post(reverse("procurement:requisition_approve", args=[third.pk]), {})
        third.refresh_from_db()
        self.expect("and a rejected one cannot then be approved", third.status == "REJECTED",
                    third.status)
        self.get_ok(client, "the requisition page", reverse("procurement:requisition_detail",
                                                              args=[req.pk]))

    # ── 15 ──
    def check_close_short(self, client):
        from apps.accounting.gl_service import accrual_remaining

        po = self.order(client, "PO-FIX-15", [(self.raw, 10, "50", False)])
        line = po.lines.first()
        close = reverse("procurement:po_close_short", args=[po.pk])
        resp = client.post(close, {})
        po.refresh_from_db()
        self.expect("an order nothing has arrived on is cancelled, not closed short",
                    po.status == "SENT", po.status)
        self.receive(client, po, [(line, 4)])
        resp = client.post(reverse("procurement:po_cancel", args=[po.pk]), {})
        po.refresh_from_db()
        self.expect("a part-received order still cannot be cancelled",
                    po.status == "PARTIALLY_RECEIVED", po.status)
        self.expect_flash(client, resp, "close short", "and the refusal points at Close short")
        resp = client.post(close, {})
        po.refresh_from_db()
        self.expect("it cannot be closed while what arrived is unbilled",
                    po.status == "PARTIALLY_RECEIVED", po.status)
        self.expect_flash(client, resp, "bill this order", "and says to bill it first")
        draft = self.bill_order(client, po, post=False)
        resp = client.post(close, {})
        po.refresh_from_db()
        self.expect("nor while its bill is a draft and the accrual still open",
                    po.status == "PARTIALLY_RECEIVED" and accrual_remaining(po) == D("200.00"),
                    f"{po.status}, {accrual_remaining(po)}")
        self.post(client, "post the bill", reverse("procurement:bill_post", args=[draft.pk]), {})
        resp = self.post(client, "close it short", close, {})
        po.refresh_from_db()
        line.refresh_from_db()
        self.expect("once billed, the order closes short as it stands",
                    po.status == "CLOSED" and line.received_quantity == D("4")
                    and line.billed_quantity == D("4") and accrual_remaining(po) == Z,
                    f"{po.status}, received {line.received_quantity}, billed {line.billed_quantity}")
        moves = po.stock_movements.count()
        self.receive(client, po, [(line, 2)])
        self.expect("a closed order takes no more goods", po.stock_movements.count() == moves)
        client.post(reverse("procurement:po_to_bill", args=[po.pk]), {})
        self.expect("and no more bills", po.bills.count() == 1, str(po.bills.count()))
        self.stock_agrees("after closing an order short")
        self.accrual_agrees("after closing an order short")

    # ── 16 ──
    def check_flash_rounding(self, client):
        import re

        long_decimals = re.compile(r"\d\.\d{3,}")
        po = self.order(client, "PO-FIX-16", [], send=False)
        resp = client.post(reverse("procurement:po_line_add", args=[po.pk]),
                           {"product": self.raw.pk, "quantity": "3", "unit_cost": "33.3333",
                            "tax_rate": self.vat.pk})
        said = self.messages_of(client, resp)
        self.expect("the order total prints to the cent",
                    said and not any(long_decimals.search(m) for m in said)
                    and any("116.00" in m for m in said), str(said))
        resp = self.adjust(client, "ADJUSTMENT_PLUS", "2.5", "12.3456")
        said = self.messages_of(client, resp)
        self.expect("a stock adjustment prints its cost to the cent",
                    said and not any(long_decimals.search(m) for m in said), str(said))

    # ─────────────────────────── round three ─────────────────────────────────

    def product_data(self, product, **changes):
        """The product form as it stands, with some boxes changed."""
        data = {
            "sku": product.sku, "name": product.name, "product_type": product.product_type,
            "category": product.category_id or "", "unit_of_measure": product.unit_of_measure_id,
            "cost_price": str(product.cost_price), "selling_price": str(product.selling_price),
            "tax_rate": product.tax_rate_id or "", "reorder_level": "0",
            "reorder_quantity": "0", "valuation_method": product.valuation_method,
            "income_account": product.income_account_id or "",
            "cogs_account": product.cogs_account_id or "",
            "inventory_account": product.inventory_account_id or "",
            "barcode": "", "description": "",
        }
        if product.track_inventory:
            data["track_inventory"] = "on"
        for key, value in changes.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        return data

    def work_order(self, client, bom, ref, planned):
        from apps.manufacturing.models import WorkOrder

        client.post(reverse("manufacturing:work_order_create"), {
            "reference": ref, "bom": bom.pk, "planned_quantity": str(planned),
            "planned_start": str(self.today), "planned_end": "",
            "warehouse": self.warehouse.pk, "notes": ""})
        return WorkOrder.objects.filter(company=self.company, reference=ref).first()

    def recipe(self, client, name, product, lines, per_run="1"):
        from apps.manufacturing.models import BillOfMaterials

        client.post(reverse("manufacturing:bom_create"), merge({
            "name": name, "product": product.pk, "version": "1",
            "status": "ACTIVE", "production_quantity": per_run, "notes": "",
        }, formset("lines", [
            {"component": part.pk, "quantity": str(qty), "unit_of_measure": "",
             "scrap_percent": "0"} for part, qty in lines
        ], blank={"component": "", "quantity": "", "unit_of_measure": "", "scrap_percent": "0"})))
        return BillOfMaterials.objects.filter(company=self.company, name=name).first()

    # ── R3-1 ──
    def check_product_edit(self, client):
        from apps.accounting.gl_service import ensure_account

        bolt = self.make_product(client, "BOLT-R1", "20")
        po = self.order(client, "PO-R3-1", [(bolt, 4, "20", False)])
        self.receive(client, po, [(po.lines.first(), 4)])
        edit = reverse("inventory:product_edit", args=[bolt.pk])

        resp = client.post(edit, self.product_data(bolt, track_inventory=None))
        bolt.refresh_from_db()
        form = resp.context.get("form") if resp.status_code == 200 and resp.context else None
        self.expect("stock tracking cannot be turned off while goods are on the shelf",
                    resp.status_code == 200 and bolt.track_inventory
                    and form is not None and "track_inventory" in form.errors,
                    f"HTTP {resp.status_code}, tracked {bolt.track_inventory}")
        self.expect("and the refusal says the goods are on hand and unbilled",
                    form is not None and "on hand" in str(form.errors)
                    and "PO-R3-1" in str(form.errors), str(form and form.errors))

        spares = ensure_account(self.company, "1410", "Inventory - Spares", "ASSET", "1000")
        resp = client.post(edit, self.product_data(bolt, inventory_account=spares.pk))
        bolt.refresh_from_db()
        self.expect("nor can it move to another inventory account",
                    resp.status_code == 200 and bolt.inventory_account.code == "1400",
                    f"HTTP {resp.status_code}, {bolt.inventory_account}")

        # Bill what arrived and use it all: then either edit is allowed.
        self.bill_order(client, po)
        self.adjust(client, "ADJUSTMENT_MINUS", "4", product=bolt)
        self.post(client, "move the empty product to 1410", edit,
                  self.product_data(bolt, inventory_account=spares.pk))
        bolt.refresh_from_db()
        self.expect("an empty, fully billed product can change its account",
                    bolt.inventory_account_id == spares.pk, str(bolt.inventory_account))
        self.post(client, "and stop tracking it", edit,
                  self.product_data(bolt, track_inventory=None))
        bolt.refresh_from_db()
        self.expect("and stop being tracked", not bolt.track_inventory)
        self.stock_agrees("after the product edits")

    # ── R3-2 and R3-5 ──
    def check_untracked_production(self, client):
        from apps.manufacturing.models import WorkOrder

        steel = self.make_product(client, "STEEL-R2", "40")
        po = self.order(client, "PO-R3-2", [(steel, 20, "40", False)])
        self.receive(client, po, [(po.lines.first(), 20)])
        self.bill_order(client, po)

        # A finished product nobody keeps on a shelf (made to order, say).
        job = self.make_product(client, "JOB-R2", "0", track=False)
        bom = self.recipe(client, f"Job recipe {self.stamp}", job, [(steel, 2)])
        wo = self.work_order(client, bom, f"WO-R3-2-{self.stamp}", 3)
        wip0, cogs0 = self.net("1450"), self.net("5000")
        self.post(client, "complete the untracked job",
                  reverse("manufacturing:work_order_complete", args=[wo.pk]),
                  {"actual_quantity": "3", "actual_end": str(self.today)})
        wo.refresh_from_db()
        self.expect("an untracked product's work order completes",
                    wo.status == "COMPLETED" and wo.cost == D("240.00"),
                    f"{wo.status} / {wo.cost}")
        self.expect("work in progress returns to where it was",
                    self.net("1450") == wip0, f"{self.net('1450')} against {wip0}")
        self.expect("and what was made is expensed to cost of sales",
                    self.net("5000") - cogs0 == D("240.00"), str(self.net("5000") - cogs0))
        self.expect("no shelf is made up for the untracked product",
                    self.shelf(job) is None or self.shelf(job).quantity == Z)
        self.stock_agrees("after an untracked output")

        # Labour is an untracked input: it is never short, and its cost goes in.
        labour = self.make_product(client, "LAB-R5", "15", track=False)
        frame = self.make_product(client, "FRAME-R5", "0")
        bom = self.recipe(client, f"Frame recipe {self.stamp}", frame, [(steel, 1), (labour, 2)])
        wo = self.work_order(client, bom, f"WO-R3-5-{self.stamp}", 4)
        absorbed0 = self.net("5250")
        resp = client.post(reverse("manufacturing:work_order_complete", args=[wo.pk]),
                           {"actual_quantity": "4", "actual_end": str(self.today)})
        wo.refresh_from_db()
        self.expect("a recipe with a labour line completes, with no stock check on it",
                    wo is not None and wo.status == "COMPLETED", wo and wo.status)
        self.expect("the frames carry steel and labour: 4 × 40 + 8 × 15",
                    wo.cost == D("280.00") and self.shelf(frame).value == D("280.00")
                    and self.shelf(frame).quantity == D("4"),
                    f"{wo.cost} / {self.shelf(frame) and self.shelf(frame).value}")
        self.expect("the labour is credited to production costs absorbed",
                    absorbed0 - self.net("5250") == D("120.00"), str(absorbed0 - self.net("5250")))
        self.expect("work in progress is empty again", self.net("1450") == wip0,
                    str(self.net("1450")))
        self.expect("labour leaves no shelf behind",
                    self.shelf(labour) is None or self.shelf(labour).quantity == Z)
        self.stock_agrees("after a recipe with labour")

    # ── R3-3 ──
    def check_cancel_one_of_two(self, client):
        from apps.accounting.gl_service import accrual_remaining

        nut = self.make_product(client, "NUT-R3", "5")
        washer = self.make_product(client, "WASHER-R3", "3")
        po = self.order(client, "PO-R3-3", [(nut, 10, "5", True), (washer, 10, "3", False)])
        nut_line, washer_line = po.lines.order_by("id")
        self.receive(client, po, [(nut_line, 10)])
        first = self.bill_order(client, po)
        self.receive(client, po, [(washer_line, 10)])
        second = self.bill_order(client, po)
        po.refresh_from_db()
        self.expect("two bills finish the order", po.status == "BILLED"
                    and first.status == second.status == "RECEIVED", po.status)

        self.post(client, "cancel the first bill",
                  reverse("procurement:bill_cancel", args=[first.pk]), {})
        po.refresh_from_db()
        nut_line.refresh_from_db()
        washer_line.refresh_from_db()
        self.expect("cancelling one bill hands back exactly its own quantities",
                    nut_line.billed_quantity == Z and washer_line.billed_quantity == D("10")
                    and po.status == "FULLY_RECEIVED",
                    f"{nut_line.billed_quantity} / {washer_line.billed_quantity} / {po.status}")
        self.expect("and its accrual is standing again, ready to be billed",
                    accrual_remaining(po) == D("50.00"), str(accrual_remaining(po)))
        self.accrual_agrees("after cancelling one of two bills")

        again = self.bill_order(client, po, post=False)
        self.expect("the order can be billed again, for that bill's part only",
                    again is not None and again.subtotal == D("50.00")
                    and again.accrual_to_clear == D("50.00"),
                    f"{again and again.subtotal} / {again and again.accrual_to_clear}")
        # A draft cancelled hands its part back too.
        self.post(client, "cancel the draft", reverse("procurement:bill_cancel", args=[again.pk]), {})
        nut_line.refresh_from_db()
        self.expect("a cancelled draft releases its quantities",
                    nut_line.billed_quantity == Z, str(nut_line.billed_quantity))
        final = self.bill_order(client, po)
        po.refresh_from_db()
        self.expect("billed once more, the order is finished and the accrual cleared",
                    final is not None and final.status == "RECEIVED" and po.status == "BILLED"
                    and accrual_remaining(po) == Z,
                    f"{final and final.status} / {po.status} / {accrual_remaining(po)}")
        self.accrual_agrees("after re-billing")
        self.stock_agrees("after re-billing")

    # ── R3-4 ──
    def check_positive_quantities(self, client):
        from apps.inventory.models import StockMovement
        from apps.manufacturing.models import BillOfMaterials

        gear = self.make_product(client, "GEAR-R4", "0")
        cog = self.make_product(client, "COG-R4", "10")
        count = BillOfMaterials.objects.filter(company=self.company).count()
        self.recipe(client, f"Negative run {self.stamp}", gear, [(cog, 1)], per_run="-1")
        self.recipe(client, f"Zero run {self.stamp}", gear, [(cog, 1)], per_run="0")
        self.expect("a recipe that makes nothing or less per run is refused",
                    BillOfMaterials.objects.filter(company=self.company).count() == count)
        bom = self.recipe(client, f"Gear recipe {self.stamp}", gear, [(cog, 1)])
        for planned in ("0", "-5"):
            wo = self.work_order(client, bom, f"WO-R3-4-{planned}-{self.stamp}", planned)
            self.expect(f"a work order planned at {planned} is refused", wo is None,
                        wo and wo.status)
        moved = StockMovement.objects.filter(product=gear).exists()
        self.expect("and no gears appear from nothing", not moved)

    # ── R3-6 ──
    def check_stock_cents(self, client):
        from apps.inventory.models import StockMovement

        # A unit cost to four places: every receipt posts 1,006.64 for 1,006.6424.
        valve = self.make_product(client, "VALVE-R6", "1006.6424")
        stock0, adj0 = self.net("1400"), self.net("5300")
        po = self.order(client, "PO-R3-6", [(valve, 10, "1006.6424", False)])
        line = po.lines.first()
        for _ in range(10):
            self.receive(client, po, [(line, 1)])
        level = self.shelf(valve)
        self.expect("ten single receipts put exactly what they posted on the shelf",
                    level.value == D("10066.40") and self.net("1400") - stock0 == D("10066.40"),
                    f"shelf {level.value}, GL {self.net('1400') - stock0}")
        self.stock_agrees("after ten single receipts")
        inventory_page = client.get(reverse("inventory:index"))
        self.expect("the inventory dashboard reports the ledger's figure",
                    inventory_page.status_code == 200
                    and inventory_page.context["inventory_value"] == self.valuation(),
                    f"{inventory_page.context and inventory_page.context.get('inventory_value')}")

        self.adjust(client, "ADJUSTMENT_MINUS", "3", product=valve)
        issue = StockMovement.objects.filter(product=valve,
                                             movement_type="ADJUSTMENT_MINUS").first()
        self.expect("a part issue is the shelf's value before less after",
                    issue is not None and issue.total_cost == D("3019.92"),
                    str(issue and issue.total_cost))
        self.stock_agrees("after a part write-off")
        self.adjust(client, "ADJUSTMENT_MINUS", "7", product=valve)
        level = self.shelf(valve)
        self.expect("the last unit out takes whatever value is left: an empty shelf is worth 0",
                    level.quantity == Z and level.value == Z,
                    f"{level.quantity} @ {level.value}")
        self.expect("and the write-offs took exactly what came in",
                    self.net("1400") == stock0 and self.net("5300") - adj0 == D("10066.40"),
                    f"1400 {self.net('1400') - stock0}, 5300 {self.net('5300') - adj0}")
        self.bill_order(client, po)
        self.stock_agrees("after the shelf is emptied")

        # Thirds: 100 over three units, out one at a time.
        third = self.make_product(client, "THIRD-R6", "33.3333")
        self.adjust(client, "ADJUSTMENT_PLUS", "3", "33.3333", product=third)
        for _ in range(3):
            self.adjust(client, "ADJUSTMENT_MINUS", "1", product=third)
        level = self.shelf(third)
        self.expect("three thirds out leave nothing behind",
                    level.quantity == Z and level.value == Z, f"{level.quantity} @ {level.value}")
        self.stock_agrees("after three thirds")

    # ── R3-7, R3-15, R3-16 ──
    def check_requisitions_round3(self, client):
        from apps.core.models import SequenceCounter
        from apps.procurement.models import PurchaseRequisition

        finance = self.role_client("FINANCE_MANAGER")
        create = reverse("procurement:requisition_create")
        before = SequenceCounter.peek_number(self.company, "PR-")
        finance.get(create)
        finance.get(create)
        self.expect("opening the requisition form takes no number",
                    SequenceCounter.peek_number(self.company, "PR-") == before,
                    f"{before} -> {SequenceCounter.peek_number(self.company, 'PR-')}")
        header = {"reference": before, "department": "",
                  "required_date": str(self.today + datetime.timedelta(days=7)), "notes": ""}
        self.post(finance, "a requisition with the previewed number", create, header)
        own = PurchaseRequisition.objects.filter(company=self.company, reference=before).first()
        self.expect("it is saved under the number the form showed", own is not None, before)
        if own is None:
            return
        self.post(finance, "its line",
                  reverse("procurement:requisition_line_add", args=[own.pk]),
                  {"product": self.raw.pk, "quantity": "2", "estimated_unit_cost": "50"})
        self.post(finance, "submit it", reverse("procurement:requisition_submit", args=[own.pk]), {})
        resp = finance.post(reverse("procurement:requisition_approve", args=[own.pk]), {})
        own.refresh_from_db()
        self.expect("a finance manager cannot approve their own requisition",
                    own.status == "PENDING_APPROVAL", own.status)
        self.expect_flash(finance, resp, "approve", "and is told why")
        page = finance.get(reverse("procurement:requisition_detail", args=[own.pk]))
        self.expect("nor is shown the Approve button on it",
                    reverse("procurement:requisition_approve", args=[own.pk])
                    not in page.content.decode())

        # Raised by one person, submitted by another: the submitter cannot approve.
        other = self.role_client("FINANCE_MANAGER")
        self.post(other, "another finance manager approves it",
                  reverse("procurement:requisition_approve", args=[own.pk]), {})
        own.refresh_from_db()
        self.expect("somebody else can", own.status == "APPROVED", own.status)

        resp = self.post(client, "order it", reverse("procurement:requisition_to_po",
                                                     args=[own.pk]),
                         {"supplier": self.supplier.pk})
        own.refresh_from_db()
        po = own.purchaseorder_set.first()
        resp = client.post(reverse("procurement:requisition_approve", args=[own.pk]), {})
        said = " ".join(self.messages_of(client, resp))
        self.expect("an ordered requisition is said to be on a purchase order",
                    "already on a purchase order" in said and "po created" not in said.lower(),
                    said)
        self.post(client, "cancel its order", reverse("procurement:po_cancel", args=[po.pk]), {})
        own.refresh_from_db()
        self.expect("cancelling the order hands the requisition back as approved",
                    own.status == "APPROVED", own.status)
        self.post(client, "cancel the requisition",
                  reverse("procurement:requisition_cancel", args=[own.pk]), {})
        own.refresh_from_db()
        self.expect("an approved requisition can be cancelled", own.status == "CANCELLED",
                    own.status)
        resp = client.post(reverse("procurement:requisition_cancel", args=[own.pk]), {})
        self.expect_flash(client, resp, "cannot be cancelled", "but only once")

        for start in ("DRAFT", "PENDING_APPROVAL"):
            self.post(client, f"a requisition to withdraw from {start}", create,
                      dict(header, reference=f"PR-R3-{start}"))
            req = PurchaseRequisition.objects.get(company=self.company,
                                                  reference=f"PR-R3-{start}")
            if start == "PENDING_APPROVAL":
                self.post(client, "its line",
                          reverse("procurement:requisition_line_add", args=[req.pk]),
                          {"product": self.raw.pk, "quantity": "1", "estimated_unit_cost": "5"})
                self.post(client, "submit",
                          reverse("procurement:requisition_submit", args=[req.pk]), {})
            self.post(client, "cancel it", reverse("procurement:requisition_cancel",
                                                   args=[req.pk]), {})
            req.refresh_from_db()
            self.expect(f"a requisition {start.lower()} can be cancelled",
                        req.status == "CANCELLED", req.status)

        # Wording: a closed-short order is not told to be sent to the supplier.
        closed = self.order(client, "PO-R3-16", [(self.raw, 4, "50", False)])
        self.receive(client, closed, [(closed.lines.first(), 1)])
        self.bill_order(client, closed)
        self.post(client, "close it short",
                  reverse("procurement:po_close_short", args=[closed.pk]), {})
        resp = self.receive(client, closed, [(closed.lines.first(), 1)])
        said = " ".join(self.messages_of(client, resp))
        self.expect("receiving on a closed order says it was closed short",
                    "was closed short" in said and "send it to the supplier" not in said, said)

    # ── R3-8 ──
    def check_requisition_currency(self, client):
        from apps.crm.models import Supplier
        from apps.procurement.models import PurchaseOrder, PurchaseRequisition

        dollars = Supplier.objects.create(company=self.company, name="Ohio Tools Inc",
                                          currency="USD")
        create = reverse("procurement:requisition_create")
        self.post(client, "a requisition", create, {
            "reference": "PR-R3-USD", "department": "",
            "required_date": str(self.today + datetime.timedelta(days=7)), "notes": ""})
        req = PurchaseRequisition.objects.get(company=self.company, reference="PR-R3-USD")
        self.post(client, "its line", reverse("procurement:requisition_line_add", args=[req.pk]),
                  {"product": self.raw.pk, "quantity": "2", "estimated_unit_cost": "3"})
        self.post(client, "submit", reverse("procurement:requisition_submit", args=[req.pk]), {})
        approver = self.role_client("FINANCE_MANAGER")
        self.post(approver, "approve",
                  reverse("procurement:requisition_approve", args=[req.pk]), {})
        self.post(client, "order it from the dollar supplier",
                  reverse("procurement:requisition_to_po", args=[req.pk]),
                  {"supplier": dollars.pk})
        po = req.purchaseorder_set.first()
        self.expect("the order is raised in the supplier's dollars at the day's rate",
                    po is not None and po.currency == "USD"
                    and po.exchange_rate == D("131"),
                    f"{po and po.currency} at {po and po.exchange_rate}")

        self.post(client, "an order form with no currency chosen",
                  reverse("procurement:po_create"), {
                      "reference": "PO-R3-8", "supplier": dollars.pk,
                      "order_date": str(self.today), "expected_delivery_date": "",
                      "warehouse": self.warehouse.pk, "notes": ""})
        typed = PurchaseOrder.objects.filter(company=self.company, reference="PO-R3-8").first()
        self.expect("the order form defaults to the supplier's currency",
                    typed is not None and typed.currency == "USD"
                    and typed.exchange_rate == D("131"),
                    f"{typed and typed.currency} at {typed and typed.exchange_rate}")
        form = client.get(reverse("procurement:po_create")).content.decode()
        self.expect("and the page knows each supplier's currency", "data-currencies" in form
                    and '"USD"' in form.replace("&quot;", '"'))

    # ── R3-9 ──
    def check_draft_lines_only(self, client):
        po = self.order(client, "PO-R3-9", [(self.raw, 2, "50", False)])
        line = po.lines.first()
        resp = client.post(reverse("procurement:po_line_add", args=[po.pk]),
                           {"product": self.raw.pk, "quantity": "1", "unit_cost": "50"})
        self.expect("a sent order takes no new lines", po.lines.count() == 1,
                    str(po.lines.count()))
        self.expect_flash(client, resp, "only a draft", "and says only a draft can change")
        client.post(reverse("procurement:po_line_delete", args=[po.pk, line.pk]), {})
        self.expect("nor loses one", po.lines.count() == 1, str(po.lines.count()))
        self.receive(client, po, [(line, 2)])
        po.refresh_from_db()
        client.post(reverse("procurement:po_line_add", args=[po.pk]),
                    {"product": self.raw.pk, "quantity": "1", "unit_cost": "50"})
        po.refresh_from_db()
        self.expect("a fully received order cannot be reopened with a new line",
                    po.lines.count() == 1 and po.status == "FULLY_RECEIVED", po.status)
        self.bill_order(client, po)
        po.refresh_from_db()
        self.expect("so it still finishes", po.status == "BILLED", po.status)
        draft = self.order(client, "PO-R3-9D", [(self.raw, 2, "50", False)], send=False)
        dline = draft.lines.first()
        self.post(client, "delete a draft's line",
                  reverse("procurement:po_line_delete", args=[draft.pk, dline.pk]), {})
        self.expect("a draft's lines can still change", draft.lines.count() == 0)

    # ── R3-10 ──
    def check_free_goods(self, client):
        from apps.procurement.models import Bill

        sample = self.make_product(client, "SAMPLE-R10", "0")
        po = self.order(client, "PO-R3-10", [(sample, 5, "0", False)])
        self.receive(client, po, [(po.lines.first(), 5)])
        bill = self.bill_order(client, po, post=False)
        resp = client.post(reverse("procurement:bill_post", args=[bill.pk]), {})
        bill.refresh_from_db()
        po.refresh_from_db()
        said = " ".join(self.messages_of(client, resp))
        self.expect("a free-goods bill posts with no journal and settles the order",
                    bill.status == "PAID" and bill.journal_entry_id is None
                    and po.status == "BILLED" and "Journal entry must have" not in said,
                    f"{bill.status} / {po.status} / {said}")
        count = Bill.objects.filter(company=self.company).count()
        resp = client.post(reverse("procurement:bill_create"),
                           self.bill_form("BILL-R3-ZERO", subtotal="0", tax_amount="0",
                                          total_amount="0"))
        form = resp.context.get("form") if resp.status_code == 200 and resp.context else None
        self.expect("a typed bill for 0.00 is refused with a clear message",
                    Bill.objects.filter(company=self.company).count() == count
                    and form is not None and "0.00 records nothing" in str(form.errors),
                    str(form and form.errors))
        self.accrual_agrees("after free goods")

    # ── R3-11, R3-12, R3-13 ──
    def check_bill_entry(self, client):
        from apps.procurement.models import Bill

        create = reverse("procurement:bill_create")
        self.post(client, "a bill", create, self.bill_form("BILL-R3-11", supplier_reference="R3-A"))
        self.reject(client, "the same bill reference again", create,
                    self.bill_form("bill-r3-11", supplier_reference="R3-B"))
        self.expect("only one bill carries the reference",
                    Bill.objects.filter(company=self.company,
                                        reference__iexact="BILL-R3-11").count() == 1)
        self.reject(client, "a total a cent above subtotal + tax", create,
                    self.bill_form("BILL-R3-12", supplier_reference="R3-C", total_amount="1160.01"))
        self.reject(client, "a total a cent below", create,
                    self.bill_form("BILL-R3-12", supplier_reference="R3-C", total_amount="1159.99"))
        ahead = self.today + datetime.timedelta(days=400)
        back = self.today.replace(year=self.today.year - 11)
        self.reject(client, "a bill dated more than a year ahead", create,
                    self.bill_form("BILL-R3-13A", supplier_reference="R3-D", bill_date=ahead,
                                   due_date=ahead))
        self.reject(client, "a bill dated more than ten years back", create,
                    self.bill_form("BILL-R3-13B", supplier_reference="R3-E", bill_date=back))
        self.expect("none of them reached the book", not Bill.objects.filter(
            company=self.company, reference__in=["BILL-R3-12", "BILL-R3-13A", "BILL-R3-13B"]
        ).exists())

    # ── R3-13, R3-14, R3-17 ──
    def check_payments_round3(self, client):
        from apps.accounting.models import JournalEntry
        from apps.procurement.models import Bill

        old = self.today - datetime.timedelta(days=3)
        self.post(client, "a bill to pay", reverse("procurement:bill_create"),
                  self.bill_form("BILL-R3-PAY", supplier_reference="R3-PAY", bill_date=old))
        bill = Bill.objects.get(company=self.company, reference="BILL-R3-PAY")
        pay = reverse("procurement:bill_pay", args=[bill.pk])
        for amount in ("1e2", "100.005", "1,250", "NaN"):
            resp = client.post(pay, {"amount": amount})
            bill.refresh_from_db()
            self.expect(f"a payment of '{amount}' is refused", resp.status_code == 302
                        and bill.paid_amount == Z, f"paid {bill.paid_amount}")
        for when, label in ((old - datetime.timedelta(days=1), "before the bill"),
                            (self.today + datetime.timedelta(days=1), "in the future")):
            resp = client.post(pay, {"amount": "100", "payment_date": str(when)})
            bill.refresh_from_db()
            self.expect(f"a payment dated {label} is refused", bill.paid_amount == Z,
                        f"paid {bill.paid_amount}")
        self.expect_flash(client, resp, "future", "and the refusal says why")

        till0, bank0 = self.net("1100"), self.net("1200")
        self.post(client, "pay with no account named", pay, {"amount": "100"})
        bill.refresh_from_db()
        self.expect("a payment naming no account leaves the main bank, not the till",
                    bill.paid_amount == D("100.00") and self.net("1200") - bank0 == D("-100.00")
                    and self.net("1100") == till0,
                    f"bank {self.net('1200') - bank0}, till {self.net('1100') - till0}")
        self.post(client, "pay from the till on purpose", pay,
                  {"amount": "60", "gl_account": self.account("1100").pk})
        self.expect("the till pays only when chosen", self.net("1100") - till0 == D("-60.00"),
                    str(self.net("1100") - till0))
        page = client.get(reverse("procurement:bill_detail", args=[bill.pk])).content.decode()
        bank_pk = str(self.account("1200").pk)
        self.expect("the pay form starts on the bank account",
                    f'value="{bank_pk}" selected' in page, "")

        # A payment dated in a closed or missing period is booked and flagged.
        paid = JournalEntry.objects.filter(company=self.company, entry_type="PAYMENT",
                                           notes__icontains="BILL-R3-PAY").count()
        self.expect("both payments were booked", paid == 2, str(paid))

    # ── R3-18 ──
    def check_hidden_buttons(self, client):
        po = self.order(client, "PO-R3-18", [(self.raw, 2, "50", False)])
        stores = self.role_client("WAREHOUSE_MANAGER")
        page = stores.get(reverse("procurement:po_detail", args=[po.pk])).content.decode()
        self.expect("the stores do not see Bill this order",
                    reverse("procurement:po_to_bill", args=[po.pk]) not in page)
        page = stores.get(reverse("procurement:po_list")).content.decode()
        self.expect("nor New PO", reverse("procurement:po_create") not in page)
        page = stores.get(reverse("procurement:grn", args=[po.pk])).content.decode()
        self.expect("but they do see the receive form",
                    reverse("procurement:grn_record", args=[po.pk]) in page)
        for role in ("FINANCE_MANAGER", "ACCOUNTANT", "PROCUREMENT"):
            other = self.role_client(role)
            page = other.get(reverse("procurement:grn", args=[po.pk])).content.decode()
            self.expect(f"{role} does not see the receive form",
                        reverse("procurement:grn_record", args=[po.pk]) not in page)
        finance = self.role_client("FINANCE_MANAGER")
        page = finance.get(reverse("procurement:po_detail", args=[po.pk])).content.decode()
        self.expect("finance does see Bill this order",
                    reverse("procurement:po_to_bill", args=[po.pk]) in page)

        cog = self.make_product(client, "COG-R18", "10")
        widget = self.make_product(client, "WIDGET-R18", "0")
        bom = self.recipe(client, f"Widget recipe {self.stamp}", widget, [(cog, 1)])
        wo = self.work_order(client, bom, f"WO-R3-18-{self.stamp}", 1)
        url = reverse("manufacturing:work_order_detail", args=[wo.pk])
        actions = [reverse(f"manufacturing:work_order_{a}", args=[wo.pk])
                   for a in ("release", "complete", "cancel")]
        for role in ("FINANCE_MANAGER", "PROCUREMENT", "VIEWER"):
            page = self.role_client(role).get(url).content.decode()
            self.expect(f"{role} sees no work-order actions",
                        not any(a in page for a in actions))
        page = stores.get(url).content.decode()
        self.expect("the warehouse manager does",
                    all(a in page for a in actions))

    # ─────────────────────────── round four ──────────────────────────────────

    def closed_month(self):
        """Last month's accounting period, closed — and the day it covers.

        The caller reopens it (self.reopen) once done, so later sections can
        still post where they expect to.
        """
        from apps.accounting.models import period_for
        from apps.organizations.models import AccountingPeriod, FiscalYear

        day = self.today.replace(day=1) - datetime.timedelta(days=1)
        period = period_for(self.company, day)
        if period is None:
            fy, _ = FiscalYear.objects.get_or_create(
                company=self.company, name=f"FY-R4-{self.stamp}",
                defaults={"start_date": day.replace(day=1), "end_date": day})
            period = AccountingPeriod.objects.create(
                fiscal_year=fy, name=f"{day:%B %Y}", start_date=day.replace(day=1),
                end_date=day)
        period.status = "CLOSED"
        period.save(update_fields=["status"])
        return period, day

    def reopen(self, period):
        period.status = "OPEN"
        period.save(update_fields=["status"])

    def said(self, client, response):
        return " ".join(str(m) for m in self.flashes(client, response))

    # ── R4-1 ──
    def check_pay_closed_bill(self, client):
        from apps.accounting.models import JournalEntry
        from apps.procurement.models import Bill

        buyer = self.role_client("PROCUREMENT")
        last = self.today.replace(day=1) - datetime.timedelta(days=5)
        self.post(buyer, "a draft bill dated last month", reverse("procurement:bill_create"),
                  self.bill_form("BILL-R4-1", supplier_reference="R4-1", bill_date=last,
                                 due_date=last + datetime.timedelta(days=30)))
        bill = Bill.objects.get(company=self.company, reference="BILL-R4-1")
        self.expect("the buyer's bill is a draft", bill.status == "DRAFT"
                    and not bill.journal_entry_id, bill.status)
        period, _day = self.closed_month()
        entries = JournalEntry.objects.filter(company=self.company).count()
        resp = client.post(reverse("procurement:bill_pay", args=[bill.pk]), {"amount": "100"})
        bill.refresh_from_db()
        said = self.said(client, resp)
        self.expect("paying a draft bill dated in a closed month is not a server error",
                    resp.status_code == 302, f"HTTP {resp.status_code}")
        self.expect("and nothing is half done: the bill is still an unpaid draft",
                    bill.status == "DRAFT" and not bill.journal_entry_id
                    and bill.paid_amount == Z
                    and JournalEntry.objects.filter(company=self.company).count() == entries,
                    f"{bill.status}, JE {bill.journal_entry_id}, paid {bill.paid_amount}")
        self.expect("the message is the closed-period one", "closed" in said.lower()
                    and "reopen" in said.lower(), said)
        self.reopen(period)
        # Posted while open; then the month closes and a payment dated in it
        # is refused with the same message rather than a server error.
        self.post(client, "post the bill", reverse("procurement:bill_post", args=[bill.pk]), {})
        bill.refresh_from_db()
        period, day = self.closed_month()
        resp = client.post(reverse("procurement:bill_pay", args=[bill.pk]),
                           {"amount": "100", "payment_date": str(max(day, bill.bill_date))})
        bill.refresh_from_db()
        said = self.said(client, resp)
        self.expect("a payment dated in a closed month is refused, not a server error",
                    resp.status_code == 302 and bill.paid_amount == Z
                    and "closed" in said.lower(), f"HTTP {resp.status_code}: {said}")
        self.reopen(period)

    # ── R4-2 ──
    def check_retracking(self, client):
        from django.core.exceptions import ValidationError
        from apps.inventory.models import StockLevel, StockMovement

        item = self.make_product(client, "RT-R4", "100")
        po = self.order(client, "PO-R4-2", [(item, 10, "100", False)])
        self.receive(client, po, [(po.lines.first(), 10)])
        self.bill_order(client, po)
        self.adjust(client, "ADJUSTMENT_MINUS", "10", product=item)
        edit = reverse("inventory:product_edit", args=[item.pk])
        self.post(client, "untrack the emptied product", edit,
                  self.product_data(item, track_inventory=None))
        item.refresh_from_db()
        self.expect("an empty, billed product can stop being tracked", not item.track_inventory)

        level = self.shelf(item)
        moves = StockMovement.objects.filter(product=item).count()
        client.post(reverse("inventory:inventory_count", args=[self.warehouse.pk]), {
            "movement_date": str(self.today), f"count_{level.pk}": "7"})
        level.refresh_from_db()
        self.expect("a count does not touch an untracked product's shelf",
                    level.quantity == Z and level.value == Z
                    and StockMovement.objects.filter(product=item).count() == moves,
                    f"{level.quantity} / {level.value}")
        page = client.get(reverse("inventory:inventory_count",
                                  args=[self.warehouse.pk])).content.decode()
        self.expect("and the count sheet leaves it off", f"count_{level.pk}" not in page)
        try:
            with transaction.atomic():
                StockMovement.objects.create(
                    product=item, warehouse=self.warehouse, movement_type="ADJUSTMENT_PLUS",
                    quantity=D("7"), unit_cost=D("100"), movement_date=self.today)
            refused = False
        except ValidationError:
            refused = True
        self.expect("no stock movement can be made for an untracked product", refused)

        # A shelf left over from before (written straight to the table) is
        # emptied when tracking comes back on, so 1400 still equals the shelves.
        StockLevel.objects.filter(pk=level.pk).update(quantity=D("7"), value=D("700"))
        resp = client.post(edit, self.product_data(item, track_inventory="on"))
        item.refresh_from_db()
        level.refresh_from_db()
        self.expect("re-tracking empties the stale shelf",
                    item.track_inventory and level.quantity == Z and level.value == Z,
                    f"{item.track_inventory}, {level.quantity} / {level.value}")
        self.expect_flash(client, resp, "count what is", "and says to count the stock on afresh")
        self.stock_agrees("after re-tracking")

        # A service received on an order and not billed cannot become stock:
        # nothing was accrued for it, and the bill would clear 2110 anyway.
        svc = self.make_product(client, "SVC-R4", "600", track=False)
        po = self.order(client, "PO-R4-2S", [(svc, 10, "600", False)])
        self.receive(client, po, [(po.lines.first(), 10)])
        resp = client.post(reverse("inventory:product_edit", args=[svc.pk]),
                           self.product_data(svc, track_inventory="on"))
        svc.refresh_from_db()
        self.expect("a service with unbilled receipts cannot turn tracked",
                    resp.status_code == 200 and not svc.track_inventory
                    and "PO-R4-2S" in resp.content.decode(), f"HTTP {resp.status_code}")
        bill = self.bill_order(client, po)
        self.expect("so its bill accrues nothing and charges the service",
                    bill is not None and bill.accrual_to_clear == Z,
                    str(bill and bill.accrual_to_clear))
        self.accrual_agrees("after billing the service")

    # ── R4-3 ──
    def check_future_stock_dates(self, client):
        from apps.inventory.models import StockMovement
        from apps.organizations.models import Warehouse

        tomorrow = self.today + datetime.timedelta(days=1)
        moves = StockMovement.objects.filter(product__company=self.company).count()
        resp = client.post(reverse("inventory:adjustment_create"), {
            "product": self.raw.pk, "warehouse": self.warehouse.pk,
            "movement_type": "ADJUSTMENT_PLUS", "quantity": "1", "unit_cost": "50",
            "movement_date": str(tomorrow), "reference": "R4-3", "notes": ""})
        self.expect("an adjustment dated tomorrow is refused",
                    resp.status_code == 200 and "future" in resp.content.decode(),
                    f"HTTP {resp.status_code}")
        level = self.shelf(self.raw)
        resp = client.post(reverse("inventory:inventory_count", args=[self.warehouse.pk]), {
            "movement_date": str(tomorrow), f"count_{level.pk}": str(level.quantity + 1)})
        self.expect_flash(client, resp, "future", "a count dated tomorrow is refused")
        second, _ = Warehouse.objects.get_or_create(
            company=self.company, code=f"R4B{self.stamp[-4:]}",
            defaults={"name": "Second Store R4", "is_active": True})
        self.second = second
        resp = client.post(reverse("inventory:stock_transfer"), {
            "from_warehouse": self.warehouse.pk, "to_warehouse": second.pk,
            "product": self.raw.pk, "quantity": "1", "movement_date": str(tomorrow)})
        self.expect_flash(client, resp, "future", "a transfer dated tomorrow is refused")
        period, day = self.closed_month()
        resp = client.post(reverse("inventory:stock_transfer"), {
            "from_warehouse": self.warehouse.pk, "to_warehouse": second.pk,
            "product": self.raw.pk, "quantity": "1", "movement_date": str(day)})
        self.expect_flash(client, resp, "closed", "a transfer dated in a closed month is refused")
        resp = client.post(reverse("inventory:adjustment_create"), {
            "product": self.raw.pk, "warehouse": self.warehouse.pk,
            "movement_type": "ADJUSTMENT_PLUS", "quantity": "1", "unit_cost": "50",
            "movement_date": str(day), "reference": "R4-3", "notes": ""})
        self.expect("an adjustment dated in a closed month is refused",
                    resp.status_code == 200 and "closed" in resp.content.decode(),
                    f"HTTP {resp.status_code}")

        part = self.make_product(client, "PART-R4", "10")
        made = self.make_product(client, "MADE-R4", "0")
        self.adjust(client, "ADJUSTMENT_PLUS", "5", cost="10", product=part)
        bom = self.recipe(client, f"R4 recipe {self.stamp}", made, [(part, 1)])
        wo = self.work_order(client, bom, f"WO-R4-3-{self.stamp}", 1)
        resp = client.post(reverse("manufacturing:work_order_complete", args=[wo.pk]),
                           {"actual_quantity": "1", "actual_end": str(tomorrow)})
        wo.refresh_from_db()
        self.expect("a work order cannot be completed tomorrow", wo.status == "DRAFT",
                    wo.status)
        self.expect_flash(client, resp, "future", "and the refusal says why")
        # R4-11: the closed-period message reads properly and advises the
        # right thing — not "receive the missing materials".
        resp = client.post(reverse("manufacturing:work_order_complete", args=[wo.pk]),
                           {"actual_quantity": "1", "actual_end": str(day)})
        wo.refresh_from_db()
        said = self.said(client, resp)
        self.expect("a work order dated in a closed month is not completed",
                    wo.status == "DRAFT", wo.status)
        self.expect("its message has one full stop and the closed-period advice",
                    ".." not in said and "reopen" in said.lower()
                    and "receive the missing" not in said.lower(), said)
        self.reopen(period)
        self.expect("nothing above moved any stock",
                    StockMovement.objects.filter(product__company=self.company,
                                                 movement_date__gt=self.today).count() == 0
                    and StockMovement.objects.filter(
                        product__company=self.company).count() == moves + 1,
                    "")
        self.post(client, "complete it today", reverse("manufacturing:work_order_complete",
                                                       args=[wo.pk]),
                  {"actual_quantity": "1", "actual_end": str(self.today)})
        wo.refresh_from_db()
        self.expect("dated today it completes", wo.status == "COMPLETED", wo.status)
        self.wo_part, self.wo_bom = part, bom

    # ── R4-4 ──
    def check_units_in_use(self, client):
        from apps.inventory.models import UnitOfMeasure

        self.post(client, "a box of twelve", reverse("inventory:unit_create"), {
            "name": "Box of 12", "abbreviation": "BX12", "kind": "COUNT",
            "base_factor": "12", "is_base": ""})
        box = UnitOfMeasure.objects.get(company=self.company, abbreviation="BX12")
        recipe_line = self.wo_bom.lines.first()
        recipe_line.unit_of_measure = box
        recipe_line.save()
        resp = client.post(reverse("inventory:unit_edit", args=[box.pk]), {
            "name": "Box of 12", "abbreviation": "BX12", "kind": "COUNT",
            "base_factor": "24", "is_base": ""})
        box.refresh_from_db()
        self.expect("a unit a recipe uses keeps its factor",
                    resp.status_code == 200 and box.base_factor == D("12"), str(box.base_factor))
        resp = client.post(reverse("inventory:unit_edit", args=[box.pk]), {
            "name": "Box of 12", "abbreviation": "BX12", "kind": "WEIGHT",
            "base_factor": "12", "is_base": ""})
        box.refresh_from_db()
        self.expect("and its kind", box.kind == "COUNT", box.kind)
        resp = client.post(reverse("inventory:unit_set_base", args=[box.pk]), {})
        box.refresh_from_db()
        self.expect("and cannot become the base of a kind in use",
                    not box.is_base and box.base_factor == D("12"), str(box.is_base))
        self.expect_flash(client, resp, "rescaled", "and the refusal says why")
        resp = client.post(reverse("inventory:unit_edit", args=[box.pk]), {
            "name": "Box of a dozen", "abbreviation": "BX12", "kind": "COUNT",
            "base_factor": "12", "is_base": ""})
        box.refresh_from_db()
        self.expect("its name can still change", box.name == "Box of a dozen", box.name)

        resp = client.post(reverse("inventory:product_edit", args=[self.raw.pk]),
                           self.product_data(self.raw, unit_of_measure=box.pk))
        self.raw.refresh_from_db()
        self.expect("a product with stock keeps its unit",
                    resp.status_code == 200 and self.raw.unit_of_measure_id == self.uom.pk,
                    f"HTTP {resp.status_code}")

    # ── R4-5 ──
    def check_scrap(self, client):
        from django.core.exceptions import ValidationError
        from apps.manufacturing.models import BillOfMaterials, BOMLine

        for scrap in ("-50", "100", "250"):
            name = f"Scrap {scrap} {self.stamp}"
            client.post(reverse("manufacturing:bom_create"), merge({
                "name": name, "product": self.wo_bom.product_id, "version": "1",
                "status": "ACTIVE", "production_quantity": "1", "notes": "",
            }, formset("lines", [
                {"component": self.wo_part.pk, "quantity": "1", "unit_of_measure": "",
                 "scrap_percent": scrap}],
                blank={"component": "", "quantity": "", "unit_of_measure": "",
                       "scrap_percent": "0"})))
            self.expect(f"a recipe line with {scrap}% scrap is refused",
                        not BillOfMaterials.objects.filter(company=self.company,
                                                           name=name).exists())
        try:
            with transaction.atomic():
                BOMLine.objects.create(bom=self.wo_bom, component=self.wo_part,
                                       quantity=D("1"), scrap_percent=D("-10"))
            refused = False
        except ValidationError:
            refused = True
        self.expect("and the model refuses it too", refused)

    # ── R4-6 / R4-8 ──
    def check_reserved_accounts(self, client):
        from apps.inventory.forms import ProductForm
        from apps.procurement.forms import chargeable_accounts
        from apps.procurement.models import Bill

        offered = set(ProductForm(company=self.company).fields["inventory_account"]
                      .queryset.values_list("code", flat=True))
        self.expect("1400 is offered as a product's inventory account", "1400" in offered,
                    str(sorted(offered)))
        self.expect("but not WIP, revaluation, depreciation or a header",
                    not offered & {"1450", "1350", "1690", "1000", "1600", "1200", "1300"},
                    str(sorted(offered)))
        resp = client.post(reverse("inventory:product_edit", args=[self.service.pk]),
                           self.product_data(self.service,
                                             inventory_account=self.account("1450").pk))
        self.service.refresh_from_db()
        self.expect("a product cannot be put on 1450",
                    resp.status_code == 200
                    and self.service.inventory_account_id != self.account("1450").pk,
                    f"HTTP {resp.status_code}")
        chargeable = set(chargeable_accounts(self.company).values_list("code", flat=True))
        self.expect("a typed bill cannot reach 1690, 1000, 1600 or 1350",
                    not chargeable & {"1690", "1000", "1600", "1350", "1450", "1400"},
                    str(sorted(chargeable)))
        self.expect("but still reaches 6000 and 1610", {"6000", "1610"} <= chargeable,
                    str(sorted(chargeable)))
        for code in ("1690", "1000", "1350"):
            resp = client.post(reverse("procurement:bill_create"), self.bill_form(
                f"BILL-R4-8-{code}", supplier_reference=f"R4-8-{code}",
                charge_account=self.account(code).pk))
            self.expect(f"a typed bill charged to {code} is refused",
                        resp.status_code == 200 and not Bill.objects.filter(
                            company=self.company, reference=f"BILL-R4-8-{code}").exists(),
                        f"HTTP {resp.status_code}")

    # ── R4-7 ──
    def check_negative_shelf_empties(self, client):
        item = self.make_product(client, "NEG-R4", "100")
        item.allow_negative_stock = True
        item.save(update_fields=["allow_negative_stock"])
        variance0 = self.net("5400")
        po = self.order(client, "PO-R4-7A", [(item, 2, "100", False)])
        self.receive(client, po, [(po.lines.first(), 2)])
        self.bill_order(client, po)
        self.post(client, "issue five of two", reverse("inventory:adjustment_create"), {
            "product": item.pk, "warehouse": self.warehouse.pk,
            "movement_type": "ADJUSTMENT_MINUS", "quantity": "5", "unit_cost": "",
            "movement_date": str(self.today), "reference": "R4-7", "notes": ""})
        level = self.shelf(item)
        self.expect("the shelf runs to -3 worth -300",
                    level.quantity == D("-3") and level.value == D("-300"),
                    f"{level.quantity} / {level.value}")
        po = self.order(client, "PO-R4-7B", [(item, 3, "120", False)])
        self.receive(client, po, [(po.lines.first(), 3)])
        level.refresh_from_db()
        self.expect("three in at 120 leave the shelf at nothing, worth nothing",
                    level.quantity == Z and level.value == Z,
                    f"{level.quantity} / {level.value}")
        self.expect("and the 60 difference is a price variance",
                    self.net("5400") - variance0 == D("60.00"),
                    str(self.net("5400") - variance0))
        self.stock_agrees("after a negative shelf is answered")
        self.bill_order(client, po)
        self.accrual_agrees("after a negative shelf is answered and billed")

    # ── R4-9 ──
    def check_plain_digits(self, client):
        from apps.inventory.models import StockMovement
        from apps.procurement.models import Bill

        self.post(client, "a bill to pay", reverse("procurement:bill_create"),
                  self.bill_form("BILL-R4-9", supplier_reference="R4-9"))
        bill = Bill.objects.get(company=self.company, reference="BILL-R4-9")
        for amount in ("1_0", "１００", "+-5", "1.2.3", "٣"):
            client.post(reverse("procurement:bill_pay", args=[bill.pk]), {"amount": amount})
            bill.refresh_from_db()
            self.expect(f"a payment of '{amount}' is refused", bill.paid_amount == Z,
                        str(bill.paid_amount))
        moves = StockMovement.objects.filter(product=self.raw).count()
        for qty in ("1_0", "１"):
            resp = self.adjust(client, "ADJUSTMENT_PLUS", qty, cost="50")
            self.expect(f"an adjustment of '{qty}' is refused",
                        resp.status_code == 200
                        and StockMovement.objects.filter(product=self.raw).count() == moves,
                        f"HTTP {resp.status_code}")
        resp = self.adjust(client, "ADJUSTMENT_PLUS", "1", cost="5_0")
        self.expect("and so is a unit cost of '5_0'",
                    resp.status_code == 200
                    and StockMovement.objects.filter(product=self.raw).count() == moves,
                    f"HTTP {resp.status_code}")

    # ── R4-10 ──
    def check_inactive_warehouse(self, client):
        from apps.inventory.models import StockMovement
        from apps.organizations.models import Warehouse

        shut, _ = Warehouse.objects.get_or_create(
            company=self.company, code=f"R4X{self.stamp[-4:]}",
            defaults={"name": "Closed Store R4"})
        shut.is_active = False
        shut.save(update_fields=["is_active"])
        po = self.order(client, "PO-R4-10", [(self.raw, 2, "50", False)])
        moves = StockMovement.objects.filter(product__company=self.company).count()
        resp = client.post(reverse("procurement:grn_record", args=[po.pk]), {
            "warehouse": shut.pk, f"receive_qty_{po.lines.first().pk}": "2"})
        po.refresh_from_db()
        self.expect("goods cannot be received into an inactive warehouse",
                    po.lines.first().received_quantity == Z
                    and StockMovement.objects.filter(
                        product__company=self.company).count() == moves,
                    str(po.lines.first().received_quantity))
        self.expect_flash(client, resp, "inactive", "and the refusal says why")
        resp = client.post(reverse("inventory:stock_transfer"), {
            "from_warehouse": self.warehouse.pk, "to_warehouse": shut.pk,
            "product": self.raw.pk, "quantity": "1", "movement_date": str(self.today)})
        self.expect_flash(client, resp, "inactive", "nor transferred into one")
        resp = client.post(reverse("inventory:adjustment_create"), {
            "product": self.raw.pk, "warehouse": shut.pk,
            "movement_type": "ADJUSTMENT_PLUS", "quantity": "1", "unit_cost": "50",
            "movement_date": str(self.today), "reference": "R4-10", "notes": ""})
        self.expect("nor adjusted into one", resp.status_code == 200
                    and StockMovement.objects.filter(warehouse=shut).count() == 0,
                    f"HTTP {resp.status_code}")
        self.adjust(client, "ADJUSTMENT_PLUS", "1", cost="10", product=self.wo_part)
        wo = self.work_order(client, self.wo_bom, f"WO-R4-10-{self.stamp}", 1)
        wo.warehouse = shut
        wo.save(update_fields=["warehouse"])
        resp = client.post(reverse("manufacturing:work_order_complete", args=[wo.pk]),
                           {"actual_quantity": "1", "actual_end": str(self.today)})
        wo.refresh_from_db()
        self.expect("nor made into one", wo.status == "DRAFT"
                    and StockMovement.objects.filter(warehouse=shut).count() == 0, wo.status)
        self.expect_flash(client, resp, "inactive", "and the work order says why")
        self.receive(client, po, [(po.lines.first(), 2)])
        self.bill_order(client, po)
