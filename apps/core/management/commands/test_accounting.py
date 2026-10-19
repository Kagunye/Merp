"""Accountant smoke test: create a real company through the forms, post the
money flows the way a bookkeeper would, then verify the double-entry rules and
that postings land on the recommended accounts.

Usage:
    python manage.py test_accounting          # rolls the whole run back
    python manage.py test_accounting --keep   # leave the test company behind

Flow (all amounts KES):
    1. company form            -> chart of accounts, FY + 12 periods, HQ, warehouse, VAT 16%
    2. product form            -> GL routing on the product (4200 / 5000 / 1400)
    3. opening journal         -> Dr 1100 20,000 + Dr 1200 80,000 / Cr 3100 100,000
    4. stock receipt 100 @ 50  -> Dr 1400 5,000 / Cr 2100 5,000
       stock delivery 10 @ 50  -> Dr 5000   500 / Cr 1400   500
    5. supplier bill 2,000+320 -> Dr 1400 2,000, Dr 2210 320 / Cr 2100 2,320
       bill payment 2,320      -> Dr 2100 2,320 / Cr 1100 2,320
    6. sales order 10@100 +VAT, 1@200, 5% header discount
       -> subtotal 1,200, tax 160, total 1,300 and no ledger entry
    7. invoice 5@100 +VAT, freight 300 -> Dr 1300 880 / Cr 4200 500, Cr 4100 300, Cr 2200 80
       customer payment 500    -> Dr 1200 500 / Cr 1300 500
    8. expense claim 1,200+300 -> Dr 6400 1,200, Dr 6000 300 / Cr 1100 1,500

Every posted line therefore totals Dr 113,020 = Cr 113,020.
"""
import datetime
import re
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum
from django.test import Client
from django.test.utils import setup_test_environment, teardown_test_environment
from django.urls import reverse

D = Decimal
Z = D("0")

# The chart codes a trading company is expected to transact through.
RECOMMENDED = [
    ("1100", "Cash on Hand"),
    ("1200", "Bank"),
    ("1300", "Accounts Receivable"),
    ("1400", "Inventory"),
    ("2100", "Accounts Payable"),
    ("2200", "VAT Payable"),
    ("2210", "VAT Input"),
    ("4100", "Sales Revenue"),
    ("5000", "Cost of Goods Sold"),
    ("6000", "Operating Expenses"),
]

# Expected (debit, credit) totals per account over all posted lines in this run.
EXPECTED = {
    "1100": ("20000.00", "3820.00"),
    "1200": ("80500.00", "0.00"),
    "1300": ("880.00", "500.00"),
    "1400": ("7000.00", "500.00"),
    "2100": ("2320.00", "7320.00"),
    "2200": ("0.00", "80.00"),
    "2210": ("320.00", "0.00"),
    "3100": ("0.00", "100000.00"),
    "4100": ("0.00", "300.00"),
    "4200": ("0.00", "500.00"),
    "5000": ("500.00", "0.00"),
    "6000": ("300.00", "0.00"),
    "6400": ("1200.00", "0.00"),
}
EXPECTED_TOTAL = D("113020.00")

INBOUND_STOCK = ("RECEIPT", "OPENING", "ADJUSTMENT_PLUS", "PRODUCTION_IN")

# What a browser submits for an untouched extra row: the rendered defaults make the
# row unchanged, which is how Django decides to skip it (empty_permitted).
BLANK_INVOICE_LINE = {
    "product": "", "description": "", "quantity": "", "unit_price": "",
    "discount_percent": "0", "tax_rate": "",
}
BLANK_JOURNAL_LINE = {
    "account": "", "description": "", "debit_amount": "0", "credit_amount": "0",
}


def formset(prefix, items, blank=None, blanks=1):
    """Management form + row data for an inline formset."""
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(items) + blanks),
        f"{prefix}-INITIAL_FORMS": "0",
        f"{prefix}-MIN_NUM_FORMS": "1",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for i, row in enumerate(items):
        for key, value in row.items():
            data[f"{prefix}-{i}-{key}"] = "" if value is None else str(value)
    for j in range(blanks):
        if blank:
            for key, value in blank.items():
                data[f"{prefix}-{len(items) + j}-{key}"] = "" if value is None else str(value)
    return data


def merge(*parts):
    out = {}
    for part in parts:
        out.update(part)
    return out


