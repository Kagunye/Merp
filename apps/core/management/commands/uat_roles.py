"""Role-agent UAT: real work plus bad transactions, across company types.

Three companies, each using the modules its business needs:

  * Duka Supermarket   (retail)        — sales, POS, stock, purchasing, ledger
  * Jenga Fabricators  (manufacturing) — bills of materials, work orders,
                                          warehouse transfers, purchasing, HR, payroll
  * Ushauri Consulting (services)      — projects, timesheets, expense claims,
                                          fixed assets, banking, leave, payroll

Every company has an agent for each work role. Each agent signs in through
the real login page and first does its normal job (the module UAT). It then
tries the mistakes and abuses someone in that role could make. Each bad
attempt is rolled back after it is measured and classified as:

  guarded   refused, with a reason shown to the person
  silent    refused, but nothing tells the person why
  accepted  no guardrail: the bad transaction went through
  crashed   the page failed

    python manage.py uat_roles
    python manage.py uat_roles --report path/to/report.md
"""
from __future__ import annotations

import secrets
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum
from django.test import override_settings
from django.test.utils import setup_test_environment, teardown_test_environment
from django.urls import reverse

from apps.core.uat_framework import (
    BOLD, CYAN, D, DIM, GREEN, MAGENTA, RED, RESET, YELLOW, Agent, Harness,
)

ROLES = ["ADMIN", "FINANCE_MANAGER", "ACCOUNTANT", "SALES_MANAGER", "SALES_REP", "POS_CASHIER",
         "PROCUREMENT", "WAREHOUSE_MANAGER", "HR_MANAGER", "PAYROLL_OFFICER", "PROJECT_MANAGER",
         "EMPLOYEE", "AUDITOR", "VIEWER"]
NAMES = ["Amina", "Baraka", "Chebet", "Daudi", "Esther", "Faith", "Githinji", "Halima", "Imani",
         "Juma", "Kerubo", "Lemayian", "Muthoni", "Njeri"]


class _Rollback(Exception):
    pass


class World:
    """One company with its people, chart, stock and master data."""

    def __init__(self, kind, name, tag):
        self.kind, self.name, self.tag = kind, name, tag
        self.agents: dict[str, Agent] = {}
        self.employees = {}


