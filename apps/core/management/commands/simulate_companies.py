"""Multi-company live simulation with an auditor agent.

Company agents
    Three Kenyan companies, each staffed with one employee per role. Every
    employee signs in through the real login form (CSRF enforced) and then
    works through the real pages and forms: the sales rep raises invoices,
    the accountant posts journals, finance approves requisitions, HR hires,
    and so on. Each employee also tries things their role should NOT allow,
    and browses every page they can reach looking for crashes.

    A fourth agent is a brand-new business owner who signs up, creates a
    company and tries to start working in it.

    One outside accountant works for two companies with different roles, to
    check that rights follow the company that is currently active.

Auditor agent
    Collects every problem the staff ran into, then runs its own checks:
    can one company reach another company's records or change them, do the
    books balance, did any journal touch another company's accounts. It
    ranks the findings and writes a report.

The business rules the agents check against live in CAPABILITIES below.
They are written as plain expectations ("a viewer must not raise
invoices") and are deliberately independent of how the app enforces
access, so the simulation can judge the implementation rather than echo it.

Usage:
    python manage.py simulate_companies
    python manage.py simulate_companies --report path/to/report.md
    python manage.py simulate_companies --keep      # don't roll back
"""
from __future__ import annotations

import re
import secrets
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.test import Client, override_settings
from django.urls import URLPattern, URLResolver, get_resolver, reverse

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, GREEN, YELLOW, CYAN = "\033[31m", "\033[32m", "\033[33m", "\033[36m"

ROLES = [
    "ADMIN", "FINANCE_MANAGER", "ACCOUNTANT", "SALES_MANAGER", "SALES_REP",
    "PROCUREMENT", "WAREHOUSE_MANAGER", "HR_MANAGER", "PAYROLL_OFFICER",
    "POS_CASHIER", "AUDITOR", "VIEWER", "EMPLOYEE",
]

FINANCE = {"ADMIN", "FINANCE_MANAGER", "ACCOUNTANT"}
SALES = {"ADMIN", "SALES_MANAGER", "SALES_REP"}

COMPANIES = [
    # (name, short code, industry)
    ("Jamii Retail Ltd", "JAMII", "Retail"),
    ("Safari Logistics Ltd", "SAFARI", "Logistics"),
    ("Mavuno Manufacturing Ltd", "MAVUNO", "Manufacturing"),
]

FIRST = ["Achieng", "Baraka", "Chebet", "Daudi", "Esther", "Faith", "Githinji",
         "Halima", "Imani", "Juma", "Kerubo", "Lemayian", "Muthoni", "Njeri",
         "Otieno", "Pendo", "Rehema", "Sifa", "Tumaini", "Wanjiru", "Yusuf",
         "Zawadi", "Kipchoge", "Akinyi", "Barasa", "Nafula", "Mwangi",
         "Wekesa", "Atieno", "Kiprono", "Nyambura", "Omondi", "Waithera",
         "Kamau", "Nekesa", "Cheruiyot", "Auma", "Mutua", "Wairimu"]
LAST = ["Odhiambo", "Kamau", "Wanjiku", "Mutiso", "Kiprotich", "Wafula",
        "Njoroge", "Ochieng", "Kariuki", "Chege"]

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# URL names that change state. Never GET these during a crawl.
ACTION_TOKENS = {"approve", "reject", "post", "reverse", "cancel", "send", "pay", "close",
                 "reopen", "submit", "toggle", "complete", "delete", "logout", "switch",
                 "depreciate", "dispose", "release", "start", "resolve", "comment", "open",
                 "credit", "up", "mark", "line", "count", "transfer"}


@dataclass
class Issue:
    category: str          # CRASH, TENANT_LEAK, PERMISSION_GAP, BLOCKED_LEGIT, ACCOUNTING, SECURITY
    severity: str          # CRITICAL, HIGH, MEDIUM
    title: str
    company: str = ""
    role: str = ""
    url: str = ""
    detail: str = ""

    @property
    def key(self):
        return (self.category, self.title, UUID_RE.sub("<id>", self.url))


@dataclass
class Staff:
    user: object
    role: str
    company: object
    password: str
    client: Client = None
    name: str = ""


@dataclass
class CompanyWorld:
    company: object
    code: str
    staff: dict = field(default_factory=dict)       # role -> Staff
    objects: dict = field(default_factory=dict)     # model label -> instance
    accounts: dict = field(default_factory=dict)    # code -> Account
    markers: list = field(default_factory=list)     # strings that only this company owns


class SimError(Exception):
    pass


class _Rollback(Exception):
    pass


# ---------------------------------------------------------------------------
# Business rules: which roles SHOULD be able to do what.
# ---------------------------------------------------------------------------

CAPABILITIES = [
    # (key, description, roles allowed, kind)   kind: "read" | "write"
    ("view_dashboard", "open the dashboard", set(ROLES), "read"),
    ("see_dashboard_financials", "see cash, payables and profit on the dashboard", FINANCE | {"AUDITOR"}, "read"),
    ("fetch_kpi_widget", "fetch the cash/KPI dashboard widget", FINANCE | {"AUDITOR"}, "read"),
    ("view_sales_invoices", "see sales invoices", FINANCE | SALES | {"AUDITOR", "VIEWER"}, "read"),
    ("create_customer", "add a customer", FINANCE | SALES, "write"),
    ("create_invoice", "raise a sales invoice", FINANCE | SALES, "write"),
    ("post_invoice_to_gl", "post an invoice to the general ledger", FINANCE, "write"),
    ("view_trial_balance", "see the trial balance", FINANCE | {"AUDITOR"}, "read"),
    ("create_journal_entry", "enter a manual journal", FINANCE, "write"),
    ("view_purchase_orders", "see purchase orders",
     FINANCE | {"PROCUREMENT", "WAREHOUSE_MANAGER", "AUDITOR", "VIEWER"}, "read"),
    ("create_requisition", "raise a purchase requisition",
     {"ADMIN", "FINANCE_MANAGER", "PROCUREMENT", "WAREHOUSE_MANAGER"}, "write"),
    ("approve_requisition", "approve a purchase requisition", {"ADMIN", "FINANCE_MANAGER"}, "write"),
    ("view_stock", "see stock levels",
     FINANCE | SALES | {"PROCUREMENT", "WAREHOUSE_MANAGER", "POS_CASHIER", "AUDITOR", "VIEWER"}, "read"),
    ("view_employees", "see the employee register",
     {"ADMIN", "HR_MANAGER", "PAYROLL_OFFICER", "AUDITOR"}, "read"),
    ("create_employee", "hire an employee", {"ADMIN", "HR_MANAGER"}, "write"),
    ("view_payroll", "see payroll runs",
     {"ADMIN", "FINANCE_MANAGER", "HR_MANAGER", "PAYROLL_OFFICER", "AUDITOR"}, "read"),
    ("use_pos", "open the POS terminal", SALES | {"POS_CASHIER"}, "read"),
    ("view_reports", "open reports",
     FINANCE | {"SALES_MANAGER", "AUDITOR", "VIEWER"}, "read"),
    ("view_expenses", "open their expense claims", set(ROLES), "read"),
    ("view_user_list", "see the user list", {"ADMIN"}, "read"),
    ("create_staff_user", "create a login for a colleague", {"ADMIN"}, "write"),
    ("assign_staff_role", "give a new colleague a role in the company", {"ADMIN"}, "write"),
    ("grant_system_admin", "create a platform-wide system admin", set(), "write"),
    ("deactivate_colleague", "deactivate a colleague's login", {"ADMIN"}, "write"),
    ("edit_branding", "change the company branding", {"ADMIN"}, "write"),
    ("open_control_panel", "open the control panel", {"ADMIN"}, "read"),
]