class Command(BaseCommand):
    help = "Post the accounting flows through the real forms and verify the ledger rules."

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep", action="store_true",
            help="Commit the test data instead of rolling it back.",
        )

    def handle(self, *args, **opts):
        self.passed = 0
        self.failures = []
        self.notes = []
        self.today = datetime.date.today()

        # response.context is only filled while the test template renderer is installed.
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
        for line in self.notes:
            self.stdout.write(self.style.WARNING(f"  note: {line}"))
        if not opts["keep"]:
            self.stdout.write("Test data rolled back (use --keep to retain it).")
        else:
            self.stdout.write(
                f"Test company kept — sign in as {self.login_email} / smoke-test-pw"
            )
        return 1 if self.failures else 0

    # ────────────────────────────── reporting ────────────────────────────────

    def section(self, title):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"-- {title}"))

    def expect(self, label, condition, detail=""):
        if condition:
            self.passed += 1
            self.stdout.write(f"  ok   {label}" + (f"  ({detail})" if detail else ""))
            return True
        self.failures.append(f"{label} — {detail}" if detail else label)
        self.stdout.write(self.style.ERROR(f"  FAIL {label} {detail}"))
        return False

    def note(self, text):
        self.notes.append(text)
        self.stdout.write(self.style.WARNING(f"  note: {text}"))

    def show_errors(self, response, marker="       "):
        ctx = getattr(response, "context", None)
        if not ctx:
            return
        form = ctx.get("form")
        if form is not None and getattr(form, "errors", None):
            for field, msgs in form.errors.items():
                self.stdout.write(f"{marker}{field}: {' '.join(msgs)}")
        lines_formset = ctx.get("line_formset")
        if lines_formset is not None:
            for err in lines_formset.non_form_errors():
                self.stdout.write(f"{marker}lines: {err}")
            for i, err in enumerate(lines_formset.errors):
                if err:
                    self.stdout.write(f"{marker}line {i}: {err}")

    def flash(self, response):
        ctx = getattr(response, "context", None)
        if not ctx or "messages" not in ctx:
            return []
        return [m.message for m in ctx["messages"]]

    def flashes(self, client, response):
        """Messages only appear once the redirect target renders them."""
        location = response.get("Location")
        if not location:
            return self.flash(response)
        return self.flash(client.get(location))

    def expect_flash(self, client, response, needle, label):
        """Follow the redirect once; rendering consumes the messages."""
        messages_ = self.flashes(client, response)
        return self.expect(label, any(needle in m for m in messages_), str(messages_))

    def post(self, client, name, url, data):
        """POST a form and require a redirect (a 200 means validation failed)."""
        response = client.post(url, data)
        if response.status_code in (301, 302, 303):
            return response
        self.stdout.write(self.style.ERROR(
            f"  FAIL {name} form rejected (HTTP {response.status_code})"))
        self.show_errors(response)
        self.failures.append(f"{name} form accepted")
        return response

    def reject(self, client, name, url, data):
        """POST data that must NOT be saved."""
        response = client.post(url, data)
        if response.status_code == 200:
            self.passed += 1
            self.stdout.write(f"  ok   {name} blocked by validation")
            self.show_errors(response)
        else:
            self.failures.append(f"{name} should have been rejected (HTTP {response.status_code})")
            self.stdout.write(self.style.ERROR(f"  FAIL {name} was accepted"))
        return response

    def get_ok(self, client, name, url):
        response = client.get(url)
        self.expect(f"{name} renders", response.status_code == 200, f"HTTP {response.status_code}")
        return response

    # ─────────────────────────────── the run ─────────────────────────────────

    def run(self):
        from apps.accounts.models import User

        stamp = datetime.datetime.now().strftime("%H%M%S%f")
        self.stamp = stamp
        user = User.objects.create(
            username=f"acct.{stamp}", email=f"acct.{stamp}@smoke.local",
            first_name="Ada", last_name="Bookkeeper",
        )
        user.set_password("smoke-test-pw")
        user.save()
        self.user = user
        self.login_email = user.email

        client = Client()
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")

        self.section("1. Company and master data through the forms")
        self.company = self.create_company(client, user, stamp)
        self.setup_master_data()
        self.create_product(client)

        self.section("2. Opening balances (manual journal, then Post)")
        self.post_opening_journal(client)

        self.section("3. Inventory: goods receipt then delivery")
        self.post_stock(client)

        self.section("4. Purchasing: bill, bill payment, mismatched bill")
        self.post_purchasing(client)

        self.section("5. Sales: order, VAT invoice then customer payment")
        self.post_sales_order(client)
        self.post_sales(client)

        self.section("6. Expenses: claim lines, approval, payment")
        self.post_expense_claim(client)

        self.section("7. Ledger rules")
        self.verify_entries()
        self.verify_unbalanced_rejected(client)
        self.verify_trial_balance(client)
        self.verify_subledgers()
        self.verify_account_routing()
        self.verify_reports(client)

        self.section("8. Recommended accounts")
        self.verify_recommended_accounts()

        self.section("9. Currency rules and tenant walls")
        self.verify_currency_and_tenancy(client)

    def verify_currency_and_tenancy(self, client):
        """One book currency per company, and no sight of another company's rows."""
        from apps.accounts.models import User
        from apps.accounting.forms import AccountForm
        from apps.banking.models import BankAccount
        from apps.core.services import provision_company
        from apps.crm.models import Customer
        from apps.organizations.models import Branch, Company
        from apps.sales.forms import InvoiceForm

        resp = client.get(reverse("sales:invoice_create"))
        form = resp.context.get("form") if resp.context else None
        if not self.expect("new invoice form renders", form is not None, f"HTTP {resp.status_code}"):
            return
        field = form.fields["currency"]
        self.expect("invoice currency cannot be edited", field.disabled, f"disabled={field.disabled}")
        self.expect("invoice currency is the company book currency",
                    field.initial == self.company.currency, str(field.initial))
        self.expect("new invoice prefills today and net-30",
                    form.fields["invoice_date"].initial == self.today
                    and form.fields["due_date"].initial == self.today + datetime.timedelta(days=30),
                    f"{form.fields['invoice_date'].initial} / {form.fields['due_date'].initial}")

        off = AccountForm(company=self.company, data={
            "code": "9911", "name": "Dollar cash", "account_type": "ASSET",
            "classification": "CURRENT_ASSET", "currency": "USD",
        })
        self.expect("account in a foreign currency is rejected", not off.is_valid(),
                    str(dict(off.errors)))
        on = AccountForm(company=self.company, data={
            "code": "9912", "name": "Shilling cash", "account_type": "ASSET",
            "classification": "CURRENT_ASSET", "currency": "KES",
        })
        self.expect("account in the book currency is accepted", on.is_valid(), str(dict(on.errors)))

        # A second company, booking in its own currency, with its own rows.
        fx_user = User.objects.create(username=f"fx.{self.stamp}", email=f"fx.{self.stamp}@smoke.local")
        fx_user.set_password("smoke-test-pw")
        fx_user.save()
        fx = Company.objects.create(name=f"Kampala Traders {self.stamp}",
                                    currency="UGX", country="Uganda")
        provision_company(fx, fx_user)  # already files the creator as ADMIN
        fx_branch = Branch.objects.create(company=fx, name="Kampala Branch")
        fx_customer = Customer.objects.create(company=fx, name="Kampala Hardware")
        fx_bank = BankAccount.objects.create(
            company=fx, name="Kampala Bank",
            gl_account=fx.accounts.filter(code="1200").first())

        self.expect("off-book customer is not offered on this invoice form",
                    not form.fields["customer"].queryset.filter(pk=fx_customer.pk).exists(),
                    f"{form.fields['customer'].queryset.count()} customers offered")

        for label, url in [("branch", reverse("organizations:branch_detail", args=[fx_branch.pk])),
                           ("branch edit", reverse("organizations:branch_edit", args=[fx_branch.pk])),
                           ("company", reverse("organizations:company_detail", args=[fx.pk])),
                           ("bank account", reverse("banking:account_detail", args=[fx_bank.pk]))]:
            other = client.get(url)
            self.expect(f"another company's {label} is not readable",
                        other.status_code == 404, f"HTTP {other.status_code}")

        listed = client.get(reverse("organizations:company_list"))
        self.expect("company list offers only companies the user belongs to",
                    fx.pk not in [c.pk for c in listed.context["companies"]],
                    f"{listed.context['companies'].count()} companies listed")
        users = client.get(reverse("accounts:user_list"))
        self.expect("user list offers only this company's members",
                    fx_user.pk not in [u.pk for u in users.context["users"]],
                    f"{users.context['users'].count()} users listed")

        fx_client = Client()
        fx_client.force_login(fx_user, backend="django.contrib.auth.backends.ModelBackend")
        s = fx_client.session
        s["active_company_id"] = str(fx.id)
        s.save()
        fx_form = fx_client.get(reverse("sales:invoice_create")).context.get("form")
        if self.expect("second accountant can open the invoice form", fx_form is not None):
            self.expect("their invoice is pinned to their own book currency",
                        fx_form.fields["currency"].initial == "UGX"
                        and fx_form.fields["currency"].disabled,
                        str(fx_form.fields["currency"].initial))
            self.expect("their own branch is readable",
                        fx_client.get(reverse("organizations:branch_detail",
                                              args=[fx_branch.pk])).status_code == 200)

        # What the browser is actually handed, not only what the form object says.
        page = client.get(reverse("sales:invoice_create")).content.decode()
        after_currency = page.split('name="currency"')[1][:260] if 'name="currency"' in page else ""
        self.expect("currency reaches the browser non-editable",
                    "readonly" in after_currency or "disabled" in after_currency,
                    after_currency[:60].strip())
        dates = re.findall(r'type="date"[^>]*?value="([^"]*)"', page)
        self.expect("date inputs carry ISO values the browser will accept",
                    len(dates) >= 2 and all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in dates),
                    str(dates))
        for_labels = re.findall(r'<label[^>]*for="([^"]+)"', page)
        self.expect("every label points at a control of the same name",
                    for_labels and all(f'id="{lab}"' in page for lab in for_labels),
                    f"{len(for_labels)} labels")

        home = client.get(reverse("organizations:home"))
        self.expect("module home hides companies the user is not a member of",
                    fx.name not in home.content.decode(), f"HTTP {home.status_code}")

        cust = Customer.objects.filter(company=self.company, is_active=True).first()
        tamper = InvoiceForm(company=self.company, data={
            "reference": f"TAMPER{self.stamp}", "customer": cust.pk,
            "invoice_date": self.today.isoformat(), "due_date": self.today.isoformat(),
            "currency": "USD", "notes": "",
        })
        tamper.full_clean()
        self.expect("a posted foreign currency cannot override the book currency",
                    tamper.cleaned_data.get("currency") == self.company.currency,
                    f"cleaned={tamper.cleaned_data.get('currency')} errors={dict(tamper.errors)}")

    # ────────────────────────────── setup steps ──────────────────────────────

    def create_company(self, client, user, stamp):
        from apps.organizations.models import Company, CompanyMembership, FiscalYear

        name = f"Smoketest Traders Ltd {stamp}"
        resp = self.post(client, "company", reverse("organizations:company_create"), {
            "name": name,
            "legal_name": f"{name} Limited",
            "registration_number": f"PVT-{stamp}",
            "tax_id": f"A{stamp}Z",
            "vat_number": "VAT-001",
            "currency": "KES",
            "timezone": "Africa/Nairobi",
            "fiscal_year_start_month": "1",
            "industry": "Trading",
            "phone": "+254 700 000 000",
            "email": f"info@{stamp}.smoke.local",
            "website": "",
            "address_line1": "1 Commerce Street",
            "address_line2": "",
            "city": "Nairobi",
            "state_province": "Nairobi",
            "postal_code": "00100",
            "country": "Kenya",
        })
        # Messages only surface on the page the redirect lands on.
        landing = client.get(resp["Location"])
        flashes = self.flash(landing)
        self.expect(
            "company form confirms provisioning",
            any("chart of accounts" in m for m in flashes),
            str(flashes),
        )

        company = Company.objects.filter(name=name).first()
        if not self.expect("company created through the form", company is not None):
            raise SystemExit(1)
        self.expect(
            "accountant's active company resolved",
            getattr(landing.wsgi_request, "active_company", None) == company,
            str(getattr(landing.wsgi_request, "active_company", None)),
        )

        self.expect("chart of accounts provisioned",
                   company.accounts.filter(is_active=True).count() >= 50,
                   f"{company.accounts.count()} accounts")
        self.expect("VAT Input 2210 exists", company.accounts.filter(code="2210").exists())

        fy = FiscalYear.objects.filter(company=company).first()
        self.expect("fiscal year created", fy is not None)
        if fy:
            self.expect("12 monthly periods", fy.periods.count() == 12, f"{fy.periods.count()}")
            self.expect("all periods open", not fy.periods.exclude(status="OPEN").exists())
        self.expect("head office branch", company.branches.filter(is_headquarters=True).exists())
        self.expect("default warehouse", company.warehouses.filter(is_default=True).exists())
        self.expect("VAT 16% tax rate", company.tax_rates.filter(rate=D("16.0000")).exists())

        membership = CompanyMembership.objects.filter(company=company, user=user).first()
        if self.expect("creator got a company membership", membership is not None):
            membership.role = "ACCOUNTANT"
            membership.save(update_fields=["role"])
        return company

    def setup_master_data(self):
        from apps.accounting.models import Account, TaxRate
        from apps.crm.models import Customer, Supplier
        from apps.hr.models import Employee
        from apps.inventory.models import ProductCategory, UnitOfMeasure
        from apps.organizations.models import Warehouse

        company = self.company
        self.account = lambda code: Account.objects.get(company=company, code=code)
        self.warehouse = Warehouse.objects.filter(company=company, is_default=True).first()
        self.vat = TaxRate.objects.filter(company=company, code="VAT16").first()
        self.expect("default warehouse and VAT rate found", bool(self.warehouse and self.vat))

        self.uom, _ = UnitOfMeasure.objects.get_or_create(
            company=company, abbreviation="PCS", defaults={"name": "Pieces", "is_base": True}
        )
        self.category, _ = ProductCategory.objects.get_or_create(
            company=company, name="General", defaults={"is_active": True}
        )
        self.customer, _ = Customer.objects.get_or_create(company=company, name="Umoja Hardware")
        self.supplier, _ = Supplier.objects.get_or_create(
            company=company, name="Nairobi Wholesale Ltd"
        )
        self.employee, _ = Employee.objects.get_or_create(
            company=company, employee_number="EMP-001",
            defaults={"first_name": "Grace", "last_name": "Rider",
                      "date_hired": self.today, "job_title": "Field Technician"},
        )

    def create_product(self, client):
        from apps.inventory.models import Product

        self.post(client, "product", reverse("inventory:product_create"), {
            "sku": "SMK-001",
            "name": "Smoke Test Widget",
            "product_type": "GOODS",
            "category": self.category.pk,
            "unit_of_measure": self.uom.pk,
            "cost_price": "50.00",
            "selling_price": "100.00",
            "tax_rate": self.vat.pk,
            "reorder_level": "5",
            "reorder_quantity": "20",
            "track_inventory": "on",
            "valuation_method": "WAVG",
            "income_account": self.account("4200").pk,
            "cogs_account": self.account("5000").pk,
            "inventory_account": self.account("1400").pk,
            "barcode": "",
            "description": "Test item",
        })
        self.product = Product.objects.filter(company=self.company, sku="SMK-001").first()
        if self.expect("product created through the form", self.product is not None):
            self.expect(
                "product GL routing saved",
                self.product.income_account_id == self.account("4200").pk
                and self.product.cogs_account_id == self.account("5000").pk
                and self.product.inventory_account_id == self.account("1400").pk,
            )
        self.get_ok(client, "product edit form", reverse("inventory:product_edit", args=[self.product.pk]))

    # ────────────────────────────── document flows ───────────────────────────

    def post_opening_journal(self, client):
        from apps.accounting.models import JournalEntry

        data = {
            "reference": "JE-OPEN-1",
            "entry_type": "OPENING",
            "posting_date": str(self.today),
            "notes": "Opening balances",
        }
        data.update(formset("lines", [
            {"account": self.account("1100").pk, "description": "Cash float",
             "debit_amount": "20000", "credit_amount": "0"},
            {"account": self.account("1200").pk, "description": "Bank opening",
             "debit_amount": "80000", "credit_amount": "0"},
            {"account": self.account("3100").pk, "description": "Share capital",
             "debit_amount": "0", "credit_amount": "100000"},
        ], blank=BLANK_JOURNAL_LINE))
        self.post(client, "opening journal", reverse("accounting:journal_entry_create"), data)

        entry = JournalEntry.objects.filter(company=self.company, reference="JE-OPEN-1").first()
        if not self.expect("manual journal saved", entry is not None):
            return
        self.expect("journal saved as DRAFT", entry.status == "DRAFT", entry.status)
        self.get_ok(client, "journal edit page is read-only detail",
                    reverse("accounting:journal_entry_detail", args=[entry.pk]))

        self.post(client, "journal post",
                  reverse("accounting:journal_entry_post", args=[entry.pk]), {})
        entry.refresh_from_db()
        self.expect("journal posted through the Post button", entry.status == "POSTED", entry.status)
        self.expect("posting stamped by the accountant",
                   entry.posted_by_id == self.user.pk and entry.posted_at is not None,
                   f"by {entry.posted_by_id}, at {entry.posted_at}")

    def post_stock(self, client):
        from apps.inventory.models import StockLevel, StockMovement

        url = reverse("inventory:adjustment_create")
        self.post(client, "stock receipt", url, {
            "product": self.product.pk, "warehouse": self.warehouse.pk,
            "movement_type": "RECEIPT", "quantity": "100", "unit_cost": "50",
            "movement_date": str(self.today), "reference": "GRN-1", "notes": "Stock received",
        })
        self.post(client, "stock delivery", url, {
            "product": self.product.pk, "warehouse": self.warehouse.pk,
            "movement_type": "DELIVERY", "quantity": "10", "unit_cost": "50",
            "movement_date": str(self.today), "reference": "SOUT-1", "notes": "Stock issued",
        })

        level = StockLevel.objects.filter(product=self.product, warehouse=self.warehouse).first()
        self.expect("stock level is 90 units", level is not None and level.quantity == D("90.0000"),
                   f"{level.quantity if level else 'no level'}")
        self.expect("weighted average cost held at 50", level is not None and level.avg_cost == D("50.0000"),
                   f"{level.avg_cost if level else 'no level'}")

        # Issuing more than the shelf holds must be refused, not posted.
        resp = client.post(url, {
            "product": self.product.pk, "warehouse": self.warehouse.pk,
            "movement_type": "DELIVERY", "quantity": "500", "unit_cost": "50",
            "movement_date": str(self.today), "reference": "SOUT-2", "notes": "",
        })
        self.expect("oversized issue blocked", resp.status_code == 200)
        self.show_errors(resp)
        level.refresh_from_db()
        self.expect("blocked issue left stock untouched", level.quantity == D("90.0000"), str(level.quantity))
        self.expect("blocked issue wrote no movement row",
                    StockMovement.objects.filter(product=self.product, reference="SOUT-2").count() == 0,
                    "a failed movement left an orphan row and a hole in stock")

    def post_purchasing(self, client):
        from apps.procurement.models import Bill

        bill_data = {
            "reference": "BILL-SMOKE-1",
            "supplier": self.supplier.pk,
            "purchase_order": "",
            "supplier_reference": "SUP-INV-77",
            "bill_date": str(self.today),
            "due_date": str(self.today + datetime.timedelta(days=30)),
            "currency": "KES",
            "subtotal": "2000",
            "tax_amount": "320",
            "total_amount": "2320",
            "notes": "Goods bought on credit",
        }
        self.post(client, "bill", reverse("procurement:bill_create"), bill_data)
        bill = Bill.objects.filter(company=self.company, reference="BILL-SMOKE-1").first()
        if not self.expect("bill created through the form", bill is not None):
            return
        self.expect("bill posted to GL on save", bill.journal_entry_id is not None)
        self.expect("bill marked received", bill.status == "RECEIVED", bill.status)

        resp = client.post(reverse("procurement:bill_pay", args=[bill.pk]), {"amount": "5000"})
        bill.refresh_from_db()
        self.expect("overpayment blocked", bill.paid_amount == Z, f"paid {bill.paid_amount}")
        self.expect_flash(client, resp, "exceeds outstanding", "overpayment explained")

        resp = self.post(client, "bill payment", reverse("procurement:bill_pay", args=[bill.pk]),
                         {"amount": "2320"})
        bill.refresh_from_db()
        self.expect("bill fully paid", bill.status == "PAID" and bill.outstanding_amount == Z,
                   f"{bill.status} / {bill.outstanding_amount}")
        self.expect_flash(client, resp, "GL entry", "payment surfaced a GL reference")

        bad = dict(bill_data, reference="BILL-SMOKE-BAD",
                   subtotal="1000", tax_amount="100", total_amount="999")
        self.reject(client, "bill whose total misses subtotal+tax",
                    reverse("procurement:bill_create"), bad)
        self.expect("mismatched bill not saved",
                   not Bill.objects.filter(company=self.company, reference="BILL-SMOKE-BAD").exists())

    def post_sales_order(self, client):
        from apps.accounts.models import User
        from apps.crm.models import Customer
        from apps.organizations.models import Company
        from apps.sales.models import SalesOrder

        posted_before = self.posted_lines().count()
        branch = self.company.branches.first()

        header = {
            "reference": "SO-SMOKE-1",
            "customer": self.customer.pk,
            "order_date": str(self.today),
            "delivery_date": str(self.today + datetime.timedelta(days=7)),
            "warehouse": self.warehouse.pk,
            "sales_rep": self.user.pk,
            "currency": "KES",
            "discount_percent": "5",
            "branch": branch.pk if branch else "",
            "notes": "Smoke order",
        }
        lines = formset("lines", [
            {"product": self.product.pk, "description": "Widgets", "quantity": "10",
             "unit_price": "100", "discount_percent": "0", "tax_rate": self.vat.pk},
            {"product": self.product.pk, "description": "Spare drum", "quantity": "1",
             "unit_price": "200", "discount_percent": "0", "tax_rate": ""},
        ], blank=BLANK_INVOICE_LINE)
        self.post(client, "sales order", reverse("sales:order_create"), merge(header, lines))

        order = SalesOrder.objects.filter(company=self.company, reference="SO-SMOKE-1").first()
        if not self.expect("sales order created through the form", order is not None):
            return
        self.expect("both order lines saved", order.lines.count() == 2, str(order.lines.count()))
        self.expect("order subtotal from the lines", order.subtotal == D("1200.00"), str(order.subtotal))
        self.expect("order tax from the lines", order.tax_amount == D("160.00"), str(order.tax_amount))
        self.expect("header discount taken once, not per line",
                   order.total_amount == D("1300.00"), str(order.total_amount))
        self.expect("order starts as DRAFT", order.status == "DRAFT", order.status)
        self.expect("a sales order moves no money",
                   self.posted_lines().count() == posted_before,
                   f"{posted_before} posted lines before, {self.posted_lines().count()} after")
        self.get_ok(client, "order detail", reverse("sales:order_detail", args=[order.pk]))
        self.get_ok(client, "order list", reverse("sales:order_list"))

        self.reject(client, "line-less sales order", reverse("sales:order_create"),
                    merge(header, formset("lines", [], blank=BLANK_INVOICE_LINE, blanks=3)))

        # Tenant isolation: another company's masters must not be selectable and
        # its documents must not open on this session.
        other_user = User.objects.create(
            username=f"other.{self.stamp}", email=f"other.{self.stamp}@smoke.local")
        other = Company.objects.create(name=f"Shadow Traders {self.stamp}", currency="KES")
        from apps.core.services import provision_company
        provision_company(other, other_user)
        foreign_customer = Customer.objects.create(company=other, name="Foreign Trader")
        foreign_order = SalesOrder.objects.create(
            company=other, customer=foreign_customer, reference="SO-FOREIGN",
            order_date=self.today)

        resp = client.get(reverse("sales:order_create"))
        offered = resp.context["form"].fields["customer"].queryset
        self.expect("order form offers only this company's customers",
                   list(offered.filter(pk__in=[foreign_customer.pk])) == []
                   and offered.filter(pk=self.customer.pk).exists(),
                   f"{offered.count()} customers offered")

        foreign = client.get(reverse("sales:order_detail", args=[foreign_order.pk]))
        self.expect("another company's order is not readable", foreign.status_code == 404,
                   f"HTTP {foreign.status_code}")

    def post_sales(self, client):
        from apps.sales.models import Invoice

        header = {
            "reference": "INV-SMOKE-1",
            "customer": self.customer.pk,
            "invoice_date": str(self.today),
            "due_date": str(self.today + datetime.timedelta(days=30)),
            "currency": "KES",
            "notes": "Smoke invoice",
        }
        lines = formset("lines", [
            {"product": self.product.pk, "description": "Widgets", "quantity": "5",
             "unit_price": "100", "discount_percent": "0", "tax_rate": self.vat.pk},
            {"product": "", "description": "Freight", "quantity": "1",
             "unit_price": "300", "discount_percent": "0", "tax_rate": ""},
        ], blank=BLANK_INVOICE_LINE)
        self.post(client, "invoice", reverse("sales:invoice_create"), merge(header, lines))

        invoice = Invoice.objects.filter(company=self.company, reference="INV-SMOKE-1").first()
        if not self.expect("invoice created through the form", invoice is not None):
            return
        self.expect("both invoice lines saved", invoice.lines.count() == 2, f"{invoice.lines.count()}")
        self.expect("subtotal is the tax-exclusive total", invoice.subtotal == D("800.00"), str(invoice.subtotal))
        self.expect("tax totalled from the lines", invoice.tax_amount == D("80.00"), str(invoice.tax_amount))
        self.expect("gross total", invoice.total_amount == D("880.00"), str(invoice.total_amount))
        self.expect("invoice posted to GL", invoice.journal_entry_id is not None)
        self.expect("unpaid invoice is not marked paid",
                   invoice.status in ("SENT", "PARTIAL"), invoice.status)
        self.get_ok(client, "invoice edit form", reverse("sales:invoice_edit", args=[invoice.pk]))

        self.post(client, "customer payment", reverse("sales:payment_create"), {
            "reference": "PAY-SMOKE-1",
            "customer": self.customer.pk,
            "payment_date": str(self.today),
            "amount": "500",
            "payment_method": "MPESA",
            "transaction_reference": "TL0001",
            "notes": "Part payment",
        })
        invoice.refresh_from_db()
        self.expect("payment allocated against the open invoice",
                   invoice.paid_amount == D("500.00"), str(invoice.paid_amount))
        self.expect("invoice now PARTIAL", invoice.status == "PARTIAL", invoice.status)
        self.expect("outstanding recalculated", invoice.outstanding_amount == D("380.00"),
                   str(invoice.outstanding_amount))

        blank = Invoice.objects.create(
            company=self.company, customer=self.customer, reference="INV-BLANK",
            invoice_date=self.today, due_date=self.today,
        )
        self.expect("a line-less draft invoice is not marked paid",
                    blank.status == "DRAFT" and blank.outstanding_amount == Z, blank.status)

    def post_expense_claim(self, client):
        from apps.expenses.models import ExpenseCategory, ExpenseClaim

        self.post(client, "expense category (mapped)", reverse("expenses:category_create"), {
            "name": "Travel", "gl_account": self.account("6400").pk,
        })
        self.post(client, "expense category (unmapped)", reverse("expenses:category_create"), {
            "name": "Miscellaneous", "gl_account": "",
        })
        travel = ExpenseCategory.objects.get(company=self.company, name="Travel")
        misc = ExpenseCategory.objects.get(company=self.company, name="Miscellaneous")
        self.expect("unmapped category left blank so the default applies",
                   travel.gl_account_id == self.account("6400").pk and misc.gl_account_id is None)

        self.post(client, "expense claim", reverse("expenses:claim_create"), {
            "reference": "EXP-SMOKE-1",
            "employee": self.employee.pk,
            "claim_date": str(self.today),
            "notes": "Fuel and taxi",
        })
        claim = ExpenseClaim.objects.filter(company=self.company, reference="EXP-SMOKE-1").first()
        if not self.expect("claim created", claim is not None):
            return

        resp = client.post(reverse("expenses:claim_submit", args=[claim.pk]), {})
        claim.refresh_from_db()
        self.expect("line-less claim cannot be submitted", claim.status == "DRAFT", claim.status)
        self.expect_flash(client, resp, "at least one line", "empty-claim reason surfaced")

        for category, description, amount in ((travel, "Fuel", "1200"), (misc, "Taxi", "300")):
            self.post(client, "claim line", reverse("expenses:claim_add_line", args=[claim.pk]), {
                "category": category.pk, "description": description,
                "expense_date": str(self.today), "amount": amount,
            })
        claim.refresh_from_db()
        self.expect("claim total built from lines", claim.total_amount == D("1500.00"),
                   str(claim.total_amount))

        resp = client.post(reverse("expenses:claim_add_line", args=[claim.pk]), {
            "category": misc.pk, "description": "Nothing",
            "expense_date": str(self.today), "amount": "0",
        })
        claim.refresh_from_db()
        self.expect("zero-value claim line rejected", claim.lines.count() == 2, f"{claim.lines.count()} lines")
        self.expect_flash(client, resp, "Could not add line", "bad line explained")

        self.post(client, "claim submit", reverse("expenses:claim_submit", args=[claim.pk]), {})
        claim.refresh_from_db()
        self.expect("claim submitted", claim.status == "SUBMITTED", claim.status)

        client.post(reverse("expenses:claim_pay", args=[claim.pk]), {})
        claim.refresh_from_db()
        self.expect("unapproved claim cannot be paid", claim.journal_entry_id is None)

        self.post(client, "claim approve", reverse("expenses:claim_approve", args=[claim.pk]), {})
        claim.refresh_from_db()
        self.expect("claim approved", claim.status == "APPROVED", claim.status)

        resp = self.post(client, "claim pay", reverse("expenses:claim_pay", args=[claim.pk]),
                         {"bank_account": ""})
        claim.refresh_from_db()
        self.expect("claim paid and posted",
                   claim.status == "PAID" and claim.journal_entry_id is not None, claim.status)
        self.expect_flash(client, resp, "posted to GL", "payment surfaced")

    # ────────────────────────────── verification ─────────────────────────────

    def posted_lines(self):
        from apps.accounting.models import JournalEntryLine
        return JournalEntryLine.objects.filter(
            journal_entry__company=self.company, journal_entry__status="POSTED"
        ).select_related("account", "journal_entry")

    def verify_entries(self):
        from apps.accounting.models import JournalEntry

        entries = list(JournalEntry.objects.filter(company=self.company).prefetch_related("lines"))
        self.expect("one journal entry per posting", len(entries) == 8, f"{len(entries)} entries")
        drafts = [e.reference for e in entries if e.status != "POSTED"]
        self.expect("every entry is POSTED", not drafts, f"draft: {drafts}")

        unbalanced, thin, double_sided, empty, unstamped = [], [], [], [], []
        for entry in entries:
            lines = list(entry.lines.all())
            if sum((l.debit_amount for l in lines), Z) != sum((l.credit_amount for l in lines), Z):
                unbalanced.append(entry.reference)
            if len(lines) < 2:
                thin.append(entry.reference)
            for line in lines:
                if line.debit_amount > 0 and line.credit_amount > 0:
                    double_sided.append(f"{entry.reference}/{line.account.code}")
                if line.debit_amount == 0 and line.credit_amount == 0:
                    empty.append(f"{entry.reference}/{line.account.code}")
            if entry.posted_at is None:
                unstamped.append(entry.reference)

        self.expect("every entry balances Dr = Cr", not unbalanced, str(unbalanced))
        self.expect("every entry has at least two lines", not thin, str(thin))
        self.expect("no line is both a debit and a credit", not double_sided, str(double_sided))
        self.expect("no zero-value line was written", not empty, str(empty))
        self.expect("no negative amounts in the ledger",
                   not self.posted_lines().filter(
                       debit_amount__lt=Z).exists() and not self.posted_lines().filter(
                       credit_amount__lt=Z).exists())
        self.expect("every entry stamped on post", not unstamped, str(unstamped))

        types = sorted({e.entry_type for e in entries})
        self.expect("document journals use their own entry types",
                   set(types) <= {"OPENING", "STOCK_ADJUSTMENT", "BILL", "PAYMENT", "INVOICE", "MANUAL"},
                   str(types))

    def verify_unbalanced_rejected(self, client):
        """The balance rule must stop a lopsided entry from reaching the ledger."""
        from apps.accounting.models import JournalEntry

        data = {
            "reference": "JE-BAD-1", "entry_type": "MANUAL",
            "posting_date": str(self.today), "notes": "Deliberately unbalanced",
        }
        data.update(formset("lines", [
            {"account": self.account("1100").pk, "description": "Dr only",
             "debit_amount": "100", "credit_amount": "0"},
            {"account": self.account("6000").pk, "description": "Cr short",
             "debit_amount": "0", "credit_amount": "50"},
        ], blank=BLANK_JOURNAL_LINE))
        self.post(client, "unbalanced journal save", reverse("accounting:journal_entry_create"), data)
        entry = JournalEntry.objects.filter(company=self.company, reference="JE-BAD-1").first()
        if not self.expect("unbalanced entry saved as DRAFT",
                          entry is not None and entry.status == "DRAFT"):
            return

        resp = client.post(reverse("accounting:journal_entry_post", args=[entry.pk]), {})
        entry.refresh_from_db()
        self.expect("Post refuses an unbalanced entry", entry.status == "DRAFT", entry.status)
        self.expect_flash(client, resp, "not balanced", "balance rule explained to the accountant")
        self.expect("rejected entry stays out of the ledger",
                   not self.posted_lines().filter(journal_entry=entry).exists())

        both = {
            "reference": "JE-BAD-2", "entry_type": "MANUAL",
            "posting_date": str(self.today), "notes": "",
        }
        both.update(formset("lines", [
            {"account": self.account("1100").pk, "description": "x",
             "debit_amount": "100", "credit_amount": "100"},
            {"account": self.account("6000").pk, "description": "y",
             "debit_amount": "0", "credit_amount": "100"},
        ], blank=BLANK_JOURNAL_LINE))
        self.reject(client, "line that is both debit and credit",
                    reverse("accounting:journal_entry_create"), both)
        self.expect("double-sided entry not saved",
                   not JournalEntry.objects.filter(company=self.company, reference="JE-BAD-2").exists())

        too_thin = {
            "reference": "JE-BAD-3", "entry_type": "MANUAL",
            "posting_date": str(self.today), "notes": "",
        }
        too_thin.update(formset("lines", [
            {"account": self.account("1100").pk, "description": "only line",
             "debit_amount": "10", "credit_amount": "0"},
        ], blank=BLANK_JOURNAL_LINE, blanks=2))
        self.reject(client, "single-line journal",
                    reverse("accounting:journal_entry_create"), too_thin)
        self.expect("single-line journal not saved",
                   not JournalEntry.objects.filter(company=self.company, reference="JE-BAD-3").exists())

    def verify_trial_balance(self, client):
        totals = self.posted_lines().aggregate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        dr, cr = totals["dr"] or Z, totals["cr"] or Z
        self.expect("ledger debits equal credits", dr == cr, f"Dr {dr} / Cr {cr}")
        self.expect("ledger matches the expected control figure",
                   dr == EXPECTED_TOTAL, f"Dr {dr} vs {EXPECTED_TOTAL}")

        resp = client.get(reverse("accounting:trial_balance"))
        ctx = getattr(resp, "context", None)
        if self.expect("trial balance renders", resp.status_code == 200 and ctx is not None):
            self.expect("trial balance reports itself balanced", ctx["is_balanced"] is True)
            self.expect("trial balance totals match the ledger",
                       ctx["total_debit"] == dr and ctx["total_credit"] == cr,
                       f"{ctx['total_debit']} / {ctx['total_credit']}")

    def verify_subledgers(self):
        from apps.accounting.models import Account
        from apps.inventory.models import StockMovement
        from apps.procurement.models import Bill
        from apps.sales.models import Invoice

        ar = Account.objects.get(company=self.company, code="1300")
        ap = Account.objects.get(company=self.company, code="2100")

        invoiced = sum((i.outstanding_amount for i in
                        Invoice.objects.filter(company=self.company, journal_entry__isnull=False)), Z)
        self.expect("AR control ties to invoice outstanding",
                   ar.current_balance == invoiced, f"GL {ar.current_balance} vs books {invoiced}")
        self.expect("AR balance is the debit-normal view", ar.current_balance == D("380.00"),
                   str(ar.current_balance))

        bills = sum((b.outstanding_amount for b in
                     Bill.objects.filter(company=self.company, journal_entry__isnull=False)), Z)
        grns = sum((m.total_cost for m in StockMovement.objects.filter(
            product__company=self.company, movement_type__in=INBOUND_STOCK)), Z)
        self.expect("AP control ties to bills plus goods receipts",
                   ap.current_balance == bills + grns,
                   f"GL {ap.current_balance} vs bills {bills} + GRN {grns}")
        self.note("Goods receipts credit 2100 directly, so billing the same goods again would "
                  "double-count AP. GRNs should post Dr 1400 / Cr goods-in-transit instead.")

        self.expect("customer statement agrees with the ledger",
                   self.customer.outstanding_balance == D("380.00"),
                   str(self.customer.outstanding_balance))
        self.expect("supplier shows no balance once paid",
                   self.supplier.bills.filter(is_active=True).aggregate(
                       t=Sum("outstanding_amount"))["t"] in (None, Z))

        gl = self.posted_lines().filter(account__code="1300")
        allocated = sum((a.allocated_amount for a in
                         self.customer.payments.first().allocations.all()), Z)
        self.expect("payment allocation equals the AR credit cleared",
                   allocated == (gl.aggregate(t=Sum("credit_amount"))["t"] or Z),
                   f"allocated {allocated}")

    def verify_account_routing(self):
        agg = {}
        for row in self.posted_lines().values("account__code").annotate(
            dr=Sum("debit_amount"), cr=Sum("credit_amount")
        ):
            agg[row["account__code"]] = (row["dr"] or Z, row["cr"] or Z)

        wrong = []
        for code, (dr, cr) in EXPECTED.items():
            got = agg.get(code)
            if got != (D(dr), D(cr)):
                wrong.append(f"{code}: expected {dr}/{cr}, got {got if got else 'no lines'}")
        self.expect("every account carries exactly the expected amounts", not wrong, "; ".join(wrong))

        extra = sorted(set(agg) - set(EXPECTED))
        self.expect("no posting leaked onto an unexpected account", not extra, str(extra))

        self.expect("product income account beat the chart default",
                   agg.get("4200", (Z, Z))[1] == D("500.00"), str(agg.get("4200")))
        self.expect("unmapped line used Sales Revenue 4100",
                   agg.get("4100", (Z, Z))[1] == D("300.00"), str(agg.get("4100")))
        self.expect("output VAT to 2200 and input VAT to 2210",
                   agg.get("2200", (Z, Z))[1] == D("80.00")
                   and agg.get("2210", (Z, Z))[0] == D("320.00"))
        self.expect("claim used the category account and the default expense account",
                   agg.get("6400", (Z, Z))[0] == D("1200.00")
                   and agg.get("6000", (Z, Z))[0] == D("300.00"))

        vat_out, vat_in = agg["2200"][1], agg["2210"][0]
        self.note(f"VAT position: output {vat_out} less input {vat_in} = net payable "
                  f"{vat_out - vat_in} on 2200/2210.")

    def verify_reports(self, client):
        from apps.accounting.models import Account

        self.get_ok(client, "journal entry list", reverse("accounting:journal_entry_list"))
        self.get_ok(client, "chart of accounts", reverse("accounting:account_list"))
        self.get_ok(client, "general ledger index", reverse("accounting:general_ledger"))
        self.get_ok(client, "fiscal year list", reverse("accounting:fiscal_year_list"))
        self.get_ok(client, "AR aging", reverse("sales:ar_aging"))
        self.get_ok(client, "AP aging", reverse("procurement:ap_aging"))
        self.get_ok(client, "invoice list", reverse("sales:invoice_list"))
        self.get_ok(client, "bill list", reverse("procurement:bill_list"))
        self.get_ok(client, "stock movements", reverse("inventory:movement_list"))
        self.get_ok(client, "expense claims", reverse("expenses:claim_list"))
        self.get_ok(client, "new invoice form", reverse("sales:invoice_create"))
        self.get_ok(client, "new bill form", reverse("procurement:bill_create"))
        self.get_ok(client, "new journal form", reverse("accounting:journal_entry_create"))
        self.get_ok(client, "new product form", reverse("inventory:product_create"))
        self.get_ok(client, "new expense claim form", reverse("expenses:claim_create"))
        self.get_ok(client, "new payment form", reverse("sales:payment_create"))

        ar = Account.objects.get(company=self.company, code="1300")
        resp = self.get_ok(client, "AR account ledger",
                           reverse("accounting:account_ledger", args=[ar.pk]))
        ctx = getattr(resp, "context", None)
        if ctx is not None:
            self.expect("ledger page shows the closing balance",
                       ctx.get("closing_balance") == D("380.00"), str(ctx.get("closing_balance")))

    def verify_recommended_accounts(self):
        hit = {}
        for row in self.posted_lines().values("account__code").annotate(
            dr=Sum("debit_amount"), cr=Sum("credit_amount")
        ):
            hit[row["account__code"]] = (row["dr"] or Z, row["cr"] or Z)

        missing = [f"{code} {label}" for code, label in RECOMMENDED
                   if hit.get(code, (Z, Z)) == (Z, Z)]
        self.expect("all 10 recommended accounts were transacted", not missing, "; ".join(missing))

        for code, label in RECOMMENDED:
            dr, cr = hit.get(code, (Z, Z))
            self.stdout.write(f"       {code} {label:<22} Dr {dr:>10} Cr {cr:>10}")

        from apps.accounting.models import Account
        parents = set(Account.objects.filter(
            company=self.company, parent__isnull=False
        ).values_list("parent__code", flat=True))
        overlapped = sorted(code for code in hit if code in parents)
        if overlapped:
            self.note(f"Postings land on parent accounts {overlapped}; they have child accounts, "
                      "so any report that rolls children into parents would double-count.")
