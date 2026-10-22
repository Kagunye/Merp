"""Accountant UAT — the full accounting cycle through the screens.

A business owner signs up and creates a company. They add an accountant,
a finance manager, a sales rep and an employee through People & access.
Everyone then works through the real pages and forms (CSRF enforced),
signed in with their own role — no superusers and no direct database
posting for the steps under test.

After each step the ledger is compared with what correct books would
show: which accounts a transaction hit, by how much, and on what date.
When a step blocks the accountant, the problem is recorded as a finding,
a clearly labelled workaround is applied, and the test carries on so the
rest of the cycle is still exercised.

    python manage.py test_uat_accountant
    python manage.py test_uat_accountant --report path/to/report.md
"""
from __future__ import annotations

import calendar
import re
import secrets
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum
from django.test import Client, override_settings
from django.test.utils import setup_test_environment, teardown_test_environment
from django.urls import reverse

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, GREEN, YELLOW, CYAN = "\033[31m", "\033[32m", "\033[33m", "\033[36m"
D = Decimal

@dataclass
class Finding:
    severity: str   # CRITICAL / HIGH / MEDIUM
    area: str
    title: str
    detail: str = ""


class _Rollback(Exception):
    pass


class Command(BaseCommand):
    help = "UAT of the full accounting cycle, signed in as an accountant."

    def add_arguments(self, parser):
        parser.add_argument("--report", default="", help="Write a Markdown report to this path.")
        parser.add_argument("--keep", action="store_true", help="Persist the data instead of rolling back.")

    # ------------------------------------------------------------------ run

    def handle(self, *args, **opts):
        self.findings: list[Finding] = []
        self.passed = 0
        self.tag = secrets.token_hex(3).upper()
        setup_test_environment()  # lets us read each page's template context
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
        if self.findings:
            raise SystemExit(1)

    def _run(self):
        self.today = date.today()
        self.fy_start = date(self.today.year, 1, 1)

        steps = [
            ("1. Owner signs up, creates the company, adds the team", self.step_onboarding),
            ("2. Accountant signs in", self.step_accountant_login),
            ("3. Chart of accounts", self.step_chart),
            ("4. VAT set-up", self.step_vat),
            ("5. Fiscal year and periods", self.step_fiscal_year),
            ("6. Opening balances", self.step_opening_balances),
            ("7. Manual journals: post, reject unbalanced, reverse", self.step_journals),
            ("8. Sales invoice (sales rep) posted to the ledger", self.step_sales_invoice),
            ("9. Customer receipt", self.step_receipt),
            ("10. Cancelling a posted invoice", self.step_cancel_invoice),
            ("11. Editing a posted invoice", self.step_edit_posted_invoice),
            ("12. Supplier bill", self.step_bill),
            ("13. Paying the supplier", self.step_bill_payment),
            ("14. Employee expense claim", self.step_expense_claim),
            ("15. Reports", self.step_reports),
            ("16. Documents", self.step_documents),
            ("17. What an accountant must not be able to do", self.step_segregation),
            ("18. Month-end: closed periods", self.step_period_close),
            ("19. Final ledger proof", self.step_final_proof),
        ]
        for title, fn in steps:
            self._h(title)
            if fn == self.step_onboarding:
                fn()  # nothing else can run without the company
                continue
            try:
                fn()
            except Exception as exc:
                import traceback
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                self._find("HIGH", "UAT step failed", f"{title}: {type(exc).__name__}: {str(exc)[:120]}",
                           f"{frame.filename.split('/Merp/')[-1]}:{frame.lineno}")
                self.stdout.write(f"  {RED}✗{RESET} step stopped: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ helpers

    def _client(self):
        return Client(enforce_csrf_checks=True)

    def _token(self, c):
        if "csrftoken" not in c.cookies:
            c.get(reverse("accounts:login"))
        return c.cookies["csrftoken"].value

    def _post(self, c, url, data=None, **kw):
        data = dict(data or {})
        data["csrfmiddlewaretoken"] = self._token(c)
        return self._safely(lambda: c.post(url, data, **kw), url)

    def _get(self, c, url):
        return self._safely(lambda: c.get(url), url)

    def _safely(self, call, url):
        """Run one request in its own savepoint; a crash becomes a finding, not an abort."""
        try:
            with transaction.atomic():
                return call()
        except Exception as exc:
            self._find("HIGH", "Crash", f"{url.split('?')[0]} crashes", f"{type(exc).__name__}: {str(exc)[:140]}")
            self.stdout.write(f"  {RED}✗{RESET} {url} crashed: {type(exc).__name__}")

            class _Crashed:
                status_code = 500
                content = b""
                context = None
            return _Crashed()

    def _login(self, email, password):
        c = self._client()
        c.get(reverse("accounts:login"))
        r = self._post(c, reverse("accounts:login"), {"email": email, "password": password})
        self._check(r.status_code == 302, f"{email.split('@')[0]} signs in")
        return c

    def _acc(self, code):
        from apps.accounting.models import Account
        return Account.objects.filter(company=self.company, code=code).first()

    def _bal(self, code):
        """Natural (normal-side) balance of an account across posted entries."""
        from apps.accounting.models import JournalEntryLine
        a = self._acc(code)
        if a is None:
            return None
        agg = JournalEntryLine.objects.filter(account=a, journal_entry__status="POSTED").aggregate(
            dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        dr, cr = agg["dr"] or D(0), agg["cr"] or D(0)
        return dr - cr if a.normal_balance == "DEBIT" else cr - dr

    def _expect_bal(self, code, expected, why):
        got = self._bal(code)
        ok = got is not None and abs(got - D(expected)) < D("0.01")
        self._check(ok, f"{code} {self._acc(code).name if self._acc(code) else ''} = {D(expected):,.2f} ({why})",
                    finding=None if ok else ("HIGH", "Ledger", f"{code} balance is {got} after {why}, expected {D(expected):,.2f}"))
        return ok

    def _tb_balanced(self, where):
        from apps.accounting.models import JournalEntryLine
        agg = JournalEntryLine.objects.filter(journal_entry__company=self.company,
                                              journal_entry__status="POSTED").aggregate(
            dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        dr, cr = agg["dr"] or D(0), agg["cr"] or D(0)
        self._check(abs(dr - cr) < D("0.01"), f"ledger balances {where} (Dr {dr:,.2f} = Cr {cr:,.2f})",
                    finding=("CRITICAL", "Ledger", f"Ledger out of balance {where}: Dr {dr} vs Cr {cr}"))

    def _check(self, ok, label, finding=None):
        if ok:
            self.passed += 1
            self.stdout.write(f"  {GREEN}✓{RESET} {label}")
        else:
            self.stdout.write(f"  {RED}✗{RESET} {label}")
            self._find(*(finding or ("HIGH", "Failed check", label)))
        return ok

    def _find(self, severity, area, title, detail=""):
        self.findings.append(Finding(severity, area, title, detail))

    def _note(self, msg):
        self.stdout.write(f"  {YELLOW}↳{RESET} {msg}")

    def _h(self, text):
        self.stdout.write(f"\n{BOLD}{CYAN}{text}{RESET}")

    # -------------------------------------------------------------- steps

    def step_onboarding(self):
        from apps.organizations.models import Company, CompanyMembership
        owner_email = f"owner.{self.tag.lower()}@uat-acct.invalid"
        self.pw = {}
        self.pw["owner"] = secrets.token_urlsafe(14)
        c = self._client()
        c.get(reverse("accounts:register"))
        self._post(c, reverse("accounts:register"), {
            "first_name": "Neema", "last_name": "Achieng", "email": owner_email,
            "phone": "+254700100200", "password": self.pw["owner"],
            "password_confirm": self.pw["owner"], "terms_accepted": "on"})
        name = f"Uzima Traders Ltd {self.tag}"
        self._post(c, reverse("organizations:company_create"), {
            "name": name, "legal_name": name, "registration_number": "PVT-UAT", "tax_id": "P051234000Z",
            "vat_number": "0123456X", "currency": "KES", "timezone": "Africa/Nairobi",
            "fiscal_year_start_month": "1", "industry": "Trading", "phone": "", "email": "",
            "website": "", "address_line1": "Moi Avenue", "address_line2": "", "city": "Nairobi",
            "state_province": "", "postal_code": "", "country": "Kenya"})
        self.company = Company.objects.filter(name=name).first()
        self._check(self.company is not None, f"owner created {name}",
                    finding=("CRITICAL", "Onboarding", "Owner could not create the company"))
        if not self.company:
            raise SystemExit(2)
        self._check(CompanyMembership.objects.filter(company=self.company, user__email=owner_email,
                                                     role="ADMIN").exists(),
                    "owner is the company administrator")
        self.owner = c
        team = [("accountant", "Wanjiru", "Kamau", "ACCOUNTANT"),
                ("finance", "Otieno", "Odhiambo", "FINANCE_MANAGER"),
                ("sales", "Baraka", "Mwangi", "SALES_REP"),
                ("staff", "Akinyi", "Njeri", "EMPLOYEE")]
        self.email = {}
        for key, first, last, role in team:
            email = f"{key}.{self.tag.lower()}@uat-acct.invalid"
            self.pw[key] = secrets.token_urlsafe(14)
            self.email[key] = email
            self._post(c, reverse("accounts:user_create"), {
                "first_name": first, "last_name": last, "email": email, "username": email.split("@")[0],
                "phone": "", "job_title": role.replace("_", " ").title(),
                "password": self.pw[key], "company_role": role})
            self._check(CompanyMembership.objects.filter(company=self.company, user__email=email,
                                                         role=role).exists(),
                        f"owner added {first} {last} as {role.replace('_', ' ').title()}",
                        finding=("HIGH", "People", f"Owner could not add a {role} through People & access"))

    def step_accountant_login(self):
        self.acct = self._login(self.email["accountant"], self.pw["accountant"])
        r = self._get(self.acct, reverse("dashboard:index"))
        body = r.content.decode()
        self._check(r.status_code == 200 and "Cash on hand" in body, "dashboard shows the finance figures")
        self._check('href="/accounting/"' in body and 'href="/payroll/"' not in body,
                    "menu shows Accounting but not Payroll")
        self._check("Accountant" in body, "top bar shows the Accountant role")

    def step_chart(self):
        from apps.accounting.models import Account, TaxRate
        r = self._get(self.acct, reverse("accounting:account_list"))
        offers = "Load standard chart" in r.content.decode()
        self._check(offers, "an empty chart offers the standard Kenyan chart",
                    finding=("HIGH", "Chart of accounts", "A new company starts with an empty chart of accounts and "
                                                          "there is no way to load a standard one"))
        self._post(self.acct, reverse("accounting:account_load_standard"))
        n = Account.objects.filter(company=self.company).count()
        self._check(n >= 50, f"standard chart loaded ({n} accounts)",
                    finding=("HIGH", "Chart of accounts", "Loading the standard chart failed"))
        self._check(TaxRate.objects.filter(company=self.company, code="VAT16", rate=D("16")).exists(),
                    "16% VAT rate created with the chart")
        missing = [c for c in ("1100", "1200", "1300", "1400", "2100", "2200", "2210", "2610",
                               "3100", "4100", "6000", "6200", "6300", "6400") if not self._acc(c)]
        self._check(not missing, "chart has the accounts every posting needs",
                    finding=("HIGH", "Chart of accounts", f"Standard chart is missing {missing}"))
        self._post(self.acct, reverse("accounting:account_create"), {
            "code": "1220", "name": "M-Pesa Paybill", "account_type": "BANK", "parent": "",
            "description": "", "currency": ""})
        self._check(self._acc("1220") is not None, "accountant added an M-Pesa Paybill account through the form")
        r = self._post(self.acct, reverse("accounting:account_create"), {
            "code": "1100", "name": "Duplicate", "account_type": "CASH", "parent": "",
            "description": "", "currency": ""})
        self._check(r.status_code == 200 and Account.objects.filter(company=self.company, code="1100").count() == 1
                    and b"already used" in r.content,
                    "a duplicate account code is refused with a message",
                    finding=("MEDIUM", "Chart of accounts", "Duplicate account code accepted or crashed",
                             f"HTTP {r.status_code}"))

    def step_vat(self):
        from apps.accounting.models import TaxRate
        r = self._get(self.acct, reverse("accounting:tax_rate_list"))
        self._check(r.status_code == 200 and "VAT16" in r.content.decode(), "tax rate screen lists VAT 16%",
                    finding=("HIGH", "VAT", "There is no screen to manage tax rates"))
        self._post(self.acct, reverse("accounting:tax_rate_create"), {
            "name": "VAT 0% (zero-rated)", "code": "VAT0", "rate": "0",
            "account": str(self._acc("2200").pk), "effective_from": self.fy_start.isoformat(), "effective_to": ""})
        self._check(TaxRate.objects.filter(company=self.company, code="VAT0").exists(),
                    "accountant added a zero-rated VAT code on the tax rate screen",
                    finding=("HIGH", "VAT", "Could not create a tax rate on the tax rate screen"))
        self.vat = TaxRate.objects.get(company=self.company, code="VAT16")

    def step_fiscal_year(self):
        from apps.organizations.models import AccountingPeriod, FiscalYear
        self._post(self.acct, reverse("accounting:fiscal_year_create"), {
            "name": f"FY{self.today.year}", "start_date": self.fy_start.isoformat(),
            "end_date": date(self.today.year, 12, 31).isoformat(), "is_current": "on"})
        self.fy = FiscalYear.objects.filter(company=self.company, name=f"FY{self.today.year}").first()
        self._check(self.fy is not None, f"fiscal year FY{self.today.year} created")
        n = AccountingPeriod.objects.filter(fiscal_year=self.fy).count() if self.fy else 0
        self._check(n == 12, f"12 monthly periods generated (got {n})")

    def step_opening_balances(self):
        from apps.accounting.models import JournalEntry
        url = reverse("accounting:opening_balances")
        before = JournalEntry.objects.filter(company=self.company, entry_type="OPENING").count()
        # Unbalanced first: must be refused.
        self._post(self.acct, url, {"posting_date": self.fy_start.isoformat(),
                                    f"dr_{self._acc('1100').pk}": "1000",
                                    f"cr_{self._acc('3100').pk}": "900"})
        self._check(JournalEntry.objects.filter(company=self.company, entry_type="OPENING").count() == before,
                    "unbalanced opening balances are refused",
                    finding=("CRITICAL", "Opening balances", "Unbalanced opening balances were posted"))
        self._post(self.acct, url, {"posting_date": self.fy_start.isoformat(),
                                    f"dr_{self._acc('1100').pk}": "20000",
                                    f"dr_{self._acc('1200').pk}": "500000",
                                    f"dr_{self._acc('1400').pk}": "150000",
                                    f"cr_{self._acc('3100').pk}": "670000"})
        self._expect_bal("1100", 20000, "opening balances")
        self._expect_bal("1200", 500000, "opening balances")
        self._expect_bal("1400", 150000, "opening balances")
        self._expect_bal("3100", 670000, "opening balances")
        self._tb_balanced("after opening balances")

    def _journal(self, client, ref, lines, posting_date=None):
        from apps.accounting.models import JournalEntry
        data = {"reference": ref, "entry_type": "MANUAL",
                "posting_date": (posting_date or self.today).isoformat(), "notes": ref,
                "lines-TOTAL_FORMS": str(len(lines)), "lines-INITIAL_FORMS": "0",
                "lines-MIN_NUM_FORMS": "2", "lines-MAX_NUM_FORMS": "1000"}
        for i, (code, dr, cr) in enumerate(lines):
            data.update({f"lines-{i}-account": str(self._acc(code).pk), f"lines-{i}-description": ref,
                         f"lines-{i}-debit_amount": str(dr), f"lines-{i}-credit_amount": str(cr)})
        self._post(client, reverse("accounting:journal_entry_create"), data)
        return JournalEntry.objects.filter(company=self.company, reference=ref).first()

    def step_journals(self):
        rent = self._journal(self.acct, f"RENT-{self.tag}", [("6200", 30000, 0), ("1200", 0, 30000)])
        self._check(rent is not None and rent.status == "DRAFT", "rent journal saved as a draft")
        self._post(self.acct, reverse("accounting:journal_entry_post", args=[rent.pk]))
        rent.refresh_from_db()
        self._check(rent.status == "POSTED", "rent journal posted")
        self._expect_bal("6200", 30000, "rent journal")
        self._expect_bal("1200", 470000, "rent journal")

        bad = self._journal(self.acct, f"BAD-{self.tag}", [("6600", 100, 0), ("1100", 0, 90)])
        if bad:
            self._post(self.acct, reverse("accounting:journal_entry_post", args=[bad.pk]))
            bad.refresh_from_db()
        self._check(bad is None or bad.status != "POSTED", "unbalanced journal cannot be posted",
                    finding=("CRITICAL", "Journals", "An unbalanced journal was posted"))

        oops = self._journal(self.acct, f"OOPS-{self.tag}", [("6600", 5000, 0), ("1100", 0, 5000)])
        self._post(self.acct, reverse("accounting:journal_entry_post", args=[oops.pk]))
        self._post(self.acct, reverse("accounting:journal_entry_reverse", args=[oops.pk]))
        oops.refresh_from_db()
        self._check(oops.status == "POSTED" and bool(oops.reversed_by_id),
                    "mistaken journal reversed (original kept, linked to its reversal)",
                    finding=("HIGH", "Journals", "Reversing a journal did not link it to a reversal entry"))
        self._expect_bal("6600", 0, "posting then reversing a 5,000 journal")
        self._expect_bal("1100", 20000, "posting then reversing a 5,000 journal")
        self._tb_balanced("after journals")

    def step_sales_invoice(self):
        from apps.crm.models import Customer
        from apps.inventory.models import Product, UnitOfMeasure
        from apps.sales.models import Invoice
        self.sales = self._login(self.email["sales"], self.pw["sales"])
        self._post(self.sales, reverse("crm:customer_create"), {
            "name": f"Mama Mboga Supplies {self.tag}", "customer_type": "BUSINESS", "tax_id": "P051999000A",
            "phone": "+254722000000", "email": "accounts@mamamboga.co.ke", "website": "",
            "credit_limit": "100000", "payment_terms_days": "30", "currency": "KES",
            "address_line1": "Gikomba", "address_line2": "", "city": "Nairobi", "country": "Kenya", "notes": ""})
        self.customer = Customer.objects.filter(company=self.company, name__startswith="Mama Mboga").first()
        self._check(self.customer is not None, "sales rep added the customer")
        uom, _ = UnitOfMeasure.objects.get_or_create(company=self.company, name="Each",
                                                     defaults={"abbreviation": "ea"})
        self.product = Product.objects.create(company=self.company, sku=f"SUG-{self.tag}", name="Sugar 50kg",
                                              product_type="GOODS", unit_of_measure=uom,
                                              cost_price=D("700"), selling_price=D("1000"))
        self.invoice = self._raise_invoice(f"INV-{self.tag}-1", qty=10, price=1000, vat=True)
        inv = self.invoice
        self._check(inv is not None, "sales rep raised the invoice")
        self._check(inv.subtotal == D("10000") and inv.tax_amount == D("1600") and inv.total_amount == D("11600"),
                    f"invoice totals 10,000 + 1,600 VAT = 11,600 (got {inv.subtotal} + {inv.tax_amount} = {inv.total_amount})",
                    finding=("HIGH", "Invoicing", "Invoice totals don't match its lines"))
        self._check(inv.status == "SENT", f"invoice status is Sent (got {inv.get_status_display()})",
                    finding=("HIGH", "Invoicing", f"New invoice shows status {inv.get_status_display()}"))
        self._check(bool(inv.journal_entry_id), "invoice posted to the ledger automatically",
                    finding=("CRITICAL", "Invoicing", "Invoice was not posted to the ledger"))
        if inv.journal_entry_id:
            lines = {(l.account.code, l.debit_amount, l.credit_amount) for l in inv.journal_entry.lines.all()}
            want = {("1300", D("11600"), D("0")), ("4100", D("0"), D("10000")), ("2200", D("0"), D("1600"))}
            self._check(lines == want, "invoice entry: Dr Receivables 11,600 / Cr Sales 10,000 / Cr VAT 1,600",
                        finding=("CRITICAL", "Invoicing", "Invoice posted to the wrong accounts", str(sorted(lines))))
        self._tb_balanced("after invoicing")

    def _raise_invoice(self, ref, qty, price, vat):
        from apps.sales.models import Invoice
        data = {"reference": ref, "customer": str(self.customer.pk),
                "invoice_date": self.today.isoformat(),
                "due_date": (self.today + timedelta(days=30)).isoformat(), "currency": "KES", "notes": "",
                "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "1",
                "lines-MAX_NUM_FORMS": "1000", "lines-0-product": str(self.product.pk),
                "lines-0-description": self.product.name, "lines-0-quantity": str(qty),
                "lines-0-unit_price": str(price), "lines-0-discount_percent": "0",
                "lines-0-tax_rate": str(self.vat.pk) if vat else ""}
        self._post(self.sales, reverse("sales:invoice_create"), data)
        return Invoice.objects.filter(company=self.company, reference=ref).first()

    def step_receipt(self):
        from apps.sales.models import CustomerPayment
        self._post(self.acct, reverse("sales:payment_create"), {
            "reference": f"RCT-{self.tag}", "customer": str(self.customer.pk),
            "payment_date": self.today.isoformat(), "amount": "5000", "payment_method": "BANK_TRANSFER",
            "transaction_reference": "MPESA-QWE123", "notes": ""})
        pay = CustomerPayment.objects.filter(company=self.company, reference=f"RCT-{self.tag}").first()
        self._check(pay is not None and bool(pay.journal_entry_id), "receipt recorded and posted")
        self.invoice.refresh_from_db()
        self._check(self.invoice.paid_amount == D("5000") and self.invoice.outstanding_amount == D("6600")
                    and self.invoice.status == "PARTIAL",
                    f"invoice now part-paid: 6,600 outstanding (got {self.invoice.outstanding_amount}, "
                    f"{self.invoice.get_status_display()})",
                    finding=("HIGH", "Receipts", "Receipt was not allocated to the invoice"))
        self._expect_bal("1200", 475000, "5,000 receipt into the bank")
        self._expect_bal("1300", 6600, "5,000 receipt")

    def step_cancel_invoice(self):
        inv2 = self._raise_invoice(f"INV-{self.tag}-2", qty=2, price=1000, vat=False)
        self._expect_bal("1300", 8600, "a second 2,000 invoice")
        self._post(self.acct, reverse("sales:invoice_cancel", args=[inv2.pk]))
        inv2.refresh_from_db()
        self._check(inv2.status == "CANCELLED", "accountant cancelled the 2,000 invoice")
        ok = self._expect_bal("1300", 6600, "cancelling the 2,000 invoice")
        if not ok:
            self.findings[-1] = Finding("CRITICAL", "Invoicing",
                                        "Cancelling a posted invoice leaves it in the ledger, so receivables "
                                        "and sales stay overstated", f"Receivables {self._bal('1300')} instead of 6600")
        self._expect_bal("4100", 10000, "cancelling the 2,000 invoice")

    def step_edit_posted_invoice(self):
        from apps.crm.models import Customer
        other = Customer.objects.create(company=self.company, name=f"Other Buyer {self.tag}")
        url = reverse("sales:invoice_edit", args=[self.invoice.pk])
        self._post(self.acct, url, {"reference": self.invoice.reference, "customer": str(other.pk),
                                    "invoice_date": self.today.isoformat(),
                                    "due_date": (self.today + timedelta(days=30)).isoformat(),
                                    "currency": "KES", "notes": ""})
        self.invoice.refresh_from_db()
        self._check(self.invoice.customer_id == self.customer.pk,
                    "a posted invoice can't be moved to a different customer",
                    finding=("HIGH", "Invoicing", "A posted, part-paid invoice can be edited and moved to "
                                                  "another customer; the ledger and receipts no longer match it"))
        if self.invoice.customer_id != self.customer.pk:
            type(self.invoice).objects.filter(pk=self.invoice.pk).update(customer=self.customer)
            self.invoice.refresh_from_db()

    def step_bill(self):
        from apps.crm.models import Supplier
        from apps.procurement.models import Bill
        self.supplier = Supplier.objects.create(company=self.company, name=f"Mumias Sugar Co {self.tag}",
                                                email="sales@mumias.co.ke")
        url = reverse("procurement:bill_create")
        # Totals that don't add up must be refused.
        self._post(self.acct, url, {"reference": f"BILL-{self.tag}-X", "supplier": str(self.supplier.pk),
                                    "supplier_reference": "X", "bill_date": self.today.isoformat(),
                                    "due_date": (self.today + timedelta(days=30)).isoformat(),
                                    "subtotal": "1000", "tax_amount": "160", "total_amount": "2000", "notes": ""})
        bad = Bill.objects.filter(company=self.company, reference=f"BILL-{self.tag}-X").first()
        self._check(bad is None, "bill whose subtotal + VAT ≠ total is refused",
                    finding=("HIGH", "Bills", "A bill with subtotal 1,000 + VAT 160 and total 2,000 was accepted"))
        if bad:
            bad.delete()
        self._post(self.acct, url, {"reference": f"BILL-{self.tag}", "supplier": str(self.supplier.pk),
                                    "supplier_reference": "MS-7781", "bill_date": self.today.isoformat(),
                                    "due_date": (self.today + timedelta(days=30)).isoformat(),
                                    "subtotal": "20000", "tax_amount": "3200", "total_amount": "23200",
                                    "notes": "20 bags sugar"})
        self.bill = Bill.objects.filter(company=self.company, reference=f"BILL-{self.tag}").first()
        self._check(self.bill is not None, "accountant entered the 23,200 supplier bill")
        posted = bool(self.bill and self.bill.journal_entry_id)
        self._check(posted, "bill posted to the ledger",
                    finding=("CRITICAL", "Bills", "Supplier bills are never posted to the ledger: payables, "
                                                  "stock and input VAT are missing from the books"))
        if not posted and self.bill:
            from apps.accounting.gl_service import post_bill
            post_bill(self.bill)
            self.bill.refresh_from_db()
            self._note("workaround: bill posted directly so payment can be tested")
        self._expect_bal("2100", 23200, "the bill")
        self._expect_bal("1400", 170000, "20,000 of stock on the bill")
        self._expect_bal("2210", -3200, "3,200 input VAT (a debit on a credit-normal VAT account)")

        # A utilities bill charged to an expense account, not stock.
        self._post(self.acct, url, {"reference": f"KPLC-{self.tag}", "supplier": str(self.supplier.pk),
                                    "supplier_reference": "KPLC-OCT", "bill_date": self.today.isoformat(),
                                    "due_date": (self.today + timedelta(days=14)).isoformat(),
                                    "expense_account": str(self._acc("6300").pk),
                                    "subtotal": "5000", "tax_amount": "800", "total_amount": "5800", "notes": ""})
        self._expect_bal("6300", 5000, "an electricity bill charged to Utilities")
        self._expect_bal("1400", 170000, "an electricity bill (stock untouched)")
        self._expect_bal("2100", 29000, "the electricity bill")

        # Once posted, the amounts are locked to the ledger.
        self._post(self.acct, reverse("procurement:bill_edit", args=[self.bill.pk]), {
            "reference": self.bill.reference, "supplier": str(self.supplier.pk), "supplier_reference": "MS-7781",
            "bill_date": self.today.isoformat(), "due_date": (self.today + timedelta(days=45)).isoformat(),
            "expense_account": "", "subtotal": "90000", "tax_amount": "0", "total_amount": "90000", "notes": "x"})
        self.bill.refresh_from_db()
        self._check(self.bill.total_amount == D("23200"), "a posted bill's amounts can't be edited",
                    finding=("HIGH", "Bills", "A posted bill's amounts can be changed, so it no longer matches "
                                              "the ledger"))

        # Cancelling a posted bill reverses it.
        self._post(self.acct, url, {"reference": f"BILL-{self.tag}-C", "supplier": str(self.supplier.pk),
                                    "supplier_reference": "C", "bill_date": self.today.isoformat(),
                                    "due_date": (self.today + timedelta(days=30)).isoformat(),
                                    "expense_account": str(self._acc("6300").pk),
                                    "subtotal": "1000", "tax_amount": "160", "total_amount": "1160", "notes": ""})
        wrong = Bill.objects.filter(company=self.company, reference=f"BILL-{self.tag}-C").first()
        self._post(self.acct, reverse("procurement:bill_cancel", args=[wrong.pk]))
        wrong.refresh_from_db()
        self._check(wrong.status == "CANCELLED", "accountant cancelled a mistaken 1,160 bill")
        self._expect_bal("2100", 29000, "cancelling the mistaken bill")
        self._expect_bal("6300", 5000, "cancelling the mistaken bill")
        self._expect_bal("2210", -4000, "input VAT on both live bills")

    def step_bill_payment(self):
        from apps.accounting.models import JournalEntry
        if self.bill.bill_date == self.today:
            type(self.bill).objects.filter(pk=self.bill.pk).update(
                bill_date=self.today - timedelta(days=10))
            self.bill.refresh_from_db()
        before = set(JournalEntry.objects.filter(company=self.company).values_list("pk", flat=True))
        self._post(self.acct, reverse("procurement:bill_pay", args=[self.bill.pk]), {
            "amount": "10000", "pay_from": str(self._acc("1200").pk), "payment_date": self.today.isoformat()})
        self.bill.refresh_from_db()
        self._check(self.bill.paid_amount == D("10000") and self.bill.outstanding_amount == D("13200"),
                    f"bill part-paid: 13,200 outstanding (got {self.bill.outstanding_amount})")
        new = JournalEntry.objects.filter(company=self.company).exclude(pk__in=before).first()
        self._check(new is not None and new.posting_date == self.today,
                    f"payment is dated the day it was paid ({self.today}), not the bill date",
                    finding=("HIGH", "Supplier payments", "Supplier payments are dated on the bill date, not the "
                                                          "payment date",
                             f"posted on {new.posting_date if new else '—'}"))
        self._expect_bal("2100", 19000, "10,000 supplier payment")
        ok = self._expect_bal("1200", 465000, "10,000 paid from the bank")
        if not ok:
            self.findings[-1] = Finding("HIGH", "Supplier payments",
                                        "Supplier payments come out of the first cash account (Cash on Hand), "
                                        "not the bank, and the accountant can't choose",
                                        f"bank {self._bal('1200')}, cash {self._bal('1100')}")
            cash_moved = D("20000") - (self._bal("1100") or 0)
            if cash_moved:
                self._note("continuing with the payment where the app put it")

    def step_expense_claim(self):
        from apps.expenses.models import ExpenseCategory, ExpenseClaim
        from apps.hr.models import Employee
        from apps.accounts.models import User
        staff_user = User.objects.get(email=self.email["staff"])
        Employee.objects.create(company=self.company, user=staff_user, employee_number=f"E-{self.tag}",
                                first_name="Akinyi", last_name="Njeri",
                                date_hired=self.today - timedelta(days=200))
        self._post(self.acct, reverse("expenses:category_create"), {
            "name": "Travel", "gl_account": str(self._acc("6400").pk)})
        cat = ExpenseCategory.objects.filter(company=self.company, name="Travel").first()
        self._check(cat is not None, "accountant created the Travel expense category (→ 6400)")
        staff = self._login(self.email["staff"], self.pw["staff"])
        emp = Employee.objects.get(company=self.company, user=staff_user)
        ref = f"EXP-{self.tag}"
        self._post(staff, reverse("expenses:claim_create"), {
            "reference": ref, "employee": str(emp.pk), "claim_date": self.today.isoformat()})
        claim = ExpenseClaim.objects.filter(company=self.company, reference=ref).first()
        self._check(claim is not None, "employee opened an expense claim")
        if not claim:
            return
        self._post(staff, reverse("expenses:claim_add_line", args=[claim.pk]), {
            "category": str(cat.pk), "description": "Matatu fare to Thika client",
            "expense_date": self.today.isoformat(), "amount": "3000"})
        self._post(staff, reverse("expenses:claim_submit", args=[claim.pk]))
        claim.refresh_from_db()
        self._check(claim.status == "SUBMITTED" and claim.total_amount == D("3000"),
                    f"employee submitted a 3,000 claim (got {claim.get_status_display()}, {claim.total_amount})")
        self._post(self.acct, reverse("expenses:claim_approve", args=[claim.pk]))
        claim.refresh_from_db()
        self._check(claim.status == "SUBMITTED", "accountant cannot approve the claim (finance manager does)")
        fm = self._login(self.email["finance"], self.pw["finance"])
        self._post(fm, reverse("expenses:claim_approve", args=[claim.pk]))
        claim.refresh_from_db()
        self._check(claim.status in ("APPROVED", "PAID"), "finance manager approved the claim")
        ok = self._expect_bal("6400", 3000, "the approved 3,000 travel claim")
        if not ok:
            self.findings[-1] = Finding("HIGH", "Expense claims",
                                        "Approved expense claims never reach the ledger: the expense and the "
                                        "amount owed or paid to the employee are missing")
        self._expect_bal("2610", 3000, "3,000 now owed to the employee")
        self._post(self.acct, reverse("expenses:claim_pay", args=[claim.pk]), {
            "pay_from": str(self._acc("1200").pk), "payment_date": self.today.isoformat()})
        claim.refresh_from_db()
        self._check(claim.status == "PAID", "accountant reimbursed the employee from the bank",
                    finding=("HIGH", "Expense claims", "Approved claims can't be paid"))
        self._expect_bal("2610", 0, "reimbursing the employee")
        self._expect_bal("1200", 462000, "3,000 reimbursement from the bank")
        self.claim = claim

    def step_reports(self):
        rng = f"?date_from={self.fy_start.isoformat()}&date_to={self.today.isoformat()}"
        r = self._get(self.acct, reverse("reporting:trial_balance") + rng)
        self._check(r.status_code == 200 and r.context["is_balanced"],
                    f"trial balance balances (Dr {r.context['total_debit']:,.2f} = Cr {r.context['total_credit']:,.2f})")
        r = self._get(self.acct, reverse("reporting:trial_balance"))
        tb_cash = next((row for row in r.context["rows"] if row["account"].code == "1200"), None)
        tb_bank = (tb_cash["debit"] - tb_cash["credit"]) if tb_cash else D(0)
        self._check(abs(tb_bank - (self._bal("1200") or 0)) < D("0.01"),
                    f"default trial balance shows the bank's actual balance ({self._bal('1200'):,.2f}), "
                    f"not just this month's movements ({tb_bank:,.2f})",
                    finding=("MEDIUM", "Reports", "The trial balance only shows movements since the 1st of the "
                                                  "month, so opening and earlier balances are missing from it"))

        r = self._get(self.acct, reverse("reporting:profit_loss") + rng)
        rev, exp = self._bal("4100") or 0, sum((self._bal(c) or 0) for c in ("6000", "6200", "6300", "6400", "6600"))
        want = rev - exp
        self._check(abs(r.context["net_profit"] - want) < D("0.01"),
                    f"P&L net result {r.context['net_profit']:,.2f} matches the ledger ({want:,.2f})",
                    finding=("HIGH", "Reports", "Profit & loss doesn't match the ledger",
                             f"{r.context['net_profit']} vs {want}"))
        self.net_profit = want

        r = self._get(self.acct, reverse("reporting:balance_sheet") + f"?date_to={self.today.isoformat()}")
        a, le = r.context["total_assets"], r.context["total_liab_equity"]
        self._check(abs(a - le) < D("0.01"),
                    f"balance sheet balances (assets {a:,.2f} = liabilities + equity {le:,.2f})",
                    finding=("HIGH", "Reports", "The balance sheet doesn't balance: this year's profit or loss "
                                                "isn't included in equity", f"assets {a} vs {le}"))

        r = self._get(self.acct, reverse("reporting:aged_debtors"))
        self._check(abs(r.context["grand_total"] - (self._bal("1300") or 0)) < D("0.01"),
                    f"aged debtors ({r.context['grand_total']:,.2f}) agrees with receivables in the ledger "
                    f"({self._bal('1300'):,.2f})",
                    finding=("HIGH", "Reports", "Aged debtors disagrees with the receivables account"))
        r = self._get(self.acct, reverse("reporting:aged_creditors"))
        self._check(abs(r.context["grand_total"] - (self._bal("2100") or 0)) < D("0.01"),
                    f"aged creditors ({r.context['grand_total']:,.2f}) agrees with payables in the ledger "
                    f"({self._bal('2100'):,.2f})",
                    finding=("HIGH", "Reports", "Aged creditors disagrees with the payables account"))
        for name in ("general_ledger", "journal_entry_list", "account_list"):
            self._check(self._get(self.acct, reverse(f"accounting:{name}")).status_code == 200, f"{name.replace('_', ' ')} opens")
        r = self._get(self.acct, reverse("accounting:account_ledger", args=[self._acc("1200").pk]))
        self._check(r.status_code == 200, "bank account ledger opens")
        self._check(self._get(self.acct, reverse("reporting:cash_flow")).status_code == 200, "cash flow report opens")

    def step_documents(self):
        for name in ("sales:invoice_print", "sales:invoice_email"):
            r = self._get(self.acct, reverse(name, args=[self.invoice.pk]))
            self._check(r.status_code == 200 and self.invoice.reference in r.content.decode(),
                        f"{name.split(':')[1].replace('_', ' ')} works")

    def step_segregation(self):
        checks = [
            ("GET", reverse("accounts:user_list"), "see or manage users"),
            ("GET", reverse("payroll:run_list"), "open payroll"),
            ("POST", reverse("organizations:branding"), "change company branding"),
        ]
        if self.fy:
            from apps.organizations.models import AccountingPeriod
            p = AccountingPeriod.objects.filter(fiscal_year=self.fy).first()
            checks.append(("POST", reverse("accounting:period_close", args=[p.pk]), "close a period"))
            checks.append(("POST", reverse("accounting:fiscal_year_close", args=[self.fy.pk]), "close the year"))
        for method, url, what in checks:
            r = self._get(self.acct, url) if method == "GET" else self._post(self.acct, url, {})
            self._check(r.status_code == 403, f"accountant cannot {what}",
                        finding=("HIGH", "Segregation of duties", f"An accountant can {what}"))

    def step_period_close(self):
        from apps.organizations.models import AccountingPeriod
        if not self.fy:
            return
        last_month_day = self.today.replace(day=1) - timedelta(days=1)
        period = (AccountingPeriod.objects.filter(fiscal_year=self.fy, start_date__lte=last_month_day,
                                                  end_date__gte=last_month_day).first()
                  or AccountingPeriod.objects.filter(fiscal_year=self.fy, start_date__lte=self.today,
                                                     end_date__gte=self.today).first())
        fm = self._login(self.email["finance"], self.pw["finance"])
        self._post(fm, reverse("accounting:period_close", args=[period.pk]))
        period.refresh_from_db()
        self._check(period.status == "CLOSED", f"finance manager closed {period.name}")
        back_date = period.end_date - timedelta(days=2)
        je = self._journal(self.acct, f"LATE-{self.tag}", [("6200", 999, 0), ("1200", 0, 999)], posting_date=back_date)
        if je:
            self._post(self.acct, reverse("accounting:journal_entry_post", args=[je.pk]))
            je.refresh_from_db()
        self._check(je is None or je.status != "POSTED",
                    f"a journal dated {back_date} can't be posted into the closed period",
                    finding=("CRITICAL", "Period close", "Closing a period doesn't stop postings into it; "
                                                         "closed months can still be changed"))
        if je and je.status == "POSTED":
            je.status = "CANCELLED"
            je.save(update_fields=["status"])
        r = self._post(self.acct, reverse("accounting:opening_balances"), {
            "posting_date": back_date.isoformat(), f"dr_{self._acc('1100').pk}": "1",
            f"cr_{self._acc('3100').pk}": "1"})
        from apps.accounting.models import JournalEntry
        late_ob = JournalEntry.objects.filter(company=self.company, entry_type="OPENING",
                                              posting_date=back_date, status="POSTED").first()
        self._check(late_ob is None, "opening balances can't be posted into the closed period either",
                    finding=("HIGH", "Period close", "Opening balances can be posted into a closed period"))
        if late_ob:
            late_ob.status = "CANCELLED"
            late_ob.save(update_fields=["status"])

    def step_final_proof(self):
        from apps.procurement.models import Bill
        from apps.sales.models import Invoice
        self._tb_balanced("at the end of the cycle")
        ar_sub = Invoice.objects.filter(company=self.company, journal_entry__isnull=False,
                                        status__in=["SENT", "PARTIAL", "OVERDUE"]).aggregate(
            t=Sum("outstanding_amount"))["t"] or D(0)
        self._check(abs(ar_sub - (self._bal("1300") or 0)) < D("0.01"),
                    f"receivables in the ledger ({self._bal('1300'):,.2f}) = unpaid invoices ({ar_sub:,.2f})",
                    finding=("HIGH", "Reconciliation", "Receivables ledger ≠ unpaid invoices"))
        ap_sub = Bill.objects.filter(company=self.company, status__in=["RECEIVED", "PARTIAL", "OVERDUE"]).aggregate(
            t=Sum("outstanding_amount"))["t"] or D(0)
        self._check(abs(ap_sub - (self._bal("2100") or 0)) < D("0.01"),
                    f"payables in the ledger ({self._bal('2100'):,.2f}) = unpaid bills ({ap_sub:,.2f})",
                    finding=("HIGH", "Reconciliation", "Payables ledger ≠ unpaid bills"))
        net_vat = (self._bal("2200") or 0) + (self._bal("2210") or 0)
        self._check(net_vat == D("1600") - D("4000"),
                    f"net VAT position {net_vat:,.2f} (1,600 output − 4,000 input = −2,400 refundable)",
                    finding=("HIGH", "VAT", f"Net VAT position is {net_vat}, expected -2,400"))

    # ------------------------------------------------------------- report

    def _report(self):
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
        seen, rows = set(), []
        for f in sorted(self.findings, key=lambda f: order[f.severity]):
            if (f.area, f.title) not in seen:
                seen.add((f.area, f.title))
                rows.append(f)
        self.stdout.write(f"\n{BOLD}Accountant UAT — result{RESET}")
        self.stdout.write(f"  Checks passed: {GREEN}{self.passed}{RESET}")
        self.stdout.write(f"  Findings:      {(RED if rows else GREEN)}{len(rows)}{RESET}")
        colour = {"CRITICAL": RED, "HIGH": YELLOW, "MEDIUM": CYAN}
        for f in rows:
            self.stdout.write(f"  {colour[f.severity]}{f.severity:8}{RESET} {f.area:22} {f.title}"
                              f"{DIM}{'  — ' + f.detail if f.detail else ''}{RESET}")
        if not rows:
            self.stdout.write(f"\n  {GREEN}{BOLD}An accountant can run the full cycle and the books are right.{RESET}")
        md = ["# mofisi — accountant UAT", "",
              f"Run `{self.tag}` on {date.today().isoformat()}, signed in as an Accountant "
              f"(plus owner, finance manager, sales rep and employee where the workflow needs them).", "",
              f"- Checks passed: **{self.passed}**", f"- Findings: **{len(rows)}**", ""]
        if rows:
            md += ["| Severity | Area | Finding | Detail |", "|---|---|---|---|"]
            md += [f"| {f.severity} | {f.area} | {f.title} | {f.detail or '—'} |" for f in rows]
        return "\n".join(md) + "\n"