class Command(BaseCommand):
    help = "Simulate several companies using the ERP, then audit what went wrong."

    def add_arguments(self, parser):
        parser.add_argument("--keep", action="store_true", help="Persist simulated data.")
        parser.add_argument("--report", default="", help="Write a Markdown report to this path.")
        parser.add_argument("--no-crawl", action="store_true", help="Skip the page-crawl crash hunt.")

    # ------------------------------------------------------------------ run

    def handle(self, *args, **opts):
        self.issues: list[Issue] = []
        self.steps_ok = 0
        self.no_crawl = opts["no_crawl"]
        self.run_tag = secrets.token_hex(3).upper()

        self._banner("mofisi — live multi-company simulation")
        try:
            with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
                if opts["keep"]:
                    self._run()
                else:
                    try:
                        with transaction.atomic():
                            self._run()
                            raise _Rollback()
                    except _Rollback:
                        pass
        except SimError as e:
            self.stdout.write(f"\n{RED}{BOLD}Simulation could not run:{RESET} {e}")
            raise SystemExit(2)

        report = self._auditor_report()
        if opts["report"]:
            with open(opts["report"], "w") as fh:
                fh.write(report)
            self.stdout.write(f"\n{DIM}Report written to {opts['report']}{RESET}")
        if self.issues:
            raise SystemExit(1)

    def _run(self):
        self._section("Setting up companies and staff")
        self.worlds = [self._build_world(name, code, industry) for name, code, industry in COMPANIES]
        self.consultant = self._build_consultant()
        for w in self.worlds:
            self.stdout.write(f"  {GREEN}✓{RESET} {w.company.name}: {len(w.staff)} staff, "
                              f"{len(w.accounts)} accounts")
        self.stdout.write(f"  {GREEN}✓{RESET} Outside accountant {self.consultant['name']} "
                          f"(ACCOUNTANT at {self.worlds[0].code}, AUDITOR at {self.worlds[1].code})")

        self._section("Staff sign in through the login page")
        for w in self.worlds:
            for staff in w.staff.values():
                self._login(staff)
        self.stdout.write(f"  {GREEN}✓{RESET} {sum(len(w.staff) for w in self.worlds)} employees signed in")

        self._section("Company agents: everyone does their job, and tries what they shouldn't")
        for w in self.worlds:
            self._company_day(w)

        self._section("Outside accountant switches between clients")
        self._consultant_day()

        self._section("New business signs up and starts work")
        self._onboarding_agent()

        if not self.no_crawl:
            self._section("Crash hunt: staff open every page they can reach")
            self._crawl()

        self._section("Auditor agent: tenant isolation")
        self._audit_tenant_isolation()

        self._section("Auditor agent: security probes")
        self._audit_security()

        self._section("Auditor agent: books and ledgers")
        self._audit_books()

    # ---------------------------------------------------------- world setup

    def _build_world(self, name, code, industry):
        from apps.accounting.models import Account, JournalEntry, JournalEntryLine
        from apps.accounts.models import User
        from apps.crm.models import Customer, Supplier
        from apps.hr.models import Employee
        from apps.inventory.models import Product, UnitOfMeasure
        from apps.organizations.models import Company, CompanyMembership, Department, Warehouse
        from apps.procurement.models import Bill, PurchaseOrder, PurchaseRequisition
        from apps.sales.models import Invoice, InvoiceLine

        tag = f"{code}-{self.run_tag}"
        company = Company.objects.create(
            name=f"{name} {self.run_tag}", currency="KES", industry=industry,
            tax_id=f"P05{secrets.randbelow(10**7):07d}X", city="Nairobi", country="Kenya",
            email=f"info@{code.lower()}.co.ke",
        )
        w = CompanyWorld(company=company, code=code)

        coa = [("1000", "Cash", "CASH"), ("1010", "Bank KES", "BANK"),
               ("1100", "Accounts Receivable", "RECEIVABLE"), ("1200", "Inventory", "ASSET"),
               ("2000", "Accounts Payable", "PAYABLE"), ("2200", "VAT Output", "TAX"),
               ("3000", "Share Capital", "EQUITY"), ("4000", "Sales", "REVENUE"),
               ("5000", "Cost of Sales", "COST_OF_GOODS"), ("6300", "Office Expenses", "EXPENSE")]
        for c, n, t in coa:
            w.accounts[c] = Account.objects.create(company=company, code=c, name=n, account_type=t)

        names = iter(FIRST)
        offset = COMPANIES.index((name, code, industry)) * len(ROLES)
        pool = FIRST[offset % len(FIRST):] + FIRST
        names = iter(pool)
        for i, role in enumerate(ROLES):
            first = next(names)
            last = LAST[(i + offset) % len(LAST)]
            pw = secrets.token_urlsafe(14)
            user = User.objects.create(
                email=f"{role.lower().replace('_', '.')}.{code.lower()}.{self.run_tag.lower()}@sim.mofisi.test",
                username=f"{role.lower()}-{code.lower()}-{self.run_tag.lower()}",
                first_name=first, last_name=last, is_active=True,
            )
            user.set_password(pw)
            user.save()
            CompanyMembership.objects.create(company=company, user=user, role=role, is_default=True)
            w.staff[role] = Staff(user=user, role=role, company=company, password=pw,
                                  name=f"{first} {last}")

        dept = Department.objects.create(company=company, name="Operations")
        wh = Warehouse.objects.create(company=company, name="Main Store", code=f"WH-{code}"[:20], is_default=True)
        uom, _ = UnitOfMeasure.objects.get_or_create(company=company, name="Each", defaults={"abbreviation": "ea"})
        product = Product.objects.create(company=company, sku=f"{tag}-SKU", name=f"{code} Widget",
                                         product_type="GOODS", unit_of_measure=uom,
                                         cost_price=Decimal("300"), selling_price=Decimal("500"))
        customer = Customer.objects.create(company=company, name=f"{code} Anchor Customer {self.run_tag}",
                                           email=f"buyer@{code.lower()}-customer.co.ke", payment_terms_days=30)
        supplier = Supplier.objects.create(company=company, name=f"{code} Main Supplier {self.run_tag}",
                                           email=f"sales@{code.lower()}-supplier.co.ke")
        today = date.today()
        invoice = Invoice.objects.create(
            company=company, reference=f"{tag}-INV-1", customer=customer, invoice_date=today,
            due_date=today + timedelta(days=30), status="SENT", subtotal=Decimal("1000"),
            tax_amount=Decimal("160"), total_amount=Decimal("1160"), outstanding_amount=Decimal("1160"))
        InvoiceLine.objects.create(invoice=invoice, product=product, description="2 x widget",
                                   quantity=Decimal("2"), unit_price=Decimal("500"))
        je = JournalEntry.objects.create(company=company, reference=f"{tag}-JE-1", entry_type="INVOICE",
                                         posting_date=today, status="DRAFT")
        JournalEntryLine.objects.create(journal_entry=je, account=w.accounts["1100"], debit_amount=Decimal("1160"))
        JournalEntryLine.objects.create(journal_entry=je, account=w.accounts["4000"], credit_amount=Decimal("1000"))
        JournalEntryLine.objects.create(journal_entry=je, account=w.accounts["2200"], credit_amount=Decimal("160"))
        je.post()
        invoice.journal_entry = je
        invoice.save()
        po = PurchaseOrder.objects.create(company=company, reference=f"{tag}-PO-1", supplier=supplier,
                                          order_date=today, status="SENT", subtotal=Decimal("600"),
                                          total_amount=Decimal("600"))
        bill = Bill.objects.create(company=company, reference=f"{tag}-BILL-1", supplier=supplier,
                                   bill_date=today, due_date=today + timedelta(days=30), status="RECEIVED",
                                   subtotal=Decimal("600"), total_amount=Decimal("600"),
                                   outstanding_amount=Decimal("600"))
        req = PurchaseRequisition.objects.create(company=company, reference=f"{tag}-REQ-1",
                                                 requested_by=w.staff["PROCUREMENT"].user,
                                                 department=dept, required_date=today + timedelta(days=7),
                                                 status="PENDING_APPROVAL")
        emp = Employee.objects.create(company=company, employee_number=f"{tag}-E1",
                                      first_name="Seeded", last_name=f"Employee {code}",
                                      date_hired=today - timedelta(days=400))

        w.objects = {
            "organizations.company": company, "organizations.department": dept,
            "organizations.warehouse": wh, "inventory.product": product,
            "crm.customer": customer, "crm.supplier": supplier, "sales.invoice": invoice,
            "accounting.journalentry": je, "accounting.account": w.accounts["1100"],
            "procurement.purchaseorder": po, "procurement.bill": bill,
            "procurement.purchaserequisition": req, "hr.employee": emp,
            "accounts.user": w.staff["VIEWER"].user,
        }
        w.markers = [invoice.reference, customer.name, supplier.name, po.reference,
                     bill.reference, req.reference, company.name, emp.employee_number]
        return w

    def _build_consultant(self):
        from apps.accounts.models import User
        from apps.organizations.models import CompanyMembership
        pw = secrets.token_urlsafe(14)
        user = User.objects.create(email=f"consultant.{self.run_tag.lower()}@sim.mofisi.test",
                                   username=f"consultant-{self.run_tag.lower()}",
                                   first_name="Wanjiru", last_name="Kamau")
        user.set_password(pw)
        user.save()
        CompanyMembership.objects.create(company=self.worlds[0].company, user=user, role="ACCOUNTANT", is_default=True)
        CompanyMembership.objects.create(company=self.worlds[1].company, user=user, role="AUDITOR")
        return {"user": user, "password": pw, "name": "Wanjiru Kamau"}

    # -------------------------------------------------------- http helpers

    def _client(self):
        return Client(enforce_csrf_checks=True)

    def _login(self, staff: Staff, client=None):
        c = client or self._client()
        login_url = reverse("accounts:login")
        c.get(login_url)
        r = c.post(login_url, {"email": staff.user.email, "password": staff.password,
                               "csrfmiddlewaretoken": c.cookies["csrftoken"].value})
        if r.status_code != 302:
            self._issue("BLOCKED_LEGIT", "HIGH", "Employee cannot sign in",
                        staff.company.name, staff.role, login_url,
                        f"login returned {r.status_code}")
        staff.client = c
        return c

    def _token(self, c):
        if "csrftoken" not in c.cookies:
            c.get(reverse("dashboard:index"))
        return c.cookies["csrftoken"].value

    def _get(self, c, url, who=None):
        try:
            r = c.get(url)
        except Exception as exc:
            self._crash(url, exc, who)
            return None
        if r.status_code >= 500:
            self._crash(url, f"HTTP {r.status_code}", who)
            return None
        return r

    def _post(self, c, url, data, who=None):
        data = dict(data)
        data["csrfmiddlewaretoken"] = self._token(c)
        try:
            r = c.post(url, data)
        except Exception as exc:
            self._crash(url, exc, who)
            return None
        if r.status_code >= 500:
            self._crash(url, f"HTTP {r.status_code}", who)
            return None
        return r

    def _crash(self, url, exc, who):
        detail = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        if not isinstance(exc, str):
            tb = traceback.extract_tb(exc.__traceback__)
            app_frames = [f for f in tb if "/apps/" in f.filename or "/templates/" in f.filename]
            if app_frames:
                f = app_frames[-1]
                detail += f" (at {f.filename.split('/Merp/')[-1]}:{f.lineno})"
        self._issue("CRASH", "HIGH", "Page crashes", who.company.name if who else "",
                    who.role if who else "", url, str(detail)[:300])

    @staticmethod
    def _allowed(r):
        """A GET counts as 'got in' when the page rendered normally."""
        return r is not None and r.status_code == 200

    # ----------------------------------------------- capability attempts

    def _attempt(self, w: CompanyWorld, staff: Staff, cap):
        key = cap[0]
        fn = getattr(self, f"_cap_{key}")
        try:
            got_in, url = fn(w, staff)
        except Exception as exc:
            self._crash(f"capability:{key}", exc, staff)
            return
        should = staff.role in cap[2]
        if got_in and should:
            self.steps_ok += 1
        elif not got_in and not should:
            self.steps_ok += 1
        elif got_in and not should:
            sev = "HIGH" if key in ("see_dashboard_financials", "fetch_kpi_widget") else "CRITICAL" if key in ("grant_system_admin", "create_staff_user", "deactivate_colleague",
                                        "approve_requisition", "assign_staff_role") else (
                "HIGH" if cap[3] == "write" or key in ("view_payroll", "view_employees", "view_user_list")
                else "MEDIUM")
            self._issue("PERMISSION_GAP", sev, f"{self._role_name(staff.role)} can {cap[1]}",
                        w.company.name, staff.role, url, "role should not have this right")
        else:
            self._issue("BLOCKED_LEGIT", "HIGH", f"{self._role_name(staff.role)} cannot {cap[1]}",
                        w.company.name, staff.role, url, "role needs this to do their job")

    def _cap_view_dashboard(self, w, s):
        url = reverse("dashboard:index")
        return self._allowed(self._get(s.client, url, s)), url

    def _cap_see_dashboard_financials(self, w, s):
        url = reverse("dashboard:index")
        r = self._get(s.client, url, s)
        return self._allowed(r) and b"Cash on hand" in r.content, url

    def _cap_fetch_kpi_widget(self, w, s):
        url = reverse("dashboard:kpi_widgets")
        return self._allowed(self._get(s.client, url, s)), url

    def _cap_view_sales_invoices(self, w, s):
        url = reverse("sales:invoice_list")
        r = self._get(s.client, url, s)
        return self._allowed(r) and w.objects["sales.invoice"].reference.encode() in r.content, url

    def _cap_create_customer(self, w, s):
        from apps.crm.models import Customer
        url = reverse("crm:customer_create")
        name = f"Cust {s.role} {w.code} {self.run_tag}"
        self._post(s.client, url, {
            "name": name, "customer_type": "BUSINESS", "tax_id": "", "phone": "", "email": "",
            "website": "", "credit_limit": "0", "payment_terms_days": "30", "currency": "KES",
            "address_line1": "", "address_line2": "", "city": "Nairobi", "country": "Kenya", "notes": "",
        }, s)
        return Customer.objects.filter(name=name, company=w.company).exists(), url

    def _cap_create_invoice(self, w, s):
        from apps.sales.models import Invoice
        url = reverse("sales:invoice_create")
        ref = f"{w.code}-{s.role}-{self.run_tag}"[:50]
        today = date.today()
        data = {
            "reference": ref, "customer": str(w.objects["crm.customer"].pk),
            "invoice_date": today.isoformat(), "due_date": (today + timedelta(days=30)).isoformat(),
            "currency": "KES", "notes": "",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "1", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": str(w.objects["inventory.product"].pk), "lines-0-description": "Widget",
            "lines-0-quantity": "3", "lines-0-unit_price": "500", "lines-0-discount_percent": "0",
            "lines-0-tax_rate": "",
        }
        self._post(s.client, url, data, s)
        return Invoice.objects.filter(reference=ref, company=w.company).exists(), url

    def _cap_post_invoice_to_gl(self, w, s):
        from apps.sales.models import Invoice, InvoiceLine
        today = date.today()
        inv = Invoice.objects.create(
            company=w.company, reference=f"{w.code}-DRAFT-{s.role}-{self.run_tag}"[:50],
            customer=w.objects["crm.customer"], invoice_date=today, due_date=today + timedelta(days=30),
            status="DRAFT", subtotal=Decimal("500"), tax_amount=Decimal("0"),
            total_amount=Decimal("500"), outstanding_amount=Decimal("500"))
        InvoiceLine.objects.create(invoice=inv, description="Service", quantity=Decimal("1"),
                                   unit_price=Decimal("500"))
        url = reverse("sales:invoice_post", args=[inv.pk])
        self._post(s.client, url, {}, s)
        inv.refresh_from_db()
        return bool(inv.journal_entry_id), url

    def _cap_view_trial_balance(self, w, s):
        url = reverse("accounting:trial_balance")
        return self._allowed(self._get(s.client, url, s)), url

    def _cap_create_journal_entry(self, w, s):
        from apps.accounting.models import JournalEntry
        url = reverse("accounting:journal_entry_create")
        ref = f"JE-{w.code}-{s.role}-{self.run_tag}"[:50]
        self._post(s.client, url, {
            "reference": ref, "entry_type": "MANUAL", "posting_date": date.today().isoformat(),
            "notes": "Accrual", "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "2", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-account": str(w.accounts["6300"].pk), "lines-0-description": "Stationery",
            "lines-0-debit_amount": "250", "lines-0-credit_amount": "0",
            "lines-1-account": str(w.accounts["1000"].pk), "lines-1-description": "Cash",
            "lines-1-debit_amount": "0", "lines-1-credit_amount": "250",
        }, s)
        return JournalEntry.objects.filter(reference=ref, company=w.company).exists(), url

    def _cap_view_purchase_orders(self, w, s):
        url = reverse("procurement:po_list")
        r = self._get(s.client, url, s)
        return self._allowed(r) and w.objects["procurement.purchaseorder"].reference.encode() in r.content, url

    def _cap_create_requisition(self, w, s):
        from apps.procurement.models import PurchaseRequisition
        url = reverse("procurement:requisition_create")
        ref = f"REQ-{w.code}-{s.role}-{self.run_tag}"[:50]
        self._post(s.client, url, {
            "reference": ref, "department": str(w.objects["organizations.department"].pk),
            "required_date": (date.today() + timedelta(days=5)).isoformat(), "notes": "Restock",
        }, s)
        return PurchaseRequisition.objects.filter(reference=ref, company=w.company).exists(), url

    def _cap_approve_requisition(self, w, s):
        from apps.procurement.models import PurchaseRequisition
        req = PurchaseRequisition.objects.create(
            company=w.company, reference=f"APR-{w.code}-{s.role}-{self.run_tag}"[:50],
            requested_by=w.staff["PROCUREMENT"].user, required_date=date.today() + timedelta(days=3),
            status="PENDING_APPROVAL")
        url = reverse("procurement:requisition_approve", args=[req.pk])
        self._post(s.client, url, {}, s)
        req.refresh_from_db()
        return req.status == "APPROVED", url

    def _cap_view_stock(self, w, s):
        url = reverse("inventory:stock_levels")
        return self._allowed(self._get(s.client, url, s)), url

    def _cap_view_employees(self, w, s):
        url = reverse("hr:employee_list")
        r = self._get(s.client, url, s)
        return self._allowed(r) and w.objects["hr.employee"].employee_number.encode() in r.content, url

    def _cap_create_employee(self, w, s):
        from apps.hr.models import Employee
        url = reverse("hr:employee_create")
        num = f"E-{w.code}-{s.role}-{self.run_tag}"[:50]
        self._post(s.client, url, {
            "employee_number": num, "first_name": "New", "last_name": "Hire", "other_names": "",
            "gender": "F", "date_of_birth": "1995-04-12", "date_hired": date.today().isoformat(),
            "status": "ACTIVE", "department": "", "branch": "", "job_title": "Clerk",
            "reports_to": "", "national_id": "", "kra_pin": "", "nssf_number": "", "nhif_number": "",
            "phone": "", "email": "", "bank_name": "", "bank_account_number": "", "notes": "",
        }, s)
        return Employee.objects.filter(employee_number=num, company=w.company).exists(), url

    def _cap_view_payroll(self, w, s):
        url = reverse("payroll:run_list")
        return self._allowed(self._get(s.client, url, s)), url

    def _cap_use_pos(self, w, s):
        url = reverse("pos:terminal")
        return self._allowed(self._get(s.client, url, s)), url

    def _cap_view_reports(self, w, s):
        url = "/reports/"
        return self._allowed(self._get(s.client, url, s)), url

    def _cap_view_expenses(self, w, s):
        url = reverse("expenses:claim_list")
        return self._allowed(self._get(s.client, url, s)), url

    def _cap_view_user_list(self, w, s):
        url = reverse("accounts:user_list")
        r = self._get(s.client, url, s)
        return self._allowed(r) and w.staff["VIEWER"].user.email.encode() in r.content, url

    def _new_user_payload(self, email, **extra):
        data = {"first_name": "Temp", "last_name": "Staff", "email": email,
                "username": email.split("@")[0][:150], "phone": "", "job_title": "Clerk",
                "password": secrets.token_urlsafe(12), "company_role": "EMPLOYEE"}
        data.update(extra)
        return data

    def _cap_create_staff_user(self, w, s):
        from apps.accounts.models import User
        url = reverse("accounts:user_create")
        email = f"new.{s.role.lower()}.{w.code.lower()}.{self.run_tag.lower()}@sim.mofisi.test"
        self._post(s.client, url, self._new_user_payload(email), s)
        return User.objects.filter(email=email).exists(), url

    def _cap_assign_staff_role(self, w, s):
        from apps.organizations.models import CompanyMembership
        url = reverse("accounts:user_create")
        email = f"role.{s.role.lower()}.{w.code.lower()}.{self.run_tag.lower()}@sim.mofisi.test"
        self._post(s.client, url, self._new_user_payload(email, company_role="SALES_REP"), s)
        return CompanyMembership.objects.filter(user__email=email, company=w.company,
                                                role="SALES_REP").exists(), url

    def _cap_grant_system_admin(self, w, s):
        from apps.accounts.models import User
        url = reverse("accounts:user_create")
        email = f"sysadm.{s.role.lower()}.{w.code.lower()}.{self.run_tag.lower()}@sim.mofisi.test"
        self._post(s.client, url, self._new_user_payload(email, is_system_admin="on"), s)
        return User.objects.filter(email=email, is_system_admin=True).exists(), url

    def _cap_deactivate_colleague(self, w, s):
        from apps.accounts.models import User
        from apps.organizations.models import CompanyMembership
        victim = User.objects.create(email=f"victim.{s.role.lower()}.{w.code.lower()}.{self.run_tag.lower()}@sim.mofisi.test",
                                     username=f"victim-{s.role.lower()}-{w.code.lower()}-{self.run_tag.lower()}")
        CompanyMembership.objects.create(company=w.company, user=victim, role="EMPLOYEE")
        url = reverse("accounts:user_toggle", args=[victim.pk])
        self._post(s.client, url, {}, s)
        victim.refresh_from_db()
        return not victim.is_active, url

    def _cap_edit_branding(self, w, s):
        url = reverse("organizations:branding")
        before = w.company.primary_color
        colour = f"#{secrets.randbelow(0xFFFFFF):06X}"
        self._post(s.client, url, {"tagline": "", "primary_color": colour, "accent_color": "#111827",
                                   "document_header": "", "document_footer": "", "bank_details": ""}, s)
        w.company.refresh_from_db()
        changed = w.company.primary_color.upper() == colour
        if changed:
            w.company.primary_color = before
            w.company.save(update_fields=["primary_color"])
        return changed, url

    def _cap_open_control_panel(self, w, s):
        url = reverse("control_panel:index")
        return self._allowed(self._get(s.client, url, s)), url

    # ------------------------------------------------------------- agents

    def _company_day(self, w: CompanyWorld):
        before = len(self.issues)
        for staff in w.staff.values():
            for cap in CAPABILITIES:
                self._attempt(w, staff, cap)
        found = len(self.issues) - before
        mark = f"{GREEN}✓{RESET}" if not found else f"{YELLOW}!{RESET}"
        self.stdout.write(f"  {mark} {w.company.name}: {len(w.staff)} staff x {len(CAPABILITIES)} tasks"
                          f" — {found} problem(s) hit")

    def _consultant_day(self):
        jamii, safari = self.worlds[0], self.worlds[1]
        s = Staff(user=self.consultant["user"], role="ACCOUNTANT", company=jamii.company,
                  password=self.consultant["password"], name=self.consultant["name"])
        self._login(s)
        # At Jamii she is the accountant: she should be able to journal.
        ok, url = self._cap_create_journal_entry(jamii, s)
        if not ok:
            self._issue("BLOCKED_LEGIT", "HIGH", "Outside accountant cannot journal at a client where she is the accountant",
                        jamii.company.name, "ACCOUNTANT", url)
        # Switch to Safari, where she is only the auditor.
        self._get(s.client, reverse("dashboard:switch_company", args=[safari.company.pk]), s)
        s.company, s.role = safari.company, "AUDITOR"
        ok, url = self._cap_create_journal_entry(safari, s)
        if ok:
            self._issue("PERMISSION_GAP", "HIGH",
                        "Rights don't follow the active company: auditor at one client can journal there "
                        "because she is accountant at another", safari.company.name, "AUDITOR", url)
        ok, url = self._cap_view_trial_balance(safari, s)
        if not ok:
            self._issue("BLOCKED_LEGIT", "HIGH", "Auditor cannot see the trial balance at her client",
                        safari.company.name, "AUDITOR", url)
        # She has no membership at Mavuno: switching there must not work.
        mavuno = self.worlds[2]
        self._get(s.client, reverse("dashboard:switch_company", args=[mavuno.company.pk]), s)
        r = self._get(s.client, reverse("sales:invoice_list"), s)
        if r is not None and mavuno.objects["sales.invoice"].reference.encode() in r.content:
            self._issue("TENANT_LEAK", "CRITICAL", "Switching to a company you don't belong to works",
                        mavuno.company.name, "OUTSIDER", reverse("dashboard:switch_company", args=[mavuno.company.pk]))
        self.stdout.write(f"  {GREEN}✓{RESET} {self.consultant['name']} worked at two clients")

    def _onboarding_agent(self):
        from apps.organizations.models import Company
        c = self._client()
        email = f"owner.{self.run_tag.lower()}@newbiz.mofisi.test"
        pw = secrets.token_urlsafe(14)
        c.get(reverse("accounts:register"))
        self._post(c, reverse("accounts:register"), {
            "first_name": "Neema", "last_name": "Achieng", "email": email, "phone": "+254700000001",
            "password": pw, "password_confirm": pw, "terms_accepted": "on"})
        r = self._get(c, reverse("dashboard:index"))
        if r is None or r.status_code != 200:
            self._issue("BLOCKED_LEGIT", "HIGH", "New owner is not signed in after registering",
                        "(new business)", "OWNER", reverse("accounts:register"))
            c = self._client()
            c.get(reverse("accounts:login"))
            c.post(reverse("accounts:login"), {"email": email, "password": pw,
                                               "csrfmiddlewaretoken": c.cookies["csrftoken"].value})
        name = f"Neema Fresh Juice {self.run_tag}"
        self._post(c, reverse("organizations:company_create"), {
            "name": name, "legal_name": name, "registration_number": "", "tax_id": "P051112223X",
            "vat_number": "", "currency": "KES", "timezone": "Africa/Nairobi",
            "fiscal_year_start_month": "1", "industry": "Food", "phone": "", "email": "",
            "website": "", "address_line1": "", "address_line2": "", "city": "Nairobi",
            "state_province": "", "postal_code": "", "country": "Kenya"})
        company = Company.objects.filter(name=name).first()
        if not company:
            self._issue("BLOCKED_LEGIT", "HIGH", "New owner cannot create their company",
                        "(new business)", "OWNER", reverse("organizations:company_create"))
            return
        from apps.organizations.models import CompanyMembership
        membership = CompanyMembership.objects.filter(company=company, user__email=email).first()
        if not membership or membership.role != "ADMIN":
            self._issue("BLOCKED_LEGIT", "CRITICAL",
                        "New owner is not made admin of the company they just created, so they can't use it",
                        name, "OWNER", reverse("organizations:company_create"),
                        "no ADMIN membership created" if not membership else f"membership role {membership.role}")
        r = self._get(c, reverse("sales:invoice_create"))
        if r is None or r.status_code != 200 or b'name="customer"' not in r.content:
            self._issue("BLOCKED_LEGIT", "HIGH", "New owner cannot start invoicing in their new company",
                        name, "OWNER", reverse("sales:invoice_create"))
        self.stdout.write(f"  {GREEN}✓{RESET} Onboarding attempted for {name}")

    # ------------------------------------------------------------- crawl

    def _routes(self):
        def walk(resolver, prefix="", ns=None):
            for p in resolver.url_patterns:
                if isinstance(p, URLResolver):
                    yield from walk(p, prefix + str(p.pattern), p.namespace or ns)
                elif isinstance(p, URLPattern):
                    yield ns, p.name or "", prefix + str(p.pattern), p
        for ns, name, pattern, p in walk(get_resolver()):
            if pattern.startswith(("admin/", "api/")) or ns in ("accounts",) and name in (
                    "logout", "password_reset_confirm", "login", "register"):
                continue
            if ACTION_TOKENS & set(name.split("_")):
                continue
            yield ns, name, pattern, p

    def _model_label(self, p):
        view_cls = getattr(p.callback, "view_class", None)
        model = getattr(view_cls, "model", None) if view_cls else None
        return model._meta.label_lower if model is not None else None

    def _fill(self, pattern, pk):
        params = re.findall(r"<[^>]+>", pattern)
        if not params:
            return "/" + pattern
        if len(params) == 1 and pk is not None and params[0].startswith("<uuid:"):
            return "/" + pattern.replace(params[0], str(pk))
        return None

    def _crawl(self):
        routes = list(self._routes())
        pages = 0
        crawlers = list(self.worlds[0].staff.values()) + [w.staff["ADMIN"] for w in self.worlds[1:]]
        for staff in crawlers:
            w = next(x for x in self.worlds if x.company == staff.company)
            for ns, name, pattern, p in routes:
                label = self._model_label(p)
                obj = w.objects.get(label) if label else None
                url = self._fill(pattern, obj.pk if obj is not None else None)
                if url is None:
                    continue
                self._get(staff.client, url, staff)
                pages += 1
        self.stdout.write(f"  {GREEN}✓{RESET} {pages} page views by {len(crawlers)} staff")

    # ----------------------------------------------------------- auditor

    def _audit_tenant_isolation(self):
        a, b = self.worlds[0], self.worlds[1]
        admin = a.staff["ADMIN"]
        probed = 0
        # 1. Every detail page, pointed at the other company's records.
        for ns, name, pattern, p in self._routes():
            label = self._model_label(p)
            if not label or label not in b.objects:
                continue
            obj = b.objects[label]
            url = self._fill(pattern, obj.pk)
            if not url:
                continue
            r = self._get(admin.client, url, admin)
            probed += 1
            if r is not None and r.status_code == 200 and any(m.encode() in r.content for m in b.markers):
                self._issue("TENANT_LEAK", "CRITICAL",
                            f"{label.split('.')[-1].title()} of another company is visible",
                            a.company.name, "ADMIN", url, f"{a.code} admin opened a {b.code} record")
        # 2. Every list page, searched for the other company's data.
        for ns, name, pattern, p in self._routes():
            url = self._fill(pattern, None)
            if not url:
                continue
            r = self._get(admin.client, url, admin)
            probed += 1
            if r is None or r.status_code != 200:
                continue
            leaked = [m for m in b.markers if m.encode() in r.content]
            if leaked:
                self._issue("TENANT_LEAK", "CRITICAL", "List shows another company's data",
                            a.company.name, "ADMIN", url, f"contains {b.code} data: {leaked[0]}")
            other_staff = [s.user.email for s in b.staff.values() if s.user.email.encode() in r.content]
            if other_staff:
                self._issue("TENANT_LEAK", "CRITICAL", "List shows another company's staff",
                            a.company.name, "ADMIN", url, f"contains {b.code} staff e.g. {other_staff[0]}")
        # 3. Writes against the other company's records.
        from apps.accounts.models import User
        from apps.organizations.models import Company
        from apps.procurement.models import PurchaseRequisition
        from apps.sales.models import Invoice
        req = b.objects["procurement.purchaserequisition"]
        url = reverse("procurement:requisition_approve", args=[req.pk])
        self._post(admin.client, url, {}, admin)
        if PurchaseRequisition.objects.get(pk=req.pk).status == "APPROVED":
            self._issue("TENANT_LEAK", "CRITICAL", "Can approve another company's requisition",
                        a.company.name, "ADMIN", url)
        inv = b.objects["sales.invoice"]
        url = reverse("sales:invoice_cancel", args=[inv.pk])
        self._post(admin.client, url, {}, admin)
        if Invoice.objects.get(pk=inv.pk).status == "CANCELLED":
            self._issue("TENANT_LEAK", "CRITICAL", "Can cancel another company's invoice",
                        a.company.name, "ADMIN", url)
        victim = b.staff["ADMIN"].user
        url = reverse("accounts:user_toggle", args=[victim.pk])
        self._post(admin.client, url, {}, admin)
        if not User.objects.get(pk=victim.pk).is_active:
            self._issue("TENANT_LEAK", "CRITICAL", "Can lock out another company's admin",
                        a.company.name, "ADMIN", url)
            User.objects.filter(pk=victim.pk).update(is_active=True)
        url = reverse("organizations:company_edit", args=[b.company.pk])
        self._post(admin.client, url, {
            "name": f"HIJACKED {self.run_tag}", "currency": "KES", "timezone": "Africa/Nairobi",
            "fiscal_year_start_month": "1"}, admin)
        if Company.objects.get(pk=b.company.pk).name.startswith("HIJACKED"):
            self._issue("TENANT_LEAK", "CRITICAL", "Can rename another company", a.company.name, "ADMIN", url)
            Company.objects.filter(pk=b.company.pk).update(name=b.company.name)
        url = reverse("organizations:branding_company", args=[b.company.pk])
        self._post(admin.client, url, {"tagline": "pwned", "primary_color": "#000000", "accent_color": "#000000",
                                       "document_header": "", "document_footer": "", "bank_details": ""}, admin)
        if Company.objects.get(pk=b.company.pk).tagline == "pwned":
            self._issue("TENANT_LEAK", "CRITICAL", "Can rebrand another company (e.g. swap its bank details)",
                        a.company.name, "ADMIN", url)
        # 4. Journal posted against the other company's accounts.
        from apps.accounting.models import JournalEntryLine
        acc_s = a.staff["ACCOUNTANT"]
        ref = f"XJE-{self.run_tag}"
        self._post(acc_s.client, reverse("accounting:journal_entry_create"), {
            "reference": ref, "entry_type": "MANUAL", "posting_date": date.today().isoformat(), "notes": "",
            "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "2",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-account": str(b.accounts["1010"].pk), "lines-0-description": "", "lines-0-debit_amount": "999",
            "lines-0-credit_amount": "0",
            "lines-1-account": str(b.accounts["3000"].pk), "lines-1-description": "", "lines-1-debit_amount": "0",
            "lines-1-credit_amount": "999"}, acc_s)
        if JournalEntryLine.objects.filter(journal_entry__reference=ref, account__company=b.company).exists():
            self._issue("TENANT_LEAK", "CRITICAL", "Accountant can post a journal into another company's accounts",
                        a.company.name, "ACCOUNTANT", reverse("accounting:journal_entry_create"))
        self.stdout.write(f"  {GREEN}✓{RESET} {probed} cross-company probes + 6 cross-company write attempts")

    def _audit_security(self):
        from apps.accounts.models import User
        w = self.worlds[2]
        admin = w.staff["ADMIN"]
        # State change over GET (a link in an email could lock someone out).
        victim = w.staff["EMPLOYEE"].user
        url = reverse("accounts:user_toggle", args=[victim.pk])
        self._get(admin.client, url, admin)
        if not User.objects.get(pk=victim.pk).is_active:
            self._issue("SECURITY", "HIGH", "Deactivating a user works over a plain link (GET), so it can be triggered "
                        "by a malicious link", w.company.name, "ADMIN", url)
            User.objects.filter(pk=victim.pk).update(is_active=True)
        # Open redirect after switching company.
        url = reverse("dashboard:switch_company", args=[w.company.pk]) + "?next=https://evil.example/x"
        r = self._get(admin.client, url, admin)
        if r is not None and r.status_code in (301, 302) and "evil.example" in r.get("Location", ""):
            self._issue("SECURITY", "MEDIUM", "Company switcher redirects to any outside site (?next=)",
                        w.company.name, "ADMIN", url)
        # Company admin escalating to platform-wide admin.
        email = f"esc.{self.run_tag.lower()}@sim.mofisi.test"
        self._post(admin.client, reverse("accounts:user_create"),
                   self._new_user_payload(email, is_system_admin="on"), admin)
        if User.objects.filter(email=email, is_system_admin=True).exists():
            self._issue("SECURITY", "CRITICAL",
                        "A company admin can create a platform-wide system admin with access to every company",
                        w.company.name, "ADMIN", reverse("accounts:user_create"))
        self.stdout.write(f"  {GREEN}✓{RESET} Security probes done")

    def _audit_books(self):
        from django.db.models import F, Sum
        from apps.accounting.models import JournalEntry, JournalEntryLine
        for w in self.worlds:
            qs = JournalEntryLine.objects.filter(journal_entry__company=w.company, journal_entry__status="POSTED")
            dr = qs.aggregate(t=Sum("debit_amount"))["t"] or Decimal("0")
            cr = qs.aggregate(t=Sum("credit_amount"))["t"] or Decimal("0")
            if abs(dr - cr) > Decimal("0.01"):
                self._issue("ACCOUNTING", "CRITICAL", "Trial balance does not balance", w.company.name,
                            url="/accounting/trial-balance/", detail=f"Dr {dr} vs Cr {cr}")
            for je in JournalEntry.objects.filter(company=w.company, status="POSTED"):
                if not je.is_balanced():
                    self._issue("ACCOUNTING", "CRITICAL", "Posted journal is unbalanced", w.company.name,
                                url=f"/accounting/journal-entries/{je.pk}/", detail=je.reference)
            foreign = JournalEntryLine.objects.filter(journal_entry__company=w.company).exclude(
                account__company=F("journal_entry__company"))
            if foreign.exists():
                self._issue("ACCOUNTING", "CRITICAL", "Journal lines point at another company's accounts",
                            w.company.name, detail=f"{foreign.count()} line(s)")
            for inv in w.company.invoices.filter(journal_entry__isnull=False).select_related("journal_entry"):
                if inv.journal_entry.company_id != w.company.id:
                    self._issue("ACCOUNTING", "CRITICAL", "Invoice posted into another company's ledger",
                                w.company.name, detail=inv.reference)
            self.stdout.write(f"  {GREEN}✓{RESET} {w.company.name}: Dr {dr:,.2f} / Cr {cr:,.2f}")

    # ------------------------------------------------------------ report

    def _issue(self, category, severity, title, company="", role="", url="", detail=""):
        self.issues.append(Issue(category, severity, title, company, role, url, detail))

    @staticmethod
    def _role_name(role):
        return role.replace("_", " ").title().replace("Pos ", "POS ").replace("Hr ", "HR ")

    def _auditor_report(self):
        groups = defaultdict(list)
        for i in self.issues:
            groups[i.key].append(i)
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
        rows = sorted(groups.values(), key=lambda g: (order[g[0].severity], g[0].category, -len(g)))

        self._banner("Auditor agent — findings")
        counts = defaultdict(int)
        for g in rows:
            counts[g[0].severity] += 1
        self.stdout.write(f"  Checks that behaved: {GREEN}{self.steps_ok}{RESET}")
        self.stdout.write(f"  Distinct problems:   {RED if rows else GREEN}{len(rows)}{RESET}  "
                          f"(critical {counts['CRITICAL']}, high {counts['HIGH']}, medium {counts['MEDIUM']}) "
                          f"from {len(self.issues)} occurrences")
        colour = {"CRITICAL": RED, "HIGH": YELLOW, "MEDIUM": CYAN}
        for g in rows:
            i = g[0]
            who = sorted({x.role for x in g if x.role})
            where = sorted({x.company for x in g if x.company})
            self.stdout.write(f"  {colour[i.severity]}{i.severity:8}{RESET} {i.category:14} {i.title}"
                              f"{DIM}  x{len(g)}  {UUID_RE.sub('<id>', i.url)}"
                              f"{'  roles: ' + ', '.join(who[:4]) + ('…' if len(who) > 4 else '') if who else ''}"
                              f"{'  — ' + i.detail if i.detail and len(g) == 1 else ''}{RESET}")
        if not rows:
            self.stdout.write(f"\n  {GREEN}{BOLD}No problems found. Every role could do its job and nothing more.{RESET}")

        md = ["# mofisi multi-company simulation — auditor report", "",
              f"Run `{self.run_tag}` on {date.today().isoformat()}. "
              f"{len(self.worlds)} companies, {sum(len(w.staff) for w in self.worlds)} staff across "
              f"{len(ROLES)} roles, plus an outside accountant and a new-business signup.", "",
              f"- Checks that behaved: **{self.steps_ok}**",
              f"- Distinct problems: **{len(rows)}** (critical {counts['CRITICAL']}, high {counts['HIGH']}, "
              f"medium {counts['MEDIUM']}) from {len(self.issues)} occurrences", ""]
        if rows:
            md += ["| Severity | Category | Problem | Seen | Where | Roles | Example detail |",
                   "|---|---|---|---|---|---|---|"]
            for g in rows:
                i = g[0]
                who = ", ".join(sorted({x.role for x in g if x.role})) or "—"
                md.append(f"| {i.severity} | {i.category} | {i.title} | {len(g)} | "
                          f"`{UUID_RE.sub('<id>', i.url) or '—'}` | {who} | {i.detail or '—'} |")
        else:
            md.append("No problems found. Every role could do its job and nothing more.")
        return "\n".join(md) + "\n"

    def _banner(self, text):
        self.stdout.write(f"\n{BOLD}{text}{RESET}\n{DIM}{'─' * 64}{RESET}")

    def _section(self, text):
        self.stdout.write(f"\n{BOLD}{CYAN}{text}{RESET}")