class Command(BaseCommand):
    help = "Role agents do real work and attempt bad transactions in three company types."

    def add_arguments(self, parser):
        parser.add_argument("--report", default="")
        parser.add_argument("--keep", action="store_true")

    def handle(self, *args, **opts):
        self.h = Harness(self.stdout)
        self.today = date.today()
        first_this_month = self.today.replace(day=1)
        self.last_month_end = first_this_month - timedelta(days=1)
        self.last_month_start = self.last_month_end.replace(day=1)
        setup_test_environment()
        try:
            with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                                   ALLOWED_HOSTS=["testserver", "localhost"]):
                if opts["keep"]:
                    self._run()
                else:
                    try:
                        with transaction.atomic():
                            self._run()
                            raise _Rollback()
                    except _Rollback:
                        pass
        finally:
            teardown_test_environment()
        report = self._report()
        if opts["report"]:
            with open(opts["report"], "w") as fh:
                fh.write(report)
            self.stdout.write(f"{DIM}Report written to {opts['report']}{RESET}")
        bad = [p for p in self.h.probes if p.outcome.kind in ("accepted", "crashed")]
        failed_steps = [s for s in self.h.steps if not s.ok]
        if bad or failed_steps:
            raise SystemExit(1)

    def _run(self):
        plans = [
            ("retail", "Duka Supermarket", [self.m_sales, self.m_receipts, self.m_ledger, self.m_stock,
                                            self.m_pos, self.m_purchasing, self.m_oversight]),
            ("manufacturing", "Jenga Fabricators", [self.m_manufacturing, self.m_stock_moves, self.m_purchasing,
                                                    self.m_hr, self.m_payroll, self.m_oversight]),
            ("services", "Ushauri Consulting", [self.m_projects, self.m_timesheets, self.m_expenses,
                                                self.m_assets, self.m_banking, self.m_leave, self.m_payroll,
                                                self.m_sales_services, self.m_oversight]),
        ]
        for kind, name, modules in plans:
            w = self._world(kind, name)
            self.stdout.write(f"\n{BOLD}{CYAN}{w.company.name}{RESET} {DIM}({kind}; {len(w.agents)} role agents){RESET}")
            for module in modules:
                self.stdout.write(f"  {BOLD}{module.__doc__}{RESET}")
                try:
                    module(w)
                except Exception as exc:
                    import traceback
                    frame = traceback.extract_tb(exc.__traceback__)[-1]
                    self.h.step(w.company, module.__name__[2:], None, f"{module.__doc__} — module run stopped",
                                lambda: (False, f"{type(exc).__name__}: {exc} ({frame.filename.split('/Merp/')[-1]}:{frame.lineno})"))

    # ================================================================ world

    def _world(self, kind, name):
        from apps.accounting.standard_chart import load_standard_chart
        from apps.accounting.models import Account, TaxRate
        from apps.accounts.models import User
        from apps.crm.models import Customer, Supplier
        from apps.hr.models import Contract, Employee, LeaveType
        from apps.inventory.models import Product, ProductCategory, UnitOfMeasure
        from apps.organizations.models import (AccountingPeriod, Branch, Company, CompanyMembership,
                                               Department, FiscalYear, Warehouse)
        import calendar

        tag = self.h.tag
        w = World(kind, name, tag)
        co = Company.objects.create(name=f"{name} {tag}", currency="KES", city="Nairobi", country="Kenya",
                                    tax_id=f"P05{secrets.randbelow(10**7):07d}Z")
        w.company = co
        load_standard_chart(co)
        w.acc = {a.code: a for a in Account.objects.filter(company=co)}
        w.vat = TaxRate.objects.get(company=co, code="VAT16")
        fy = FiscalYear.objects.create(company=co, name=f"FY{self.today.year}", start_date=date(self.today.year, 1, 1),
                                       end_date=date(self.today.year, 12, 31), is_current=True)
        for m in range(1, 13):
            AccountingPeriod.objects.create(fiscal_year=fy, name=f"P{m:02d}", start_date=date(self.today.year, m, 1),
                                            end_date=date(self.today.year, m, calendar.monthrange(self.today.year, m)[1]))
        w.branch = Branch.objects.create(company=co, name="HQ", code="HQ", is_headquarters=True)
        w.dept = Department.objects.create(company=co, name="Operations")
        w.wh1 = Warehouse.objects.create(company=co, name="Main Store", code=f"MS{tag}"[:20], is_default=True)
        w.wh2 = Warehouse.objects.create(company=co, name="Branch Store", code=f"BS{tag}"[:20])
        w.uom, _ = UnitOfMeasure.objects.get_or_create(company=co, name="Each", defaults={"abbreviation": "ea"})
        w.category = ProductCategory.objects.create(company=co, name="General")
        w.product = Product.objects.create(company=co, sku=f"P1-{tag}", name="Unga 2kg", product_type="GOODS",
                                           unit_of_measure=w.uom, cost_price=D("150"), selling_price=D("200"),
                                           track_inventory=True)
        w.component = Product.objects.create(company=co, sku=f"C1-{tag}", name="Steel sheet", product_type="GOODS",
                                             unit_of_measure=w.uom, cost_price=D("500"), selling_price=D("700"),
                                             track_inventory=True)
        w.finished = Product.objects.create(company=co, sku=f"F1-{tag}", name="Steel gate", product_type="GOODS",
                                            unit_of_measure=w.uom, cost_price=D("3000"), selling_price=D("5000"),
                                            track_inventory=True)
        w.customer = Customer.objects.create(company=co, name=f"Mama Njeri Shop {tag}", credit_limit=D("50000"),
                                             payment_terms_days=30, email="mama@njeri.co.ke")
        w.dormant = Customer.objects.create(company=co, name=f"Closed Account {tag}", is_active=False)
        w.supplier = Supplier.objects.create(company=co, name=f"Bidco {tag}", email="ar@bidco.co.ke")
        w.leave_type = LeaveType.objects.create(company=co, name="Annual leave", days_per_year=21)

        for i, role in enumerate(ROLES):
            first = NAMES[i]
            pw = secrets.token_urlsafe(14)
            u = User.objects.create(email=f"{role.lower()}.{kind}.{tag.lower()}@uat-roles.invalid",
                                    username=f"{role.lower()}-{kind}-{tag.lower()}", first_name=first,
                                    last_name=kind.title(), is_active=True)
            u.set_password(pw)
            u.save()
            CompanyMembership.objects.create(company=co, user=u, role=role, is_default=True)
            emp = Employee.objects.create(company=co, user=u, employee_number=f"E{i:02d}-{tag}", first_name=first,
                                          last_name=kind.title(), date_hired=date(2022, 1, 10),
                                          date_of_birth=date(1990, 5, 1), status="ACTIVE", department=w.dept,
                                          kra_pin=f"A00{secrets.randbelow(10**7):07d}B")
            Contract.objects.create(employee=emp, start_date=date(2022, 1, 10), basic_salary=D("80000"),
                                    job_title=role.replace("_", " ").title(), is_current=True)
            w.employees[role] = emp
            agent = Agent(company=co, role=role, user=u, password=pw, name=f"{first} ({role.replace('_', ' ').title()})")
            if not agent.login():
                self.h.step(co, "people", agent, f"{agent.name} signs in", lambda: (False, "login refused"))
            w.agents[role] = agent
        self.h.step(co, "people", None, f"{len(w.agents)} role agents signed in through the login page",
                    lambda: (all(a.client for a in w.agents.values()), ""))
        return w

    # ================================================================ helpers

    def a(self, w, role):
        return w.agents[role]

    def ref(self, w, prefix):
        return f"{prefix}-{secrets.token_hex(3).upper()}"

    def bal(self, w, code):
        from apps.accounting.models import JournalEntryLine
        acc = w.acc[code]
        agg = JournalEntryLine.objects.filter(account=acc, journal_entry__status="POSTED").aggregate(
            dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        dr, cr = agg["dr"] or D(0), agg["cr"] or D(0)
        return dr - cr if acc.normal_balance == "DEBIT" else cr - dr

    def stock(self, product, warehouse):
        from apps.inventory.models import StockLevel
        sl = StockLevel.objects.filter(product=product, warehouse=warehouse).first()
        return sl.quantity if sl else D(0)

    def step(self, w, module, role, name, fn):
        return self.h.step(w.company, module, self.a(w, role) if role else None, name, fn)

    def probe(self, w, module, role, name, risk, severity, attempt):
        return self.h.probe(w.company, module, self.a(w, role), name, risk, severity, attempt)

    # ---- form payload builders ------------------------------------------
    def invoice_payload(self, w, ref, qty="2", price="1000", discount="0", vat=True, customer=None,
                        invoice_date=None, due_date=None):
        d = invoice_date or self.today
        return {"reference": ref, "customer": str((customer or w.customer).pk), "invoice_date": d.isoformat(),
                "due_date": (due_date or d + timedelta(days=30)).isoformat(), "currency": "KES", "notes": "",
                "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "1",
                "lines-MAX_NUM_FORMS": "1000", "lines-0-product": str(w.product.pk), "lines-0-description": "Unga",
                "lines-0-quantity": qty, "lines-0-unit_price": price, "lines-0-discount_percent": discount,
                "lines-0-tax_rate": str(w.vat.pk) if vat else ""}

    def je_payload(self, w, ref, lines, posting_date=None):
        data = {"reference": ref, "entry_type": "MANUAL", "posting_date": (posting_date or self.today).isoformat(),
                "notes": ref, "lines-TOTAL_FORMS": str(len(lines)), "lines-INITIAL_FORMS": "0",
                "lines-MIN_NUM_FORMS": "2", "lines-MAX_NUM_FORMS": "1000"}
        for i, (code, dr, cr) in enumerate(lines):
            data.update({f"lines-{i}-account": str(w.acc[code].pk), f"lines-{i}-description": ref,
                         f"lines-{i}-debit_amount": str(dr), f"lines-{i}-credit_amount": str(cr)})
        return data

    def bill_payload(self, w, ref, sup_ref, sub="1000", tax="160", total="1160", bill_date=None, due_date=None):
        d = bill_date or self.today
        return {"reference": ref, "supplier": str(w.supplier.pk), "supplier_reference": sup_ref,
                "bill_date": d.isoformat(), "due_date": (due_date or d + timedelta(days=30)).isoformat(),
                "expense_account": "", "subtotal": sub, "tax_amount": tax, "total_amount": total, "notes": ""}

    def stock_in(self, w, product, qty, warehouse=None):
        from apps.inventory.models import StockMovement
        StockMovement.objects.create(product=product, warehouse=warehouse or w.wh1, movement_type="OPENING",
                                     quantity=D(qty), unit_cost=product.cost_price, movement_date=self.today,
                                     reference=f"OPEN-{product.sku}")

    # ================================================================ retail

    def m_sales(self, w):
        """Sales — the sales rep and sales manager"""
        from apps.sales.models import Invoice
        rep = self.a(w, "SALES_REP")
        url = reverse("sales:invoice_create")

        def good():
            r = self.ref(w, "INV")
            rep.post(url, self.invoice_payload(w, r))
            inv = Invoice.objects.filter(company=w.company, reference=r).first()
            ok = inv and inv.total_amount == D("2320") and inv.status == "SENT" and inv.journal_entry_id
            return bool(ok), f"got {inv and (inv.total_amount, inv.status, bool(inv.journal_entry_id))}"
        self.step(w, "sales", "SALES_REP", "raise a 2,320 VAT invoice that posts to the ledger", good)

        def made(ref):
            return Invoice.objects.filter(company=w.company, reference=ref).exists()

        def neg_qty():
            r = self.ref(w, "INV"); rep.post(url, self.invoice_payload(w, r, qty="-5")); return made(r), "negative quantity saved"
        self.probe(w, "sales", "SALES_REP", "invoice with a negative quantity",
                   "a negative sale reduces revenue and receivables without a credit note", "HIGH", neg_qty)

        def neg_price():
            r = self.ref(w, "INV"); rep.post(url, self.invoice_payload(w, r, price="-1000")); return made(r), "negative price saved"
        self.probe(w, "sales", "SALES_REP", "invoice with a negative unit price",
                   "hides a refund inside an invoice", "HIGH", neg_price)

        def big_discount():
            r = self.ref(w, "INV"); rep.post(url, self.invoice_payload(w, r, discount="150")); return made(r), "150% discount saved"
        self.probe(w, "sales", "SALES_REP", "150% discount on a line",
                   "turns a sale into a payout to the customer", "HIGH", big_discount)

        def neg_discount():
            r = self.ref(w, "INV"); rep.post(url, self.invoice_payload(w, r, discount="-20")); return made(r), "-20% discount saved"
        self.probe(w, "sales", "SALES_REP", "negative discount (-20%)",
                   "silently overcharges the customer above list price", "MEDIUM", neg_discount)

        def due_before():
            r = self.ref(w, "INV")
            rep.post(url, self.invoice_payload(w, r, due_date=self.today - timedelta(days=10)))
            return made(r), "due date 10 days before invoice date"
        self.probe(w, "sales", "SALES_REP", "due date before the invoice date",
                   "invoice is overdue the moment it's raised; ageing is wrong", "MEDIUM", due_before)

        def dup_ref():
            r = self.ref(w, "INV")
            rep.post(url, self.invoice_payload(w, r)); rep.post(url, self.invoice_payload(w, r))
            n = Invoice.objects.filter(company=w.company, reference=r).count()
            return n > 1, f"{n} invoices share {r}"
        self.probe(w, "sales", "SALES_REP", "two invoices with the same number",
                   "KRA requires unique invoice numbers; duplicates confuse receipts", "HIGH", dup_ref)

        def over_limit():
            r = self.ref(w, "INV")
            rep.post(url, self.invoice_payload(w, r, qty="300", price="200"))
            return made(r), "69,600 invoice to a customer with a 50,000 credit limit"
        self.probe(w, "sales", "SALES_REP", "invoice that takes a customer over their credit limit",
                   "credit control is bypassed; bad-debt exposure", "MEDIUM", over_limit)

        def dormant():
            r = self.ref(w, "INV"); rep.post(url, self.invoice_payload(w, r, customer=w.dormant))
            return made(r), "invoiced an inactive customer"
        self.probe(w, "sales", "SALES_REP", "invoice to an inactive customer",
                   "sales to closed accounts", "MEDIUM", dormant)

        def rep_posts_receipt():
            from apps.sales.models import CustomerPayment
            r = self.ref(w, "RCT")
            rep.post(reverse("sales:payment_create"), {"reference": r, "customer": str(w.customer.pk),
                     "payment_date": self.today.isoformat(), "amount": "500", "payment_method": "CASH",
                     "transaction_reference": "", "notes": ""})
            return CustomerPayment.objects.filter(company=w.company, reference=r).exists(), "sales rep recorded cash"
        self.probe(w, "sales", "SALES_REP", "sales rep records a cash receipt",
                   "whoever sells shouldn't also book the cash (segregation of duties)", "HIGH", rep_posts_receipt)

    def m_receipts(self, w):
        """Receipts — the accountant"""
        from apps.sales.models import CustomerPayment
        acct = self.a(w, "ACCOUNTANT")
        url = reverse("sales:payment_create")

        def pay(amount, ref=None):
            r = ref or self.ref(w, "RCT")
            acct.post(url, {"reference": r, "customer": str(w.customer.pk), "payment_date": self.today.isoformat(),
                            "amount": amount, "payment_method": "BANK_TRANSFER", "transaction_reference": "",
                            "notes": ""})
            return r

        def good():
            before = self.bal(w, "1200")
            r = pay("1000")
            p = CustomerPayment.objects.filter(company=w.company, reference=r).first()
            return bool(p and p.journal_entry_id and self.bal(w, "1200") - before == D("1000")), ""
        self.step(w, "receipts", "ACCOUNTANT", "record a 1,000 receipt into the bank", good)

        def zero():
            r = pay("0"); return CustomerPayment.objects.filter(company=w.company, reference=r).exists(), "0.00 receipt saved"
        self.probe(w, "receipts", "ACCOUNTANT", "a receipt of 0.00", "empty receipts clutter the cash book", "MEDIUM", zero)

        def negative():
            r = pay("-5000"); return CustomerPayment.objects.filter(company=w.company, reference=r).exists(), "-5,000 receipt saved"
        self.probe(w, "receipts", "ACCOUNTANT", "a negative receipt",
                   "money leaves the business disguised as a receipt", "HIGH", negative)

        def dup():
            r = self.ref(w, "RCT"); pay("100", r); pay("100", r)
            n = CustomerPayment.objects.filter(company=w.company, reference=r).count()
            return n > 1, f"{n} receipts share {r}"
        self.probe(w, "receipts", "ACCOUNTANT", "the same receipt entered twice",
                   "double-counted cash: bank looks richer than it is", "HIGH", dup)

        def overpay():
            owed = w.customer.invoices.filter(status__in=["SENT", "PARTIAL", "OVERDUE"]).aggregate(
                t=Sum("outstanding_amount"))["t"] or D(0)
            r = pay(str(owed + D("50000")))
            return CustomerPayment.objects.filter(company=w.company, reference=r).exists(), \
                f"receipt 50,000 more than the {owed:,.2f} owed, with no credit recorded"
        self.probe(w, "receipts", "ACCOUNTANT", "a receipt far larger than the customer owes",
                   "excess cash vanishes into receivables with no customer credit or refund trail", "MEDIUM", overpay)

        def nan():
            r = pay("NaN"); return CustomerPayment.objects.filter(company=w.company, reference=r).exists(), "NaN accepted"
        self.probe(w, "receipts", "ACCOUNTANT", "amount typed as 'NaN'", "garbage input", "MEDIUM", nan)

    def m_ledger(self, w):
        """General ledger — the accountant"""
        from apps.accounting.models import JournalEntry
        acct = self.a(w, "ACCOUNTANT")
        create, post = reverse("accounting:journal_entry_create"), "accounting:journal_entry_post"

        def je(ref, lines, posting_date=None, do_post=True):
            acct.post(create, self.je_payload(w, ref, lines, posting_date))
            e = JournalEntry.objects.filter(company=w.company, reference=ref).first()
            if e and do_post:
                acct.post(reverse(post, args=[e.pk]))
                e.refresh_from_db()
            return e

        def good():
            e = je(self.ref(w, "JE"), [("6200", 1000, 0), ("1200", 0, 1000)])
            return bool(e and e.status == "POSTED"), ""
        self.step(w, "ledger", "ACCOUNTANT", "post a balanced rent journal", good)

        def unbalanced():
            e = je(self.ref(w, "JE"), [("6200", 1000, 0), ("1200", 0, 900)])
            return bool(e and e.status == "POSTED"), "posted 1,000 / 900"
        self.probe(w, "ledger", "ACCOUNTANT", "post an unbalanced journal", "books stop balancing", "CRITICAL", unbalanced)

        def both_sides():
            e = je(self.ref(w, "JE"), [("6200", 500, 500), ("1200", 0, 0)], do_post=False)
            return e is not None, "line with both debit and credit saved"
        self.probe(w, "ledger", "ACCOUNTANT", "a line with both a debit and a credit",
                   "meaningless line that hides amounts", "MEDIUM", both_sides)

        def zero_je():
            e = je(self.ref(w, "JE"), [("6200", 0, 0), ("1200", 0, 0)])
            return bool(e and e.status == "POSTED"), "all-zero journal posted"
        self.probe(w, "ledger", "ACCOUNTANT", "post a journal where every line is zero",
                   "empty postings clutter the audit trail", "MEDIUM", zero_je)

        def far_future():
            e = je(self.ref(w, "JE"), [("6200", 777, 0), ("1200", 0, 777)], posting_date=date(2099, 1, 1))
            return bool(e and e.status == "POSTED"), "posted on 1 Jan 2099"
        self.probe(w, "ledger", "ACCOUNTANT", "post a journal dated 2099 (outside any fiscal year)",
                   "a typo in the year moves money out of this year's books", "HIGH", far_future)

        def far_past():
            e = je(self.ref(w, "JE"), [("6200", 777, 0), ("1200", 0, 777)], posting_date=date(1990, 1, 1))
            return bool(e and e.status == "POSTED"), "posted on 1 Jan 1990"
        self.probe(w, "ledger", "ACCOUNTANT", "post a journal dated 1990 (before the company existed)",
                   "rewrites history no one is reviewing", "HIGH", far_past)

        def header():
            e = je(self.ref(w, "JE"), [("6000", 400, 0), ("1200", 0, 400)])
            return bool(e and e.status == "POSTED"), "posted to 6000 Operating Expenses, a heading with sub-accounts"
        self.probe(w, "ledger", "ACCOUNTANT", "post to a heading account that has sub-accounts",
                   "amounts land on a summary line and vanish from the detailed accounts", "MEDIUM", header)

        def dup():
            r = self.ref(w, "JE")
            je(r, [("6200", 10, 0), ("1200", 0, 10)]); je(r, [("6200", 10, 0), ("1200", 0, 10)])
            n = JournalEntry.objects.filter(company=w.company, reference=r).count()
            return n > 1, f"{n} journals share {r}"
        self.probe(w, "ledger", "ACCOUNTANT", "two journals with the same reference",
                   "auditors can't tell entries apart; easy to post twice", "MEDIUM", dup)

        def nan_opening():
            acct.post(reverse("accounting:opening_balances"), {"posting_date": self.today.isoformat(),
                      f"dr_{w.acc['1100'].pk}": "NaN", f"cr_{w.acc['3100'].pk}": "1"})
            return JournalEntry.objects.filter(company=w.company, entry_type="OPENING").exists(), "NaN opening balance"
        self.probe(w, "ledger", "ACCOUNTANT", "opening balance typed as 'NaN'", "garbage input", "MEDIUM", nan_opening)

    def m_stock(self, w):
        """Stock — the warehouse manager"""
        from apps.inventory.models import Product, StockMovement
        wm = self.a(w, "WAREHOUSE_MANAGER")
        adj = reverse("inventory:adjustment_create")

        def adjust(product, mtype, qty, wh=None):
            wm.post(adj, {"product": str(product.pk), "warehouse": str((wh or w.wh1).pk), "movement_type": mtype,
                          "quantity": str(qty), "unit_cost": str(product.cost_price),
                          "movement_date": self.today.isoformat(), "notes": "", "reference": self.ref(w, "ADJ")})

        def good():
            adjust(w.product, "ADJUSTMENT_PLUS", 100)
            return self.stock(w.product, w.wh1) == D("100"), f"on hand {self.stock(w.product, w.wh1)}"
        self.step(w, "stock", "WAREHOUSE_MANAGER", "receive 100 units by stock adjustment", good)

        def product_payload(sku, cost="100", price="150"):
            return {"sku": sku, "name": f"Item {sku}", "product_type": "GOODS", "unit_of_measure": str(w.uom.pk),
                    "cost_price": cost, "selling_price": price, "reorder_level": "0", "reorder_quantity": "0",
                    "valuation_method": "WAVG", "category": str(w.category.pk), "description": "",
                    "track_inventory": "on"}

        def good_product():
            sku = self.ref(w, "SKU")
            wm.post(reverse("inventory:product_create"), product_payload(sku))
            return Product.objects.filter(company=w.company, sku=sku).exists(), ""
        self.step(w, "stock", "WAREHOUSE_MANAGER", "add a new product", good_product)

        def neg_out():
            before = self.stock(w.product, w.wh1)
            adjust(w.product, "ADJUSTMENT_MINUS", -40)
            after = self.stock(w.product, w.wh1)
            return after != before, f"stock went {before} → {after} on a 'minus' adjustment of -40"
        self.probe(w, "stock", "WAREHOUSE_MANAGER", "negative quantity on a stock-out adjustment",
                   "a write-off that actually adds stock — hides shrinkage", "HIGH", neg_out)

        def neg_in():
            before = self.stock(w.product, w.wh1)
            adjust(w.product, "ADJUSTMENT_PLUS", -500)
            after = self.stock(w.product, w.wh1)
            return after != before, f"stock went {before} → {after}"
        self.probe(w, "stock", "WAREHOUSE_MANAGER", "negative quantity on a stock-in adjustment",
                   "pushes stock below zero without any check", "HIGH", neg_in)

        def over_out():
            n0 = StockMovement.objects.filter(product=w.product).count()
            adjust(w.product, "ADJUSTMENT_MINUS", 100000)
            n1 = StockMovement.objects.filter(product=w.product).count()
            return n1 > n0, "refused on screen, but the movement row was still saved"
        self.probe(w, "stock", "WAREHOUSE_MANAGER", "write off more stock than is on hand",
                   "phantom movements corrupt the stock history", "HIGH", over_out)

        def below_cost():
            sku = self.ref(w, "SKU")
            wm.post(reverse("inventory:product_create"), product_payload(sku, cost="500", price="-20"))
            return Product.objects.filter(company=w.company, sku=sku).exists(), "selling price -20"
        self.probe(w, "stock", "WAREHOUSE_MANAGER", "product with a negative selling price",
                   "the till pays customers to take goods", "HIGH", below_cost)

        def dup_sku():
            wm.post(reverse("inventory:product_create"), product_payload(w.product.sku))
            return Product.objects.filter(company=w.company, sku=w.product.sku).count() > 1, "duplicate SKU saved"
        self.probe(w, "stock", "WAREHOUSE_MANAGER", "a product with an SKU that already exists",
                   "two items share a barcode", "MEDIUM", dup_sku)

    def m_stock_moves(self, w):
        """Warehouse transfers and counts — the warehouse manager"""
        wm = self.a(w, "WAREHOUSE_MANAGER")
        self.stock_in(w, w.product, 50)
        url = reverse("inventory:stock_transfer")

        def transfer(qty, frm=None, to=None):
            wm.post(url, {"from_warehouse": frm or str(w.wh1.pk), "to_warehouse": to or str(w.wh2.pk),
                          "product": str(w.product.pk), "quantity": str(qty), "movement_date": self.today.isoformat(),
                          "reference": self.ref(w, "TRF")})

        def good():
            transfer(10)
            return self.stock(w.product, w.wh1) == D("40") and self.stock(w.product, w.wh2) == D("10"), \
                f"{self.stock(w.product, w.wh1)} / {self.stock(w.product, w.wh2)}"
        self.step(w, "stock", "WAREHOUSE_MANAGER", "transfer 10 units to the branch store", good)

        def too_many():
            transfer(9999)
            return self.stock(w.product, w.wh1) < 0, f"main store now {self.stock(w.product, w.wh1)}"
        self.probe(w, "stock", "WAREHOUSE_MANAGER", "transfer more than is on hand",
                   "stock goes negative", "HIGH", too_many)

        def same_wh():
            transfer(5, to=str(w.wh1.pk).upper())
            from apps.inventory.models import StockMovement
            n = StockMovement.objects.filter(product=w.product, movement_type="TRANSFER_IN", warehouse=w.wh1).count()
            return n > 0, "transfer into the same store accepted"
        self.probe(w, "stock", "WAREHOUSE_MANAGER", "transfer into the same warehouse (ID in capitals)",
                   "fake movements inflate activity", "MEDIUM", same_wh)

        def neg_count():
            from apps.inventory.models import StockLevel
            sl = StockLevel.objects.get(product=w.product, warehouse=w.wh1)
            wm.post(reverse("inventory:inventory_count", args=[w.wh1.pk]),
                    {"movement_date": self.today.isoformat(), f"count_{sl.pk}": "-15"})
            return self.stock(w.product, w.wh1) < 0, f"counted -15; on hand {self.stock(w.product, w.wh1)}"
        self.probe(w, "stock", "WAREHOUSE_MANAGER", "stock count entered as a negative number",
                   "a count can't be negative", "MEDIUM", neg_count)

    def m_pos(self, w):
        """Point of sale — the cashier"""
        from apps.pos.models import POSSession
        cashier = self.a(w, "POS_CASHIER")

        def open_session(cash="5000", who=None):
            (who or cashier).post(reverse("pos:session_open"), {"branch": str(w.branch.pk), "opening_cash": cash})
            return POSSession.objects.filter(company=w.company, cashier=(who or cashier).user).order_by("-opened_at").first()

        def good_open():
            s = open_session()
            w.pos_session = s
            return bool(s and s.status == "OPEN"), ""
        self.step(w, "pos", "POS_CASHIER", "open the till with 5,000 float", good_open)

        def good_sell():
            from apps.pos.models import POSSale
            before_stock, before_cash = self.stock(w.product, w.wh1), self.bal(w, "1100")
            cashier.post(reverse("pos:terminal"), {"product_0": str(w.product.pk), "qty_0": "2",
                                                   "payment_method": "CASH", "amount_tendered": "1000"})
            sale = POSSale.objects.filter(session=w.pos_session).first()
            sold = before_stock - self.stock(w.product, w.wh1)
            took = self.bal(w, "1100") - before_cash
            return bool(sale and sold == 2 and took == sale.total_amount and sale.change_given >= 0), \
                f"sale={bool(sale)}, stock -{sold}, cash +{took}"
        self.step(w, "pos", "POS_CASHIER", "ring up 2 items for cash: stock down, cash and sales posted", good_sell)

        def sell_more_than_stock():
            from apps.pos.models import POSSale
            n = POSSale.objects.filter(session=w.pos_session).count()
            cashier.post(reverse("pos:terminal"), {"product_0": str(w.product.pk), "qty_0": "999999",
                                                   "payment_method": "MPESA"})
            return POSSale.objects.filter(session=w.pos_session).count() > n, "sold more than the store holds"
        self.probe(w, "pos", "POS_CASHIER", "sell more than is in stock",
                   "stock goes negative; shrinkage is hidden", "HIGH", sell_more_than_stock)

        def short_cash():
            from apps.pos.models import POSSale
            n = POSSale.objects.filter(session=w.pos_session).count()
            cashier.post(reverse("pos:terminal"), {"product_0": str(w.product.pk), "qty_0": "1",
                                                   "payment_method": "CASH", "amount_tendered": "1"})
            return POSSale.objects.filter(session=w.pos_session).count() > n, "cash sale with 1.00 tendered"
        self.probe(w, "pos", "POS_CASHIER", "cash sale where less cash is tendered than the total",
                   "the drawer is short from the start", "MEDIUM", short_cash)

        def neg_float():
            s = open_session("-2000")
            return bool(s and s.opening_cash < 0), "opened with -2,000"
        self.probe(w, "pos", "POS_CASHIER", "open the till with a negative float",
                   "cash-up variance is hidden", "MEDIUM", neg_float)

        def second_session():
            open_session()
            n = POSSession.objects.filter(company=w.company, cashier=cashier.user, status="OPEN").count()
            return n > 1, f"{n} tills open for one cashier"
        self.probe(w, "pos", "POS_CASHIER", "open a second till while one is already open",
                   "sales can be split across tills to hide shortages", "MEDIUM", second_session)

        def close_twice():
            s = w.pos_session
            url = reverse("pos:session_close", args=[s.pk])
            cashier.post(url, {"closing_cash": "5000"})
            s.refresh_from_db(); first = s.closing_cash
            cashier.post(url, {"closing_cash": "9999"})
            s.refresh_from_db()
            return s.closing_cash != first, f"closing cash changed {first} → {s.closing_cash} after close"
        self.probe(w, "pos", "POS_CASHIER", "re-close an already closed till with a different cash count",
                   "the cash count can be rewritten after the fact", "HIGH", close_twice)

        def close_someone_elses():
            other = self.a(w, "SALES_REP")
            s = open_session(who=cashier)
            other.post(reverse("pos:session_close", args=[s.pk]), {"closing_cash": "1"})
            s.refresh_from_db()
            return s.status == "CLOSED", "a sales rep closed the cashier's till"
        self.probe(w, "pos", "SALES_REP", "close another cashier's till",
                   "someone else can sign off your cash", "HIGH", close_someone_elses)

    def m_purchasing(self, w):
        """Purchasing — procurement officer, accountant, finance manager"""
        from apps.procurement.models import Bill, PurchaseOrder, PurchaseRequisition
        proc, acct, fm = self.a(w, "PROCUREMENT"), self.a(w, "ACCOUNTANT"), self.a(w, "FINANCE_MANAGER")

        def po_payload(ref, qty="10", cost="1000", **extra):
            d = {"reference": ref, "supplier": str(w.supplier.pk), "order_date": self.today.isoformat(),
                 "expected_delivery_date": (self.today + timedelta(days=7)).isoformat(),
                 "warehouse": str(w.wh1.pk), "notes": "",
                 "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "1",
                 "lines-MAX_NUM_FORMS": "1000", "lines-0-product": str(w.product.pk), "lines-0-description": "",
                 "lines-0-quantity": qty, "lines-0-unit_cost": cost, "lines-0-tax_rate": str(w.vat.pk)}
            d.update(extra)
            return d

        def good_po():
            r = self.ref(w, "PO"); proc.post(reverse("procurement:po_create"), po_payload(r))
            w.po = PurchaseOrder.objects.filter(company=w.company, reference=r).first()
            return w.po is not None, "purchase order not saved"
        self.step(w, "purchasing", "PROCUREMENT", "raise a purchase order", good_po)

        def good_po_lines():
            po = w.po
            ok = po and po.lines.count() == 1 and po.total_amount == D("11600")
            return bool(ok), f"no way to add the items being ordered (lines={po and po.lines.count()}, " \
                             f"total={po and po.total_amount})"
        self.step(w, "purchasing", "PROCUREMENT", "PO for 10 × 1,000 + VAT totals 11,600 from its lines", good_po_lines)

        def good_receive():
            po = w.po
            before, grni_before = self.stock(w.product, w.wh1), self.bal(w, "2150")
            line = po.lines.first()
            proc.post(reverse("procurement:grn", args=[po.pk]), {f"qty_{line.pk}": "10",
                                                                "received_date": self.today.isoformat()})
            po.refresh_from_db()
            gained = self.stock(w.product, w.wh1) - before
            grni = self.bal(w, "2150") - grni_before
            return gained == 10 and po.status == "FULLY_RECEIVED" and grni == D("10000"), \
                f"stock +{gained}, PO {po.status}, goods-received-not-invoiced {grni}"
        self.step(w, "purchasing", "PROCUREMENT", "receive the 10 units against the PO", good_receive)

        def good_po_bill():
            r = self.ref(w, "BILL")
            grni_before, ap_before = self.bal(w, "2150"), self.bal(w, "2100")
            acct.post(reverse("procurement:bill_create"),
                      {**self.bill_payload(w, r, f"S-{secrets.token_hex(2)}", "10000", "1600", "11600"),
                       "purchase_order": str(w.po.pk)})
            b = Bill.objects.filter(company=w.company, reference=r).first()
            cleared = grni_before - self.bal(w, "2150")
            owed = self.bal(w, "2100") - ap_before
            return bool(b and b.journal_entry_id and cleared == D("10000") and owed == D("11600")), \
                f"bill={bool(b)}, GRNI cleared {cleared}, payables +{owed}"
        self.step(w, "purchasing", "ACCOUNTANT", "supplier's bill for the PO clears goods-received and raises payables",
                  good_po_bill)

        def good_req():
            r = self.ref(w, "REQ")
            proc.post(reverse("procurement:requisition_create"), {"reference": r, "department": str(w.dept.pk),
                      "required_date": (self.today + timedelta(days=5)).isoformat(), "notes": ""})
            req = PurchaseRequisition.objects.filter(company=w.company, reference=r).first()
            if not req:
                return False, "requisition not saved"
            proc.post(reverse("procurement:requisition_submit", args=[req.pk]))
            fm.post(reverse("procurement:requisition_approve", args=[req.pk]))
            req.refresh_from_db()
            return req.status == "APPROVED", f"a new requisition stays {req.get_status_display()}; it can't be sent for approval"
        self.step(w, "purchasing", "PROCUREMENT", "raise a requisition, send it, and get it approved", good_req)

        def po_bad_totals():
            r = self.ref(w, "PO")
            proc.post(reverse("procurement:po_create"), po_payload(r, subtotal="10000", tax_amount="1600",
                                                                 total_amount="50000"))
            po = PurchaseOrder.objects.filter(company=w.company, reference=r).first()
            return bool(po and po.total_amount != D("11600")), f"10,000 + 1,600 saved as {po and po.total_amount}"
        self.probe(w, "purchasing", "PROCUREMENT", "purchase order whose total ≠ subtotal + VAT",
                   "commits the company to the wrong amount", "HIGH", po_bad_totals)

        def po_negative():
            r = self.ref(w, "PO"); proc.post(reverse("procurement:po_create"), po_payload(r, qty="-10"))
            return PurchaseOrder.objects.filter(company=w.company, reference=r).exists(), "PO for -10 units saved"
        self.probe(w, "purchasing", "PROCUREMENT", "purchase order for a negative quantity",
                   "nonsense commitment; negative receipts", "MEDIUM", po_negative)

        def over_receive():
            r = self.ref(w, "PO"); proc.post(reverse("procurement:po_create"), po_payload(r, qty="5"))
            po = PurchaseOrder.objects.get(company=w.company, reference=r)
            before = self.stock(w.product, w.wh1)
            proc.post(reverse("procurement:grn", args=[po.pk]), {f"qty_{po.lines.first().pk}": "50"})
            return self.stock(w.product, w.wh1) - before > 5, "received 50 against an order for 5"
        self.probe(w, "purchasing", "PROCUREMENT", "receive 10× more than was ordered",
                   "stock and supplier liability inflated beyond what was agreed", "HIGH", over_receive)

        def double_bill():
            sref = f"INV-{secrets.token_hex(2)}"
            acct.post(reverse("procurement:bill_create"), self.bill_payload(w, self.ref(w, "BILL"), sref))
            acct.post(reverse("procurement:bill_create"), self.bill_payload(w, self.ref(w, "BILL"), sref))
            n = Bill.objects.filter(company=w.company, supplier=w.supplier, supplier_reference=sref).count()
            return n > 1, f"supplier invoice {sref} entered {n} times"
        self.probe(w, "purchasing", "ACCOUNTANT", "enter the same supplier invoice twice",
                   "the supplier gets paid twice — the most common AP fraud", "CRITICAL", double_bill)

        def bill_due_before():
            r = self.ref(w, "BILL")
            acct.post(reverse("procurement:bill_create"),
                      self.bill_payload(w, r, "X1", due_date=self.today - timedelta(days=5)))
            return Bill.objects.filter(company=w.company, reference=r).exists(), "due date before bill date"
        self.probe(w, "purchasing", "ACCOUNTANT", "bill due date before the bill date",
                   "ageing and payment runs are wrong", "MEDIUM", bill_due_before)

        def neg_bill():
            r = self.ref(w, "BILL")
            acct.post(reverse("procurement:bill_create"), self.bill_payload(w, r, "X2", "-1000", "-160", "-1160"))
            return Bill.objects.filter(company=w.company, reference=r).exists(), "-1,160 bill saved"
        self.probe(w, "purchasing", "ACCOUNTANT", "a bill with negative amounts",
                   "a credit note disguised as a bill", "HIGH", neg_bill)

        def pay_nan():
            r = self.ref(w, "BILL")
            acct.post(reverse("procurement:bill_create"), self.bill_payload(w, r, "X3"))
            b = Bill.objects.get(company=w.company, reference=r)
            acct.post(reverse("procurement:bill_pay", args=[b.pk]), {"amount": "NaN", "pay_from": str(w.acc["1200"].pk),
                                                                       "payment_date": self.today.isoformat()})
            b.refresh_from_db()
            return b.paid_amount != 0, "NaN payment"
        self.probe(w, "purchasing", "ACCOUNTANT", "supplier payment typed as 'NaN'", "garbage input", "MEDIUM", pay_nan)

        def self_approve():
            req = PurchaseRequisition.objects.create(company=w.company, reference=self.ref(w, "REQ"),
                                                     requested_by=fm.user, required_date=self.today + timedelta(days=3),
                                                     status="PENDING_APPROVAL")
            fm.post(reverse("procurement:requisition_approve", args=[req.pk]))
            req.refresh_from_db()
            return req.status == "APPROVED", "finance manager approved their own requisition"
        self.probe(w, "purchasing", "FINANCE_MANAGER", "approve your own purchase requisition",
                   "no second pair of eyes on spending (segregation of duties)", "HIGH", self_approve)

        def reject_approved():
            req = PurchaseRequisition.objects.create(company=w.company, reference=self.ref(w, "REQ"),
                                                     requested_by=proc.user, required_date=self.today + timedelta(days=3),
                                                     status="APPROVED")
            fm.post(reverse("procurement:requisition_reject", args=[req.pk]))
            req.refresh_from_db()
            return req.status == "REJECTED", "an approved requisition was flipped to rejected"
        self.probe(w, "purchasing", "FINANCE_MANAGER", "reject a requisition that was already approved",
                   "approval history can be rewritten after purchase", "MEDIUM", reject_approved)

    def m_oversight(self, w):
        """Read-only roles — auditor and viewer"""
        from apps.accounting.models import JournalEntry
        from apps.sales.models import Invoice

        def auditor_journal():
            r = self.ref(w, "JE")
            self.a(w, "AUDITOR").post(reverse("accounting:journal_entry_create"),
                                     self.je_payload(w, r, [("6200", 5, 0), ("1200", 0, 5)]))
            return JournalEntry.objects.filter(company=w.company, reference=r).exists(), "auditor posted a journal"
        self.probe(w, "oversight", "AUDITOR", "auditor enters a journal", "auditors must not change the books",
                   "CRITICAL", auditor_journal)

        def viewer_invoice():
            r = self.ref(w, "INV")
            self.a(w, "VIEWER").post(reverse("sales:invoice_create"), self.invoice_payload(w, r))
            return Invoice.objects.filter(company=w.company, reference=r).exists(), "viewer raised an invoice"
        self.probe(w, "oversight", "VIEWER", "view-only user raises an invoice", "read-only must mean read-only",
                   "HIGH", viewer_invoice)

        def employee_payroll():
            r = self.a(w, "EMPLOYEE").get(reverse("payroll:run_list"))
            return r.status_code == 200, "employee opened everyone's payroll"
        self.probe(w, "oversight", "EMPLOYEE", "ordinary employee opens payroll", "salaries are confidential",
                   "HIGH", employee_payroll)

    # ================================================================ manufacturing

    def m_manufacturing(self, w):
        """Manufacturing — warehouse manager and production planner"""
        from apps.manufacturing.models import BillOfMaterials, WorkOrder
        wm, pm = self.a(w, "WAREHOUSE_MANAGER"), self.a(w, "PROJECT_MANAGER")
        self.stock_in(w, w.component, 20)

        def bom_payload(name, product, lines, prod_qty="1", status="ACTIVE"):
            data = {"name": name, "product": str(product.pk), "version": "1", "status": status,
                    "production_quantity": prod_qty, "notes": "", "lines-TOTAL_FORMS": str(len(lines)),
                    "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000"}
            for i, (comp, qty) in enumerate(lines):
                data.update({f"lines-{i}-id": "", f"lines-{i}-component": str(comp.pk), f"lines-{i}-quantity": str(qty),
                             f"lines-{i}-unit_of_measure": "", f"lines-{i}-scrap_percent": "0"})
            return data

        def make_bom(lines, prod_qty="1", product=None):
            name = self.ref(w, "BOM")
            wm.post(reverse("manufacturing:bom_create"), bom_payload(name, product or w.finished, lines, prod_qty))
            return BillOfMaterials.objects.filter(company=w.company, name=name).first()

        def wo_payload(ref, bom, qty):
            return {"reference": ref, "bom": str(bom.pk), "planned_quantity": str(qty),
                    "planned_start": self.today.isoformat(), "planned_end": (self.today + timedelta(days=3)).isoformat(),
                    "warehouse": str(w.wh1.pk), "notes": ""}

        def good():
            bom = make_bom([(w.component, 2)])
            if not bom:
                return False, "bill of materials not saved"
            w.bom = bom
            r = self.ref(w, "WO")
            pm.post(reverse("manufacturing:work_order_create"), wo_payload(r, bom, 5))
            wo = WorkOrder.objects.filter(company=w.company, reference=r).first()
            if not wo:
                return False, "work order not saved"
            for action in ("release", "start"):
                pm.post(reverse(f"manufacturing:work_order_{action}", args=[wo.pk]))
            pm.post(reverse("manufacturing:work_order_complete", args=[wo.pk]),
                    {"actual_quantity": "5", "actual_end": self.today.isoformat()})
            wo.refresh_from_db()
            ok = wo.status == "COMPLETED" and self.stock(w.finished, w.wh1) == D("5") \
                and self.stock(w.component, w.wh1) == D("10")
            return ok, f"status {wo.status}, gates {self.stock(w.finished, w.wh1)}, steel {self.stock(w.component, w.wh1)}"
        self.step(w, "manufacturing", "PROJECT_MANAGER", "make 5 gates from 10 steel sheets (BOM → work order → complete)", good)

        def empty_bom():
            bom = make_bom([])
            return bom is not None, "bill of materials with no components saved"
        self.probe(w, "manufacturing", "WAREHOUSE_MANAGER", "bill of materials with no components",
                   "products appear from nothing; costing is zero", "HIGH", empty_bom)

        def self_component():
            bom = make_bom([(w.finished, 1)])
            return bom is not None, "a gate is made from a gate"
        self.probe(w, "manufacturing", "WAREHOUSE_MANAGER", "product listed as its own component",
                   "circular recipe; stock and cost loop", "MEDIUM", self_component)

        def neg_component():
            bom = make_bom([(w.component, -3)])
            return bom is not None, "component quantity -3"
        self.probe(w, "manufacturing", "WAREHOUSE_MANAGER", "negative component quantity in a recipe",
                   "making goods would add raw materials", "HIGH", neg_component)

        def neg_wo():
            r = self.ref(w, "WO")
            pm.post(reverse("manufacturing:work_order_create"), wo_payload(r, w.bom, -10))
            return WorkOrder.objects.filter(company=w.company, reference=r).exists(), "planned quantity -10"
        self.probe(w, "manufacturing", "PROJECT_MANAGER", "work order for -10 units",
                   "nonsense plans in the production schedule", "MEDIUM", neg_wo)

        def neg_output():
            r = self.ref(w, "WO")
            pm.post(reverse("manufacturing:work_order_create"), wo_payload(r, w.bom, 1))
            wo = WorkOrder.objects.get(company=w.company, reference=r)
            before = self.stock(w.finished, w.wh1)
            pm.post(reverse("manufacturing:work_order_complete", args=[wo.pk]),
                    {"actual_quantity": "-4", "actual_end": self.today.isoformat()})
            return self.stock(w.finished, w.wh1) < before, f"finished goods {before} → {self.stock(w.finished, w.wh1)}"
        self.probe(w, "manufacturing", "PROJECT_MANAGER", "complete a work order with -4 produced",
                   "production removes finished goods; stock and cost go wrong", "HIGH", neg_output)

        def no_materials():
            r = self.ref(w, "WO")
            pm.post(reverse("manufacturing:work_order_create"), wo_payload(r, w.bom, 500))
            wo = WorkOrder.objects.get(company=w.company, reference=r)
            pm.post(reverse("manufacturing:work_order_complete", args=[wo.pk]),
                    {"actual_quantity": "500", "actual_end": self.today.isoformat()})
            wo.refresh_from_db()
            return wo.status == "COMPLETED", "completed 500 gates with steel for 5"
        self.probe(w, "manufacturing", "PROJECT_MANAGER", "complete a work order without enough materials",
                   "output with no inputs; stock goes wrong", "HIGH", no_materials)

        def skip_release():
            r = self.ref(w, "WO")
            pm.post(reverse("manufacturing:work_order_create"), wo_payload(r, w.bom, 1))
            wo = WorkOrder.objects.get(company=w.company, reference=r)
            pm.post(reverse("manufacturing:work_order_complete", args=[wo.pk]),
                    {"actual_quantity": "1", "actual_end": self.today.isoformat()})
            wo.refresh_from_db()
            return wo.status == "COMPLETED", "a draft work order was completed without being released"
        self.probe(w, "manufacturing", "PROJECT_MANAGER", "complete a work order that was never released",
                   "production bypasses planning sign-off", "MEDIUM", skip_release)

    # ================================================================ people

    def m_hr(self, w):
        """HR — the HR manager"""
        from apps.hr.models import Employee
        hr = self.a(w, "HR_MANAGER")

        def emp_payload(num, dob="1995-03-01", hired=None, kra=""):
            return {"employee_number": num, "first_name": "Wambui", "last_name": "Test", "other_names": "",
                    "gender": "F", "date_of_birth": dob, "date_hired": (hired or self.today).isoformat(),
                    "status": "ACTIVE", "department": str(w.dept.pk), "branch": "", "job_title": "Fitter",
                    "reports_to": "", "national_id": "", "kra_pin": kra, "nssf_number": "", "nhif_number": "",
                    "phone": "", "email": "", "bank_name": "", "bank_account_number": "", "notes": ""}

        def made(num):
            return Employee.objects.filter(company=w.company, employee_number=num).exists()

        def good():
            n = self.ref(w, "EMP"); hr.post(reverse("hr:employee_create"), emp_payload(n)); return made(n), ""
        self.step(w, "hr", "HR_MANAGER", "hire a new employee", good)

        def good_salary():
            body = hr.get(reverse("hr:employee_create")).content.decode().lower()
            return "salary" in body, "there's no screen to record an employee's salary or contract"
        self.step(w, "hr", "HR_MANAGER", "record the new employee's salary", good_salary)

        def child():
            n = self.ref(w, "EMP")
            hr.post(reverse("hr:employee_create"), emp_payload(n, dob=(self.today - timedelta(days=365 * 10)).isoformat()))
            return made(n), "hired a 10-year-old"
        self.probe(w, "hr", "HR_MANAGER", "hire someone aged 10", "child labour; illegal under Kenyan law",
                   "HIGH", child)

        def future():
            n = self.ref(w, "EMP")
            hr.post(reverse("hr:employee_create"), emp_payload(n, hired=self.today + timedelta(days=400)))
            return made(n), "hire date next year"
        self.probe(w, "hr", "HR_MANAGER", "hire date more than a year ahead",
                   "ghost employees on the books", "MEDIUM", future)

        def before_birth():
            n = self.ref(w, "EMP")
            hr.post(reverse("hr:employee_create"), emp_payload(n, dob="2000-01-01", hired=date(1999, 1, 1)))
            return made(n), "hired before being born"
        self.probe(w, "hr", "HR_MANAGER", "hire date before date of birth", "impossible record", "MEDIUM", before_birth)

        def dup_number():
            num = w.employees["EMPLOYEE"].employee_number
            hr.post(reverse("hr:employee_create"), emp_payload(num))
            return Employee.objects.filter(company=w.company, employee_number=num).count() > 1, "duplicate staff number"
        self.probe(w, "hr", "HR_MANAGER", "employee number already in use", "two people on one payroll number",
                   "MEDIUM", dup_number)

        def dup_kra():
            pin = w.employees["EMPLOYEE"].kra_pin
            n = self.ref(w, "EMP"); hr.post(reverse("hr:employee_create"), emp_payload(n, kra=pin))
            return made(n), f"second employee with KRA PIN {pin}"
        self.probe(w, "hr", "HR_MANAGER", "KRA PIN already used by another employee",
                   "classic ghost-worker pattern; wrong PAYE returns", "HIGH", dup_kra)

    def m_leave(self, w):
        """Leave — employee and HR manager"""
        from apps.hr.models import LeaveRequest
        emp, hr = self.a(w, "EMPLOYEE"), self.a(w, "HR_MANAGER")
        me = w.employees["EMPLOYEE"]

        def ask(agent, employee, start, end):
            agent.post(reverse("hr:leave_create"), {"employee": str(employee.pk), "leave_type": str(w.leave_type.pk),
                                                    "start_date": start.isoformat(), "end_date": end.isoformat(),
                                                    "notes": "uat"})
            return LeaveRequest.objects.filter(employee=employee, start_date=start, end_date=end).order_by("-created_at").first()

        base = self.today + timedelta(days=30)

        def good():
            lr = ask(emp, me, base, base + timedelta(days=2))
            if not lr:
                return False, "leave request not saved"
            hr.post(reverse("hr:leave_approve", args=[lr.pk]), {"action": "approve"})
            lr.refresh_from_db()
            return lr.status == "APPROVED", f"status {lr.status}"
        self.step(w, "leave", "EMPLOYEE", "request 3 days' leave and get it approved", good)

        def backwards():
            lr = ask(emp, me, base + timedelta(days=60), base + timedelta(days=55))
            return lr is not None, "leave ends 5 days before it starts"
        self.probe(w, "leave", "EMPLOYEE", "leave that ends before it starts",
                   "negative days are added back to the leave balance", "MEDIUM", backwards)

        def overlap():
            lr = ask(emp, me, base + timedelta(days=1), base + timedelta(days=3))
            return lr is not None, "overlaps leave already approved"
        self.probe(w, "leave", "EMPLOYEE", "leave overlapping leave already approved",
                   "the same days are taken twice", "MEDIUM", overlap)

        def too_long():
            lr = ask(emp, me, base + timedelta(days=90), base + timedelta(days=150))
            return lr is not None, "61 days requested against a 21-day entitlement"
        self.probe(w, "leave", "EMPLOYEE", "leave far beyond the annual entitlement",
                   "entitlement isn't enforced", "MEDIUM", too_long)

        def for_someone_else():
            other = w.employees["SALES_REP"]
            lr = ask(emp, other, base + timedelta(days=200), base + timedelta(days=201))
            return lr is not None, "an employee filed leave for a colleague"
        self.probe(w, "leave", "EMPLOYEE", "file leave on behalf of a colleague",
                   "anyone can book anyone off work", "MEDIUM", for_someone_else)

        def hr_self():
            hr_emp = w.employees["HR_MANAGER"]
            lr = ask(hr, hr_emp, base + timedelta(days=300), base + timedelta(days=302))
            hr.post(reverse("hr:leave_approve", args=[lr.pk]), {"action": "approve"})
            lr.refresh_from_db()
            return lr.status == "APPROVED", "HR manager approved their own leave"
        self.probe(w, "leave", "HR_MANAGER", "approve your own leave",
                   "no one checks the approver's own requests", "HIGH", hr_self)

    def m_payroll(self, w):
        """Payroll — the payroll officer"""
        from apps.payroll.models import PayrollRun, Payslip
        po = self.a(w, "PAYROLL_OFFICER")
        url = reverse("payroll:run_create")

        def run(ref, start=None, end=None):
            po.post(url, {"reference": ref, "period_start": (start or self.last_month_start).isoformat(),
                          "period_end": (end or self.last_month_end).isoformat(),
                          "payment_date": self.today.isoformat(), "notes": ""})
            return PayrollRun.objects.filter(company=w.company, reference=ref).first()

        def good():
            r = run(self.ref(w, "PAY"))
            if not r:
                return False, "payroll run not saved"
            w.payroll = r
            slips = Payslip.objects.filter(payroll_run=r)
            return slips.count() >= len(ROLES) and not slips.filter(net_salary__lte=0).exists(), \
                f"{slips.count()} payslips"
        self.step(w, "payroll", "PAYROLL_OFFICER", "run last month's payroll for all staff", good)

        def good_paye():
            slip = Payslip.objects.filter(payroll_run=getattr(w, "payroll", None)).first()
            # On KES 80,000 a month, PAYE after the 2,400 personal relief is about 15,000.
            return bool(slip and slip.paye_tax > D("10000")), \
                f"PAYE on an 80,000 salary is {slip.paye_tax if slip else '—'}; it should be about 15,000"
        self.step(w, "payroll", "PAYROLL_OFFICER", "PAYE is calculated on an 80,000 salary", good_paye)

        def good_posted():
            r = getattr(w, "payroll", None)
            return bool(r and r.journal_entry_id), "payroll never reaches the ledger: salaries and PAYE payable missing"
        self.step(w, "payroll", "PAYROLL_OFFICER", "payroll is posted to the ledger", good_posted)

        def twice():
            run(self.ref(w, "PAY"))
            n = PayrollRun.objects.filter(company=w.company, period_start=self.last_month_start).count()
            return n > 1, f"{n} payroll runs for {self.last_month_start:%B}"
        self.probe(w, "payroll", "PAYROLL_OFFICER", "run the same month's payroll twice",
                   "everyone is paid twice", "CRITICAL", twice)

        def backwards():
            r = run(self.ref(w, "PAY"), start=self.last_month_end, end=self.last_month_start)
            return r is not None, "period ends before it starts"
        self.probe(w, "payroll", "PAYROLL_OFFICER", "payroll period that ends before it starts",
                   "nonsense period", "MEDIUM", backwards)

        def no_salary():
            from apps.hr.models import Employee
            Employee.objects.create(company=w.company, employee_number=self.ref(w, "NOSAL"), first_name="No",
                                    last_name="Contract", date_hired=date(2023, 1, 1), status="ACTIVE")
            r = run(self.ref(w, "PAY"), start=self.last_month_start - timedelta(days=31),
                    end=self.last_month_start - timedelta(days=1))
            neg = Payslip.objects.filter(payroll_run=r, net_salary__lt=0).count() if r else 0
            return neg > 0, f"{neg} payslip(s) with negative net pay"
        self.probe(w, "payroll", "PAYROLL_OFFICER", "pay an employee who has no salary set",
                   "negative pay slips; deductions charged with nothing to deduct from", "HIGH", no_salary)

    # ================================================================ services

    def m_projects(self, w):
        """Projects — the project manager"""
        from apps.projects.models import Project, Task
        pm = self.a(w, "PROJECT_MANAGER")

        def payload(ref, start, end, budget="500000"):
            return {"reference": ref, "name": f"Audit engagement {ref}", "description": "", "status": "ACTIVE",
                    "start_date": start.isoformat(), "end_date": end.isoformat() if end else "", "budget": budget,
                    "customer": str(w.customer.pk), "manager": "", "branch": "", "notes": ""}

        def good():
            r = self.ref(w, "PRJ")
            pm.post(reverse("projects:project_create"), payload(r, self.today, self.today + timedelta(days=90)))
            p = Project.objects.filter(company=w.company, reference=r).first()
            if not p:
                return False, "project not saved"
            w.project = p
            pm.post(reverse("projects:task_create", args=[p.pk]), {
                "title": "Fieldwork", "description": "", "status": "TODO", "priority": "MEDIUM",
                "assigned_to": str(self.a(w, "EMPLOYEE").user.pk), "due_date": (self.today + timedelta(days=30)).isoformat(),
                "estimated_hours": "40", "notes": ""})
            w.task = Task.objects.filter(project=p).first()
            return w.task is not None, ""
        self.step(w, "projects", "PROJECT_MANAGER", "open a project and assign a task", good)

        def backwards():
            r = self.ref(w, "PRJ")
            pm.post(reverse("projects:project_create"), payload(r, self.today, self.today - timedelta(days=30)))
            return Project.objects.filter(company=w.company, reference=r).exists(), "ends a month before it starts"
        self.probe(w, "projects", "PROJECT_MANAGER", "project that ends before it starts", "nonsense plan", "MEDIUM", backwards)

        def neg_budget():
            r = self.ref(w, "PRJ")
            pm.post(reverse("projects:project_create"), payload(r, self.today, None, budget="-100000"))
            return Project.objects.filter(company=w.company, reference=r).exists(), "budget -100,000"
        self.probe(w, "projects", "PROJECT_MANAGER", "project with a negative budget",
                   "budget reports become meaningless", "MEDIUM", neg_budget)

    def m_timesheets(self, w):
        """Timesheets — employee and project manager"""
        from apps.timesheets.models import TimesheetEntry
        emp, pm = self.a(w, "EMPLOYEE"), self.a(w, "PROJECT_MANAGER")

        def log(agent, hours, day=None):
            d = day or self.today
            agent.post(reverse("timesheets:entry_create"), {"project": str(w.project.pk), "task": str(w.task.pk),
                       "entry_date": d.isoformat(), "hours": str(hours), "description": f"uat {hours}h",
                       "billable": "on"})
            return TimesheetEntry.objects.filter(user=agent.user, entry_date=d, hours=D(str(hours))).order_by("-created_at").first()

        def good():
            e = log(emp, 8)
            if not e:
                return False, "entry not saved"
            emp.post(reverse("timesheets:entry_submit", args=[e.pk]))
            pm.post(reverse("timesheets:entry_approve", args=[e.pk]))
            e.refresh_from_db()
            return e.status == "APPROVED", f"status {e.status}"
        self.step(w, "timesheets", "EMPLOYEE", "log 8 hours, submit, project manager approves", good)

        def too_many():
            return log(emp, 30) is not None, "30 hours in one day"
        self.probe(w, "timesheets", "EMPLOYEE", "30 hours logged in a single day",
                   "clients are billed for impossible time", "HIGH", too_many)

        def negative():
            return log(emp, -6) is not None, "-6 hours"
        self.probe(w, "timesheets", "EMPLOYEE", "negative hours", "reduces billable time silently", "MEDIUM", negative)

        def future():
            return log(emp, 8, self.today + timedelta(days=20)) is not None, "hours logged 20 days ahead"
        self.probe(w, "timesheets", "EMPLOYEE", "timesheet for a day that hasn't happened",
                   "billing for work not done", "MEDIUM", future)

        def pm_self():
            e = log(pm, 5, self.today - timedelta(days=1))
            pm.post(reverse("timesheets:entry_submit", args=[e.pk]))
            pm.post(reverse("timesheets:entry_approve", args=[e.pk]))
            e.refresh_from_db()
            return e.status == "APPROVED", "project manager approved their own hours"
        self.probe(w, "timesheets", "PROJECT_MANAGER", "approve your own timesheet",
                   "no one checks the approver's own hours", "HIGH", pm_self)

        def submit_by_link():
            e = log(emp, 2, self.today - timedelta(days=2))
            pm.get(reverse("timesheets:entry_submit", args=[e.pk]))
            e.refresh_from_db()
            return e.status == "SUBMITTED", "a plain link (GET) submitted someone else's timesheet"
        self.probe(w, "timesheets", "PROJECT_MANAGER", "submit a colleague's timesheet by opening a link",
                   "state changes on a link click (forgeable); no ownership check", "MEDIUM", submit_by_link)

    def m_expenses(self, w):
        """Expense claims — employee and finance manager"""
        from apps.expenses.models import ExpenseCategory, ExpenseClaim, ExpenseClaimLine
        emp, fm, acct = self.a(w, "EMPLOYEE"), self.a(w, "FINANCE_MANAGER"), self.a(w, "ACCOUNTANT")
        acct.post(reverse("expenses:category_create"), {"name": "Travel", "gl_account": str(w.acc["6400"].pk)})
        cat = ExpenseCategory.objects.get(company=w.company, name="Travel")

        def claim(agent, employee):
            r = self.ref(w, "EXP")
            agent.post(reverse("expenses:claim_create"), {"reference": r, "employee": str(employee.pk),
                                                          "claim_date": self.today.isoformat()})
            return ExpenseClaim.objects.filter(company=w.company, reference=r).first()

        def line(agent, c, amount, day=None):
            agent.post(reverse("expenses:claim_add_line", args=[c.pk]), {
                "category": str(cat.pk), "description": "Taxi to client", "amount": str(amount),
                "expense_date": (day or self.today).isoformat()})

        def good():
            c = claim(emp, w.employees["EMPLOYEE"])
            line(emp, c, 2500)
            emp.post(reverse("expenses:claim_submit", args=[c.pk]))
            fm.post(reverse("expenses:claim_approve", args=[c.pk]))
            acct.post(reverse("expenses:claim_pay", args=[c.pk]), {"pay_from": str(w.acc["1200"].pk),
                                                                   "payment_date": self.today.isoformat()})
            c.refresh_from_db()
            w.paid_claim = c
            return c.status == "PAID" and self.bal(w, "6400") >= D("2500"), f"status {c.status}"
        self.step(w, "expenses", "EMPLOYEE", "claim 2,500 taxi, approved, reimbursed", good)

        def neg_line():
            c = claim(emp, w.employees["EMPLOYEE"]); line(emp, c, -900)
            return ExpenseClaimLine.objects.filter(claim=c, amount__lt=0).exists(), "-900 line saved"
        self.probe(w, "expenses", "EMPLOYEE", "expense line with a negative amount",
                   "nets other expenses down without explanation", "MEDIUM", neg_line)

        def future_line():
            c = claim(emp, w.employees["EMPLOYEE"]); line(emp, c, 500, self.today + timedelta(days=15))
            return ExpenseClaimLine.objects.filter(claim=c).exists(), "expense dated 15 days ahead"
        self.probe(w, "expenses", "EMPLOYEE", "expense dated in the future", "claiming for spending not yet made",
                   "MEDIUM", future_line)

        def empty_submit():
            c = claim(emp, w.employees["EMPLOYEE"])
            emp.post(reverse("expenses:claim_submit", args=[c.pk]))
            c.refresh_from_db()
            return c.status == "SUBMITTED", "a claim with no lines was submitted"
        self.probe(w, "expenses", "EMPLOYEE", "submit a claim with nothing on it", "noise in the approver's queue",
                   "MEDIUM", empty_submit)

        def for_colleague():
            c = claim(emp, w.employees["SALES_REP"])
            return c is not None, "an employee opened a claim in a colleague's name"
        self.probe(w, "expenses", "EMPLOYEE", "claim expenses in a colleague's name",
                   "money reimbursed to the wrong person", "HIGH", for_colleague)

        def line_after_paid():
            c = w.paid_claim
            before = c.total_amount
            line(emp, c, 7000)
            c.refresh_from_db()
            return c.total_amount != before, f"paid claim total changed {before} → {c.total_amount}"
        self.probe(w, "expenses", "EMPLOYEE", "add a line to a claim that's already been paid",
                   "claim no longer matches what was approved and paid", "HIGH", line_after_paid)

        def fm_self():
            c = claim(fm, w.employees["FINANCE_MANAGER"]); line(fm, c, 4000)
            fm.post(reverse("expenses:claim_submit", args=[c.pk]))
            fm.post(reverse("expenses:claim_approve", args=[c.pk]))
            c.refresh_from_db()
            return c.status == "APPROVED", "finance manager approved their own claim"
        self.probe(w, "expenses", "FINANCE_MANAGER", "approve your own expense claim",
                   "the approver can pay themselves", "CRITICAL", fm_self)

    def m_assets(self, w):
        """Fixed assets — accountant and finance manager"""
        from apps.fixed_assets.models import AssetDepreciation, AssetDisposal, FixedAsset
        acct, fm = self.a(w, "ACCOUNTANT"), self.a(w, "FINANCE_MANAGER")

        def register(code, cost="120000", residual="0", life="3", method="SLM"):
            acct.post(reverse("fixed_assets:asset_create"), {
                "code": code, "name": f"Laptop {code}", "category": "", "serial_number": "", "location": "",
                "purchase_date": self.last_month_start.isoformat(), "purchase_cost": cost, "residual_value": residual,
                "useful_life_years": life, "depreciation_method": method, "status": "ACTIVE", "supplier": "",
                "warranty_expiry": "", "notes": ""})
            return FixedAsset.objects.filter(company=w.company, code=code).first()

        def good():
            a = register(self.ref(w, "FA"))
            if not a:
                return False, "asset not saved"
            w.asset = a
            acct.post(reverse("fixed_assets:asset_depreciate", args=[a.pk]), {"period_date": self.last_month_end.isoformat()})
            a.refresh_from_db()
            return a.book_value == D("116666.67"), f"book value {a.book_value} after one month"
        self.step(w, "assets", "ACCOUNTANT", "register a 120,000 laptop and depreciate one month", good)

        def good_posted():
            dep = AssetDepreciation.objects.filter(asset=getattr(w, "asset", None)).first()
            return bool(dep and dep.journal_entry_id), "depreciation never reaches the ledger"
        self.step(w, "assets", "ACCOUNTANT", "depreciation is posted to the ledger", good_posted)

        def neg_cost():
            return register(self.ref(w, "FA"), cost="-50000") is not None, "asset costing -50,000"
        self.probe(w, "assets", "ACCOUNTANT", "asset with a negative cost", "inflates or hides the asset register",
                   "MEDIUM", neg_cost)

        def residual_over():
            return register(self.ref(w, "FA"), cost="10000", residual="50000") is not None, "residual 50,000 on a 10,000 asset"
        self.probe(w, "assets", "ACCOUNTANT", "residual value above cost", "impossible depreciation", "MEDIUM", residual_over)

        def zero_life_wdv():
            a = register(self.ref(w, "FA"), life="0", method="WDV")
            if not a:
                return False, ""
            acct.post(reverse("fixed_assets:asset_depreciate", args=[a.pk]), {"period_date": self.last_month_end.isoformat()})
            return True, "asset with 0 years' useful life saved"
        self.probe(w, "assets", "ACCOUNTANT", "useful life of 0 years, then depreciate",
                   "division by zero in depreciation", "MEDIUM", zero_life_wdv)

        def dup_code():
            register(w.asset.code)
            return FixedAsset.objects.filter(company=w.company, code=w.asset.code).count() > 1, "duplicate asset tag"
        self.probe(w, "assets", "ACCOUNTANT", "asset tag already in use", "two assets on one tag", "MEDIUM", dup_code)

        def twice_in_month():
            day = self.last_month_end - timedelta(days=3)
            acct.post(reverse("fixed_assets:asset_depreciate", args=[w.asset.pk]), {"period_date": day.isoformat()})
            n = AssetDepreciation.objects.filter(asset=w.asset, period_date__gte=self.last_month_start,
                                                 period_date__lte=self.last_month_end).count()
            return n > 1, f"{n} depreciation charges in {self.last_month_start:%B}"
        self.probe(w, "assets", "ACCOUNTANT", "depreciate the same asset twice in one month",
                   "overstated expense; understated assets", "HIGH", twice_in_month)

        def dispose_twice():
            a = register(self.ref(w, "FA"))
            url = reverse("fixed_assets:asset_dispose", args=[a.pk])
            data = {"disposal_date": self.today.isoformat(), "disposal_type": "SALE", "proceeds": "50000", "notes": ""}
            fm.post(url, data)
            first = AssetDisposal.objects.get(asset=a)
            fm.post(url, {**data, "proceeds": "90000"})
            first.refresh_from_db()
            return first.proceeds != D("50000"), "the recorded sale was overwritten by a second one"
        self.probe(w, "assets", "FINANCE_MANAGER", "dispose of an asset that's already been sold",
                   "double disposal income", "HIGH", dispose_twice)

    def m_banking(self, w):
        """Banking — the accountant"""
        from apps.banking.models import BankAccount, BankTransaction
        acct = self.a(w, "ACCOUNTANT")

        def good():
            acct.post(reverse("banking:account_create"), {"name": "KCB Operating", "bank_name": "KCB",
                      "account_number": "1100223344", "account_type": "CURRENT", "currency": "KES", "branch": "",
                      "gl_account": str(w.acc["1200"].pk), "opening_balance": "0"})
            ba = BankAccount.objects.filter(company=w.company, account_number="1100223344").first()
            w.bank_account = ba
            return ba is not None, ""
        self.step(w, "banking", "ACCOUNTANT", "add the KCB operating account", good)

        def wrong_gl():
            acct.post(reverse("banking:account_create"), {"name": "Fake bank", "bank_name": "X", "account_number": "999",
                      "account_type": "CURRENT", "currency": "KES", "branch": "",
                      "gl_account": str(w.acc["4100"].pk), "opening_balance": "0"})
            return BankAccount.objects.filter(company=w.company, account_number="999").exists(), \
                "bank account linked to Sales Revenue"
        self.probe(w, "banking", "ACCOUNTANT", "link a bank account to the Sales Revenue ledger account",
                   "bank movements would post to revenue", "HIGH", wrong_gl)

        def negative():
            acct.post(reverse("banking:transaction_create"), {"bank_account": str(w.bank_account.pk),
                      "transaction_date": self.today.isoformat(), "value_date": "", "transaction_type": "CREDIT",
                      "amount": "-25000", "description": "uat", "reference": "NEG"})
            return BankTransaction.objects.filter(bank_account=w.bank_account, amount__lt=0).exists(), "-25,000 credit"
        self.probe(w, "banking", "ACCOUNTANT", "bank statement line with a negative amount",
                   "credit/debit direction becomes ambiguous", "MEDIUM", negative)

    def m_sales_services(self, w):
        """Service invoicing — the sales rep"""
        from apps.sales.models import Invoice
        rep = self.a(w, "SALES_REP")

        def good():
            r = self.ref(w, "INV")
            rep.post(reverse("sales:invoice_create"), self.invoice_payload(w, r, qty="8", price="5000"))
            inv = Invoice.objects.filter(company=w.company, reference=r).first()
            return bool(inv and inv.journal_entry_id and inv.total_amount == D("46400")), ""
        self.step(w, "sales", "SALES_REP", "invoice 8 consulting days with VAT (46,400)", good)

        def manager_override():
            r = self.ref(w, "INV")
            self.a(w, "SALES_MANAGER").post(reverse("sales:invoice_create"),
                                            self.invoice_payload(w, r, qty="2", price="5000"))
            inv = Invoice.objects.filter(company=w.company, reference=r).first()
            return bool(inv and inv.journal_entry_id), "sales manager couldn't approve going over the limit"
        self.step(w, "sales", "SALES_MANAGER", "sales manager knowingly invoices past the credit limit", manager_override)

    # ================================================================ report

    def _report(self):
        h = self.h
        kinds = Counter(p.outcome.kind for p in h.probes)
        steps_ok = sum(1 for s in h.steps if s.ok)
        self.stdout.write(f"\n{BOLD}Guardrail & module UAT — auditor summary{RESET}")
        self.stdout.write(f"  Module work: {GREEN}{steps_ok}{RESET}/{len(h.steps)} tasks completed")
        self.stdout.write(f"  Bad transactions tried: {len(h.probes)} — {GREEN}guarded {kinds['guarded']}{RESET}, "
                          f"{YELLOW}silent {kinds['silent']}{RESET}, {RED}accepted {kinds['accepted']}{RESET}, "
                          f"{MAGENTA}crashed {kinds['crashed']}{RESET}")
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
        bad = sorted([p for p in h.probes if p.outcome.kind in ("accepted", "crashed")],
                     key=lambda p: (order[p.severity], p.module))
        if bad:
            self.stdout.write(f"\n  {BOLD}Missing guardrails{RESET}")
            for p in bad:
                c = RED if p.outcome.kind == "accepted" else MAGENTA
                self.stdout.write(f"  {c}{p.severity:8}{RESET} {p.module:13} {p.name} {DIM}[{p.role}; {p.outcome.kind}]{RESET}")
        failed = [s for s in h.steps if not s.ok]
        if failed:
            self.stdout.write(f"\n  {BOLD}Work people couldn't do{RESET}")
            for s in failed:
                self.stdout.write(f"  {RED}✗{RESET} {s.module:13} {s.name} {DIM}— {s.detail}{RESET}")

        md = ["# mofisi — role agents: module UAT and bad-transaction guardrails", "",
              f"Run `{h.tag}` on {date.today().isoformat()}: 3 companies (retail, manufacturing, services), "
              f"{len(ROLES)} role agents each, signing in through the login page.", "",
              f"- Module work completed: **{steps_ok}/{len(h.steps)}**",
              f"- Bad transactions tried: **{len(h.probes)}** — guarded {kinds['guarded']}, silent "
              f"{kinds['silent']}, **accepted {kinds['accepted']}**, **crashed {kinds['crashed']}**", ""]
        if bad:
            md += ["## Missing guardrails", "", "| Severity | Module | Bad transaction | Who tried | Result | Why it matters |",
                   "|---|---|---|---|---|---|"]
            md += [f"| {p.severity} | {p.module} | {p.name} | {p.role.replace('_', ' ').title()} | "
                   f"{p.outcome.kind}: {p.outcome.detail} | {p.risk} |" for p in bad]
        if failed:
            md += ["", "## Work people couldn't do", "", "| Module | Task | What happened |", "|---|---|---|"]
            md += [f"| {s.module} | {s.name} | {s.detail} |" for s in failed]
        good = [p for p in h.probes if p.outcome.kind in ("guarded", "silent")]
        if good:
            md += ["", "## Guardrails that held", "", "| Module | Bad transaction | Result |", "|---|---|---|"]
            md += [f"| {p.module} | {p.name} | {p.outcome.kind}: {p.outcome.detail} |" for p in good]
        return "\n".join(md) + "\n"
