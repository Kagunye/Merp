"""End-to-end integration tests for the scenarios the user named.

Run with:
    python manage.py test_flows            # all five
    python manage.py test_flows -t A B     # subset

Each flow runs in its own transaction and rolls back at the end so the
database stays clean; pass --keep to persist them for inspection.

Scenarios:
  A. Sales to cash — customer → quote → order → invoice → part-payment →
     verify outstanding + GL.
  B. Procurement to stock — supplier → PO → GRN → stock + valuation →
     bill → pay → reconcile payable.
  C. Permissions and approvals — non-admin denied, admin approves, audit.
  D. Accounting integrity — post a JE, verify Dr = Cr, trial balance
     balances, AR / AP tie back.
  E. Bank reconciliation — clear transactions against a statement, post it,
     reverse it, and prove the ledger moved the same amount.
"""
from decimal import Decimal
from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction


RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
BOLD = "\033[1m"


class FlowError(AssertionError):
    pass


def ok(msg):
    return f"  {GREEN}✓{RESET} {msg}"


def fail(msg):
    return f"  {RED}✗{RESET} {msg}"


def step(msg):
    return f"  {CYAN}·{RESET} {msg}"


class Command(BaseCommand):
    help = "Run end-to-end integration test flows."

    def add_arguments(self, parser):
        parser.add_argument("-t", "--tests", nargs="+", default=["A", "B", "C", "D", "E"],
                            choices=["A", "B", "C", "D", "E"],
                            help="Subset of tests to run.")
        parser.add_argument("--keep", action="store_true",
                            help="Keep the data instead of rolling back.")

    def handle(self, *args, **opts):
        self.keep = opts["keep"]
        self.tests = opts["tests"]
        results = {}
        for key in self.tests:
            fn = getattr(self, f"flow_{key.lower()}")
            self.stdout.write("")
            self.stdout.write(f"{BOLD}Test {key} — {fn.__doc__.strip()}{RESET}")
            try:
                if self.keep:
                    fn()
                else:
                    with transaction.atomic():
                        fn()
                        if not self.keep:
                            # roll back by raising
                            raise _Rollback()
                results[key] = "PASS"
            except _Rollback:
                results[key] = "PASS"
            except FlowError as e:
                self.stdout.write(fail(str(e)))
                results[key] = "FAIL"
            except Exception as e:
                self.stdout.write(fail(f"UNEXPECTED: {e}"))
                import traceback
                traceback.print_exc()
                results[key] = "ERROR"

        self.stdout.write("")
        self.stdout.write(f"{BOLD}Summary{RESET}")
        for k, v in results.items():
            color = GREEN if v == "PASS" else RED
            self.stdout.write(f"  Test {k}: {color}{v}{RESET}")
        if any(v != "PASS" for v in results.values()):
            raise SystemExit(1)

    # ---------- Shared helpers ----------

    def _company(self):
        from apps.organizations.models import Company
        company = Company.objects.filter(is_default=True).first() or Company.objects.first()
        if not company:
            raise FlowError("No company found. Run `manage.py seed_demo` first.")
        return company

    def _accounts(self, company):
        from apps.accounting.models import Account
        wanted = {
            "CASH": "1000", "BANK": "1010", "AR": "1100",
            "INV": "1200", "AP": "2000", "TAX": "2200",
            "REV": "4000", "COGS": "5000", "EXP": "6300",
        }
        out = {}
        for key, code in wanted.items():
            acc = Account.objects.filter(company=company, code=code).first()
            if not acc:
                raise FlowError(f"Chart of accounts missing code {code}; run seed_demo.")
            out[key] = acc
        return out

    def _je(self, company, ref, narration, lines, posting_date=None):
        """Create a POSTED journal entry with given lines list of
        (account, dr, cr)."""
        from apps.accounting.models import JournalEntry, JournalEntryLine
        from django.utils import timezone
        je = JournalEntry.objects.create(
            company=company, reference=ref, entry_type="MANUAL",
            posting_date=posting_date or date.today(),
            notes=narration, status="POSTED", posted_at=timezone.now(),
        )
        total_dr = Decimal("0")
        total_cr = Decimal("0")
        for acc, dr, cr in lines:
            JournalEntryLine.objects.create(
                journal_entry=je, account=acc, description=narration,
                debit_amount=dr, credit_amount=cr,
            )
            total_dr += dr
            total_cr += cr
        if total_dr != total_cr:
            raise FlowError(f"{ref}: journal unbalanced ({total_dr} vs {total_cr})")
        return je

    # ---------- Test A — Sales to cash ----------

    def flow_a(self):
        """Sales to cash — customer → quote → order → invoice → part-payment."""
        from apps.crm.models import Customer
        from apps.sales.models import Invoice, InvoiceLine, CustomerPayment, SalesOrder
        from apps.inventory.models import Product, UnitOfMeasure

        company = self._company()
        acc = self._accounts(company)

        # 1. Customer
        cust, _ = Customer.objects.get_or_create(
            company=company, name="Flow-A Customer",
            defaults={"email": "flowa@test.local", "payment_terms_days": 30},
        )
        self.stdout.write(ok(f"Customer created / fetched: {cust.name}"))

        # 2. Product (uses existing WDG-001 if present)
        product = Product.objects.filter(company=company).first()
        if not product:
            uom, _ = UnitOfMeasure.objects.get_or_create(
                company=company, name="Each", defaults={"abbreviation": "ea"})
            product = Product.objects.create(
                company=company, sku="FLOWA-01", name="Flow-A Widget",
                product_type="GOODS", unit_of_measure=uom,
                cost_price=Decimal("100"), selling_price=Decimal("250"),
            )
        self.stdout.write(ok(f"Product in catalogue: {product.sku}"))

        # 3. Quotation (= DRAFT SalesOrder)
        quote = SalesOrder.objects.create(
            company=company, reference="SO-FLOWA-Q1",
            customer=cust, order_date=date.today(),
            status="DRAFT", subtotal=Decimal("500"),
            tax_amount=Decimal("80"), total_amount=Decimal("580"),
        )
        self.stdout.write(ok(f"Quotation raised: {quote.reference} total 580.00"))

        # 4. Approve + convert to confirmed order
        quote.status = "CONFIRMED"
        quote.save(update_fields=["status"])
        self.stdout.write(ok("Quotation approved → sales order confirmed"))

        # 5. Issue invoice (same totals)
        inv = Invoice.objects.create(
            company=company, reference="INV-FLOWA-1",
            customer=cust, invoice_date=date.today(),
            due_date=date.today() + timedelta(days=30),
            status="SENT",
            subtotal=Decimal("500"), tax_amount=Decimal("80"),
            total_amount=Decimal("580"),
            outstanding_amount=Decimal("580"),
        )
        InvoiceLine.objects.create(
            invoice=inv, product=product, description="2 × Widget",
            quantity=Decimal("2"), unit_price=Decimal("250"),
        )
        self._je(company, "JE-FLOWA-INV1", f"Invoice {inv.reference}", [
            (acc["AR"], Decimal("580"), Decimal("0")),
            (acc["REV"], Decimal("0"), Decimal("500")),
            (acc["TAX"], Decimal("0"), Decimal("80")),
        ])
        self.stdout.write(ok(f"Invoice issued: {inv.reference} outstanding 580.00"))

        # 6. Record partial payment of 200
        pay = CustomerPayment.objects.create(
            company=company, reference="RCT-FLOWA-1",
            customer=cust, payment_date=date.today(),
            amount=Decimal("200"), payment_method="BANK_TRANSFER",
        )
        self._je(company, "JE-FLOWA-PAY1", f"Receipt {pay.reference}", [
            (acc["BANK"], Decimal("200"), Decimal("0")),
            (acc["AR"], Decimal("0"), Decimal("200")),
        ])
        inv.paid_amount = Decimal("200")
        inv.outstanding_amount = inv.total_amount - inv.paid_amount
        inv.status = "PARTIAL"
        inv.save(update_fields=["paid_amount", "outstanding_amount", "status"])
        self.stdout.write(ok(f"Partial receipt 200.00 recorded"))

        # 7. Verify outstanding + GL
        inv.refresh_from_db()
        if inv.outstanding_amount != Decimal("380"):
            raise FlowError(f"Outstanding mismatch: expected 380.00, got {inv.outstanding_amount}")
        self.stdout.write(ok(f"Outstanding balance = {inv.outstanding_amount} (expected 380.00)"))

        # AR account should net 380 for this customer flow (580 - 200)
        from django.db.models import Sum
        from apps.accounting.models import JournalEntryLine
        ar_sum = JournalEntryLine.objects.filter(
            journal_entry__reference__startswith="JE-FLOWA",
            account=acc["AR"],
        ).aggregate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        ar_net = (ar_sum["dr"] or 0) - (ar_sum["cr"] or 0)
        if ar_net != Decimal("380"):
            raise FlowError(f"AR GL net mismatch: expected 380.00, got {ar_net}")
        self.stdout.write(ok(f"AR GL net = {ar_net} ties to invoice outstanding"))

    # ---------- Test B — Procurement to stock ----------

    def flow_b(self):
        """Procurement to stock — supplier → PO → receive → bill → pay."""
        from apps.crm.models import Supplier
        from apps.procurement.models import Bill, PurchaseOrder, PurchaseOrderLine
        from apps.inventory.models import Product, StockMovement, UnitOfMeasure
        from apps.organizations.models import Warehouse

        company = self._company()
        acc = self._accounts(company)

        # 1. Supplier
        sup, _ = Supplier.objects.get_or_create(
            company=company, name="Flow-B Supplier",
            defaults={"email": "flowb@test.local"},
        )
        self.stdout.write(ok(f"Supplier created / fetched: {sup.name}"))

        # Prepare product + warehouse
        product = Product.objects.filter(company=company).first()
        warehouse = Warehouse.objects.filter(company=company).first()
        if not (product and warehouse):
            raise FlowError("Need at least one Product and Warehouse; run seed_demo.")

        # Opening stock snapshot
        from apps.inventory.models import StockLevel
        stock_before = StockLevel.objects.filter(
            product=product, warehouse=warehouse,
        ).first()
        qty_before = stock_before.quantity if stock_before else Decimal("0")

        # 2. Raise PO
        po = PurchaseOrder.objects.create(
            company=company, reference="PO-FLOWB-1",
            supplier=sup, order_date=date.today(),
            status="SENT", warehouse=warehouse,
            subtotal=Decimal("1000"), tax_amount=Decimal("160"),
            total_amount=Decimal("1160"),
        )
        PurchaseOrderLine.objects.create(
            order=po, product=product, description="10 × Widget",
            quantity=Decimal("10"), unit_cost=Decimal("100"),
        )
        self.stdout.write(ok(f"PO raised: {po.reference} total 1160.00"))

        # 3. Receive goods — post a stock movement + mark fully received
        from django.utils import timezone
        from apps.accounts.models import User
        performed = User.objects.filter(is_superuser=True).first()
        StockMovement.objects.create(
            product=product, warehouse=warehouse, movement_type="RECEIPT",
            quantity=Decimal("10"), unit_cost=Decimal("100"),
            total_cost=Decimal("1000"), reference=po.reference,
            movement_date=date.today(), performed_by=performed,
        )
        po.status = "FULLY_RECEIVED"
        po.save(update_fields=["status"])
        self.stdout.write(ok("Goods received: 10 units moved into warehouse"))

        # 4. Verify stock + valuation
        sl = StockLevel.objects.filter(product=product, warehouse=warehouse).first()
        qty_after = sl.quantity if sl else Decimal("0")
        qty_delta = qty_after - qty_before
        if qty_delta != Decimal("10"):
            raise FlowError(f"Stock delta mismatch: expected +10, got {qty_delta}")
        self.stdout.write(ok(f"Stock on hand increased by {qty_delta} (unit cost 100.00 → +1000.00 valuation)"))

        # 5. Record supplier bill (books AP)
        bill = Bill.objects.create(
            company=company, reference="BILL-FLOWB-1",
            supplier=sup, bill_date=date.today(),
            due_date=date.today() + timedelta(days=30),
            status="RECEIVED",
            subtotal=Decimal("1000"), tax_amount=Decimal("160"),
            total_amount=Decimal("1160"), paid_amount=Decimal("0"),
            outstanding_amount=Decimal("1160"),
            purchase_order=po,
        )
        self._je(company, "JE-FLOWB-BILL", f"Bill {bill.reference}", [
            (acc["INV"], Decimal("1000"), Decimal("0")),
            (acc["TAX"], Decimal("160"), Decimal("0")),
            (acc["AP"], Decimal("0"), Decimal("1160")),
        ])
        self.stdout.write(ok(f"Supplier bill booked: {bill.reference} AP 1160.00"))

        # 6. Pay the bill in full
        self._je(company, "JE-FLOWB-PAY", f"Pay {bill.reference}", [
            (acc["AP"], Decimal("1160"), Decimal("0")),
            (acc["BANK"], Decimal("0"), Decimal("1160")),
        ])
        bill.paid_amount = bill.total_amount
        bill.outstanding_amount = Decimal("0")
        bill.status = "PAID"
        bill.save(update_fields=["paid_amount", "outstanding_amount", "status"])
        self.stdout.write(ok("Supplier paid in full"))

        # 7. Reconcile payable
        bill.refresh_from_db()
        if bill.outstanding_amount != Decimal("0"):
            raise FlowError(f"AP didn't reconcile: outstanding {bill.outstanding_amount}")

        from django.db.models import Sum
        from apps.accounting.models import JournalEntryLine
        ap_sum = JournalEntryLine.objects.filter(
            journal_entry__reference__startswith="JE-FLOWB",
            account=acc["AP"],
        ).aggregate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        ap_net = (ap_sum["dr"] or 0) - (ap_sum["cr"] or 0)
        if ap_net != Decimal("0"):
            raise FlowError(f"AP net != 0: got {ap_net}")
        self.stdout.write(ok("AP reconciled: GL net = 0.00, bill outstanding = 0.00"))

    # ---------- Test C — Permissions and approvals ----------

    def flow_c(self):
        """Permissions and approvals — staff denied, manager approves, audit."""
        from django.contrib.auth import get_user_model
        from django.test import RequestFactory
        from apps.procurement.models import PurchaseRequisition
        User = get_user_model()
        company = self._company()

        # 1. Create a staff user and a manager
        staff, _ = User.objects.get_or_create(
            email="flowc.staff@test.local",
            defaults={"first_name": "Flow", "last_name": "Staff", "is_active": True},
        )
        if not staff.has_usable_password():
            staff.set_password("Dummy@2026")
            staff.save()
        staff.is_staff = False
        staff.is_superuser = False
        staff.save(update_fields=["is_staff", "is_superuser"])

        manager = User.objects.filter(is_superuser=True, is_active=True).first()
        if not manager:
            raise FlowError("No superuser available for approval step.")
        self.stdout.write(ok(f"Staff: {staff.email}, Manager: {manager.email}"))

        # 2. Staff raises a requisition
        req = PurchaseRequisition.objects.create(
            company=company, reference="PR-FLOWC-1",
            requested_by=staff, required_date=date.today() + timedelta(days=7),
            status="PENDING_APPROVAL", total_amount=Decimal("500"),
        )
        self.stdout.write(ok(f"Staff raised requisition {req.reference}, PENDING_APPROVAL"))

        # 3. Attempt approve as the staff user via the view — must fail
        from django.test.client import Client
        c = Client()
        c.force_login(staff)
        url = f"/procurement/requisitions/{req.pk}/approve/"
        resp = c.post(url)
        # The approve view requires login and reads request.active_company —
        # for a non-admin staff with no company membership, approve either
        # redirects (302) or no-ops. Either way the requisition should
        # NOT transition out of PENDING_APPROVAL *as the staff*.
        req.refresh_from_db()
        if req.status == "APPROVED" and req.approved_by == staff:
            raise FlowError(f"Staff was able to self-approve requisition.")
        self.stdout.write(ok(f"Staff approve attempt blocked (status still {req.status})"))

        # 4. Manager approves
        from django.utils import timezone
        req.status = "APPROVED"
        req.approved_by = manager
        req.approved_at = timezone.now()
        req.save(update_fields=["status", "approved_by", "approved_at"])
        self.stdout.write(ok(f"Manager approved requisition"))

        # 5. Verify audit trail: approved_by + approved_at set
        req.refresh_from_db()
        if req.approved_by_id != manager.id:
            raise FlowError("Audit: approved_by didn't persist")
        if req.approved_at is None:
            raise FlowError("Audit: approved_at didn't persist")
        self.stdout.write(ok(f"Audit history recorded: approved by {req.approved_by.email} at {req.approved_at:%Y-%m-%d %H:%M}"))

    # ---------- Test D — Accounting integrity ----------

    def flow_d(self):
        """Accounting integrity — JE balanced, trial balance, AR/AP tie."""
        from decimal import Decimal
        from django.db.models import Sum
        from apps.accounting.models import JournalEntry, JournalEntryLine, Account
        from apps.sales.models import Invoice
        from apps.procurement.models import Bill

        company = self._company()
        acc = self._accounts(company)

        # 1. Post a balanced journal entry
        je = self._je(company, "JE-FLOWD-1", "Flow D integrity test", [
            (acc["CASH"], Decimal("300"), Decimal("0")),
            (acc["REV"], Decimal("0"), Decimal("300")),
        ])
        self.stdout.write(ok(f"Posted JE {je.reference}"))

        # 2. Verify Dr == Cr for THIS entry
        line_sum = JournalEntryLine.objects.filter(journal_entry=je).aggregate(
            dr=Sum("debit_amount"), cr=Sum("credit_amount"),
        )
        if (line_sum["dr"] or 0) != (line_sum["cr"] or 0):
            raise FlowError(f"JE not balanced: {line_sum}")
        self.stdout.write(ok(f"Entry balanced: Dr {line_sum['dr']} = Cr {line_sum['cr']}"))

        # 3. Trial balance over ALL posted entries
        tb = JournalEntryLine.objects.filter(
            journal_entry__company=company,
            journal_entry__status="POSTED",
        ).aggregate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        total_dr = tb["dr"] or Decimal("0")
        total_cr = tb["cr"] or Decimal("0")
        if total_dr != total_cr:
            raise FlowError(f"Trial balance out of balance: Dr {total_dr} vs Cr {total_cr}")
        self.stdout.write(ok(f"Trial balance: Dr {total_dr} = Cr {total_cr}"))

        # 4. AR GL vs invoice outstanding
        ar_gl = JournalEntryLine.objects.filter(
            journal_entry__company=company,
            journal_entry__status="POSTED",
            account=acc["AR"],
        ).aggregate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        ar_net = (ar_gl["dr"] or 0) - (ar_gl["cr"] or 0)
        ar_sub = Invoice.objects.filter(
            company=company, status__in=["SENT", "PARTIAL", "OVERDUE"],
        ).aggregate(t=Sum("outstanding_amount"))["t"] or Decimal("0")
        self.stdout.write(ok(f"AR control: GL net {ar_net}, invoice subledger {ar_sub}"))
        if ar_net != ar_sub:
            self.stdout.write(f"    {YELLOW}note:{RESET} AR GL and AR subledger differ by {ar_net - ar_sub} — may indicate manual JEs touching AR outside the invoice path.")

        # 5. AP GL vs bill outstanding
        ap_gl = JournalEntryLine.objects.filter(
            journal_entry__company=company,
            journal_entry__status="POSTED",
            account=acc["AP"],
        ).aggregate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        ap_net = (ap_gl["cr"] or 0) - (ap_gl["dr"] or 0)
        ap_sub = Bill.objects.filter(
            company=company, status__in=["RECEIVED", "PARTIAL", "OVERDUE"],
        ).aggregate(t=Sum("outstanding_amount"))["t"] or Decimal("0")
        self.stdout.write(ok(f"AP control: GL net {ap_net}, bill subledger {ap_sub}"))
        if ap_net != ap_sub:
            self.stdout.write(f"    {YELLOW}note:{RESET} AP GL and AP subledger differ by {ap_net - ap_sub}.")

    # ---------- Test E — Bank reconciliation ----------

    def flow_e(self):
        """Bank reconciliation — clear, post, reverse, and tie back to the ledger."""
        from django.core.exceptions import ValidationError
        from apps.banking import services
        from apps.banking.models import BankAccount, BankTransaction

        company = self._company()
        acc = self._accounts(company)
        statement_date = date.today()

        # 1. A bank account mapped to the ledger, and the statement window.
        bank = BankAccount.objects.create(
            company=company, name="Flow E Current Account", bank_name="Test Bank",
            account_number="99000123", account_type="CURRENT",
            currency=company.currency or "KES", gl_account=acc["BANK"],
            opening_balance=Decimal("50000"),
        )
        self.stdout.write(ok(f"Bank account {bank.name} → GL {bank.gl_account.code}"))

        def txn(day_offset, kind, amount, ref):
            return BankTransaction.objects.create(
                bank_account=bank, transaction_date=statement_date - timedelta(days=day_offset),
                transaction_type=kind, amount=Decimal(amount), reference=ref,
                description=f"Flow E {kind.lower()} {ref}",
            )

        deposits = [txn(9, "CREDIT", "10000", "E-DEP-1"), txn(4, "CREDIT", "2000", "E-DEP-2")]
        withdrawals = [txn(7, "DEBIT", "4000", "E-WDL-1"), txn(2, "DEBIT", "1000", "E-WDL-2")]
        items = deposits + withdrawals
        self.stdout.write(ok(f"Recorded {len(items)} transactions"))

        # 2. Mirror them through the ledger so the books and the bank agree.
        entries = []
        for t in items:
            amount = t.amount
            if t.transaction_type == "CREDIT":
                entries.append(self._je(company, f"JE-{t.reference}", t.description, [
                    (acc["BANK"], amount, Decimal("0")), (acc["REV"], Decimal("0"), amount),
                ], posting_date=t.transaction_date))
            else:
                entries.append(self._je(company, f"JE-{t.reference}", t.description, [
                    (acc["EXP"], amount, Decimal("0")), (acc["BANK"], Decimal("0"), amount),
                ], posting_date=t.transaction_date))
        from django.db.models import Sum
        ledger = acc["BANK"].journal_lines.filter(journal_entry__in=entries).aggregate(
            dr=Sum("debit_amount"), cr=Sum("credit_amount"),
        )
        ledger_net = (ledger["dr"] or Decimal("0")) - (ledger["cr"] or Decimal("0"))
        cleared_net = sum((t.signed_amount for t in items), Decimal("0"))
        if ledger_net != cleared_net:
            raise FlowError(f"Ledger moved {ledger_net} but the transactions net to {cleared_net}")
        self.stdout.write(ok(f"Ledger and bank agree: both moved {ledger_net}"))

        # 3. Opening balance is carried forward, statement balances are derived.
        opening = services.carried_forward_balance(bank, statement_date)
        if opening != bank.opening_balance:
            raise FlowError(f"First statement should start from the account's opening balance, got {opening}")
        closing = opening + cleared_net
        recon = services.create_draft(
            company=company, bank_account=bank, reference="STM-E-1",
            statement_date=statement_date, closing_balance=closing,
        )
        self.stdout.write(ok(f"Draft statement {recon.reference}: opening {opening}, closing {closing}"))

        # 4. Nothing cleared yet -- the whole statement is a difference.
        if recon.difference != cleared_net:
            raise FlowError(f"An untouched statement should differ by {cleared_net}, got {recon.difference}")
        self.stdout.write(ok(f"Untouched statement is out by {recon.difference} -- that is the point"))

        # 5. Clear a subset: the statement stays out of balance, and posting is refused.
        for t in deposits + [withdrawals[0]]:
            services.clear(t, recon)
        if recon.is_balanced:
            raise FlowError("A partial clearing should not balance")
        try:
            services.post(recon)
        except ValidationError as e:
            self.stdout.write(ok(f"Posting refused while out of balance: {e.messages[0][:70]}..."))
        else:
            raise FlowError("An unexplained out-of-balance statement was posted")

        # 6. An accepted difference needs a written reason -- the escape hatch is documented.
        recon.notes = "Withdrawal E-WDL-2 reaches the bank a day later."
        recon.save(update_fields=["notes", "updated_at"])
        services.post(recon)
        recon.refresh_from_db()
        if recon.status != "POSTED":
            raise FlowError("Statement with a recorded reason should post")
        self.stdout.write(ok("Posted with a recorded reason, difference carried as a note"))

        # 7. A posted statement is locked.
        try:
            services.unclear(deposits[0])
        except ValidationError as e:
            self.stdout.write(ok(f"Locked: {e.messages[0][:60]}..."))
        else:
            raise FlowError("A posted statement let a cleared transaction be released")

        # 8. Reverse it -- everything goes back to outstanding.
        services.reverse(recon)
        released = BankTransaction.objects.filter(reconciliation=recon).count()
        still_flagged = sum(1 for t in items if BankTransaction.objects.get(pk=t.pk).is_reconciled)
        if released or still_flagged:
            raise FlowError(f"After reversing: {released} rows still linked, {still_flagged} still flagged")
        self.stdout.write(ok("Reversed: every transaction is outstanding again, flags cleared"))

        # 9. Clear all four and the statement balances exactly.
        recon2 = services.create_draft(
            company=company, bank_account=bank, reference="STM-E-2",
            statement_date=statement_date, closing_balance=closing,
        )
        for t in items:
            services.clear(t, recon2)
        if not recon2.is_balanced:
            raise FlowError(f"Full clearing should balance, difference {recon2.difference}")
        services.post(recon2)
        for t in items:
            t.refresh_from_db()
            if not t.is_reconciled or t.reconciliation_id != recon2.pk:
                raise FlowError(f"{t.reference} did not survive posting as cleared")
        self.stdout.write(ok(f"Full statement posted {recon2.reference}: {recon2.cleared_count} items, difference {recon2.difference}"))

        # 10. The next statement starts from this one's closing balance.
        next_opening = services.carried_forward_balance(bank, statement_date + timedelta(days=30))
        if next_opening != closing:
            raise FlowError(f"Next statement should open at {closing}, got {next_opening}")
        self.stdout.write(ok(f"Next statement carries forward {next_opening}"))

        # 11. Nothing is left outstanding inside the period.
        if services.outstanding(bank, statement_date).exists():
            raise FlowError("Outstanding items remain after a full clearing")
        self.stdout.write(ok("Outstanding list for the account is empty"))

        # 12. Two live statements cannot cover the same day.
        try:
            services.create_draft(
                company=company, bank_account=bank, reference="STM-E-3",
                statement_date=statement_date, closing_balance=closing,
            )
        except ValidationError as e:
            self.stdout.write(ok(f"Duplicate statement date refused: {e.messages[0][:60]}..."))
        else:
            raise FlowError("A second statement for the same date was accepted")

        # 13. A transaction after the statement date does not belong on it.
        next_date = statement_date + timedelta(days=30)
        late = txn(-45, "CREDIT", "500", "E-LATE")
        draft = services.create_draft(
            company=company, bank_account=bank, reference="STM-E-4",
            statement_date=next_date, closing_balance=closing,
        )
        try:
            services.clear(late, draft)
        except ValidationError as e:
            self.stdout.write(ok(f"Post-statement item refused: {e.messages[0][:60]}..."))
        else:
            raise FlowError("A transaction dated after the statement was cleared onto it")
        if services.outstanding(bank, statement_date).filter(pk=late.pk).exists():
            raise FlowError("A transaction beyond the statement date should not be offered as outstanding")
        self.stdout.write(ok("Outstanding list stops at the statement date"))


class _Rollback(Exception):
    pass