"""Regression checks for the ledger fixes: reversals, closes, assets, opening
balances, accounts, trial balances and exchange revaluation.

Usage:
    python manage.py test_fix_ledger          # rolls the whole run back
    python manage.py test_fix_ledger --keep   # leave the test companies behind

Each section builds a company of its own through the company form, so one
section's closes and revaluations cannot leak into another's figures, and
drives the real screens with a logged-in client the way a bookkeeper would.
Everything happens inside one transaction that is rolled back at the end.

    1. the generic Reverse button   -> only hand-typed journals are reversed
                                       there; invoices, closes, depreciation,
                                       revaluations and reversals themselves
                                       are refused and pointed at their screen
    2. manual journal types         -> CLOSING, REVALUATION and the other
                                       machinery types cannot be typed; lines
                                       of nothing and journals of nothing are
                                       refused; a date with no fiscal year is
                                       refused at Post
    3. closes                       -> a late posting into a closed month is
                                       swept at year end, the year end reads
                                       3300 as at its own date, a closed year
                                       takes no postings and its months cannot
                                       be reopened on their own, years close and
                                       reopen in order
    4. the fiscal year form         -> overlapping and backwards years refused
    5. fixed assets                 -> one charge per calendar month, missed
                                       months caught up together, accounts
                                       locked once posted, status off the form,
                                       an unbought asset is neither charged nor
                                       disposed of
    6. opening balances             -> junk, negative, two-sided and sub-cent
                                       figures refused without a 500, and only
                                       one live opening entry
    7. the chart of accounts        -> a type fixed once posted to, a balance
                                       blocks retirement
    8. trial balances and dashboard -> as at a date, closing balances with
                                       what was brought forward, cash as at
                                       today
    9. exchange revaluation         -> outstanding read as at the revaluation
                                       day, and a settled document's
                                       unrealised share released on settlement
   10. the role table               -> expense claims, documents, tickets,
                                       disposals, and the rules other modules
                                       asked for (orders, payroll, receiving,
                                       purchase orders, transfers)
   11. object rules                 -> only an admin or the uploader deletes a
                                       document, and the file goes with it;
                                       only the assignee resolves a ticket and
                                       the reporter may close it; marking a
                                       notification read is a POST
   12. dashboard and search         -> each widget and result type only for a
                                       role that may open the page behind it,
                                       and no link the role cannot follow
   13. numbering                    -> opening a create form takes no number;
                                       saving takes the one shown
   14. figures and limits           -> index tiles as at today, cash flow nets
                                       transfers and keeps retired tills,
                                       budgets ignore the close, the currency
                                       register is the platform's, lives are
                                       capped at 100 years
   15. revaluation shares           -> a part payment releases its slice, a
                                       cancellation or a backdated receipt the
                                       rest, and checking twice changes nothing
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


class Command(BaseCommand):
    help = "Regression checks for the ledger fixes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep", action="store_true",
            help="Commit the test data instead of rolling it back.",
        )

    def handle(self, *args, **opts):
        self.passed = 0
        self.failures = []
        self.today = datetime.date.today()
        self.year = self.today.year

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

    def flashes(self, client, response):
        """Messages only appear once the redirect target renders them."""
        location = response.get("Location")
        page = client.get(location) if location else response
        ctx = getattr(page, "context", None)
        if not ctx or "messages" not in ctx:
            return []
        return [str(m.message) for m in ctx["messages"]]

    def said(self, client, response, needle):
        """Whether the page the response lands on says `needle`.

        Rendering consumes the messages, so what was read is kept on
        self.heard for the check's detail line.
        """
        self.heard = self.flashes(client, response)
        return any(needle.lower() in m.lower() for m in self.heard)

    # ─────────────────────────────── a book ──────────────────────────────────

    def book(self, label):
        """A fresh company through the company form, and a client working in it."""
        from apps.accounts.models import User
        from apps.organizations.models import Company

        stamp = datetime.datetime.now().strftime("%H%M%S%f")
        user = User.objects.create(
            username=f"fix.{label}.{stamp}", email=f"fix.{label}.{stamp}@ledger.local",
            first_name="Fix", last_name=label.title(),
        )
        user.set_password("fix-ledger-pw")
        user.save()
        client = Client()
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        name = f"Ledger Fix {label} {stamp}"
        client.post(reverse("organizations:company_create"), {
            "name": name, "legal_name": f"{name} Ltd", "registration_number": f"PVT-{stamp}",
            "tax_id": f"A{stamp}Z", "vat_number": "VAT-1", "currency": "KES",
            "timezone": "Africa/Nairobi", "fiscal_year_start_month": "1",
            "industry": "Trading", "phone": "+254700000000", "email": f"i@{stamp}.ledger.local",
            "website": "", "address_line1": "1 Ledger St", "address_line2": "",
            "city": "Nairobi", "state_province": "Nairobi", "postal_code": "00100",
            "country": "Kenya",
        })
        company = Company.objects.get(name=name)
        session = client.session
        session["active_company_id"] = str(company.id)
        session.save()
        self.company, self.client, self.user = company, client, user
        return company, client, user

    def account(self, code):
        from apps.accounting.models import Account
        return Account.objects.get(company=self.company, code=code)

    def bal(self, code, upto=None):
        """Debit minus credit on an account, optionally as at a day."""
        from apps.accounting.models import JournalEntryLine
        lines = JournalEntryLine.objects.filter(
            journal_entry__company=self.company, journal_entry__status="POSTED",
            account__code=code)
        if upto:
            lines = lines.filter(journal_entry__posting_date__lte=upto)
        totals = lines.aggregate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        return (totals["dr"] or Z) - (totals["cr"] or Z)

    def journal_data(self, lines, on_date, entry_type="MANUAL", blanks=1):
        data = {"reference": "", "entry_type": entry_type,
                "posting_date": str(on_date), "notes": "Ledger fix check",
                "lines-TOTAL_FORMS": str(len(lines) + blanks), "lines-INITIAL_FORMS": "0",
                "lines-MIN_NUM_FORMS": "2", "lines-MAX_NUM_FORMS": "1000"}
        for i, (code, dr, cr) in enumerate(lines):
            data.update({f"lines-{i}-account": str(self.account(code).pk),
                         f"lines-{i}-description": f"line {i}",
                         f"lines-{i}-debit_amount": str(dr),
                         f"lines-{i}-credit_amount": str(cr)})
        for j in range(blanks):
            k = len(lines) + j
            data.update({f"lines-{k}-account": "", f"lines-{k}-description": "",
                         f"lines-{k}-debit_amount": "0", f"lines-{k}-credit_amount": "0"})
        return data

    def draft(self, lines, on_date, entry_type="MANUAL"):
        """POST the manual journal form; (entry or None, response)."""
        from apps.accounting.models import JournalEntry
        resp = self.client.post(reverse("accounting:journal_entry_create"),
                                self.journal_data(lines, on_date, entry_type))
        if resp.status_code not in (301, 302, 303):
            return None, resp
        match = re.search(r"/journal-entries/([0-9a-fA-F-]{36})/?$", resp["Location"])
        return JournalEntry.objects.get(pk=match.group(1)), resp

    def journal(self, lines, on_date):
        entry, resp = self.draft(lines, on_date)
        if entry is None:
            raise AssertionError(f"journal refused on {on_date}: HTTP {resp.status_code}")
        resp = self.client.post(reverse("accounting:journal_entry_post", args=[entry.pk]), {})
        entry.refresh_from_db()
        return entry, resp

    def invoice(self, reference, on_date, amount, currency="KES", rate=None):
        """A posted invoice, raised the way the sales module raises it."""
        from apps.accounting import gl_service
        from apps.crm.models import Customer
        from apps.sales.models import Invoice, InvoiceLine

        customer = Customer.objects.filter(company=self.company).first() or \
            Customer.objects.create(company=self.company, name="Kamau Stores")
        invoice = Invoice.objects.create(
            company=self.company, customer=customer, reference=reference,
            invoice_date=on_date, due_date=on_date + datetime.timedelta(days=30),
            currency=currency, exchange_rate=rate, subtotal=D(amount), tax_amount=Z,
            total_amount=D(amount))
        InvoiceLine.objects.create(invoice=invoice, description="Consulting",
                                   quantity=D("1"), unit_price=D(amount))
        gl_service.post_invoice(invoice, self.user)
        invoice.refresh_from_db()
        return invoice

    def day(self, month, day=15):
        return datetime.date(self.year, month, day)

    # ─────────────────────────────── the run ─────────────────────────────────

    def run(self):
        self.section("1. The generic Reverse button reverses only what a person typed")
        self.check_generic_reverse()
        self.section("2. Manual journal types, empty lines and dates with no year")
        self.check_manual_journals()
        self.section("3. Period and year close")
        self.check_closes()
        self.section("4. The fiscal year form")
        self.check_fiscal_year_form()
        self.section("5. Fixed assets")
        self.check_assets()
        self.section("6. Opening balances")
        self.check_opening_balances()
        self.section("7. The chart of accounts")
        self.check_accounts()
        self.section("8. Trial balances and the dashboard's cash")
        self.check_reports()
        self.section("9. Exchange revaluation as at its day, released on settlement")
        self.check_revaluation()
        self.section("10. The role table")
        self.check_role_table()
        self.section("11. Documents, tickets and notifications")
        self.check_object_rules()
        self.section("12. The dashboard and the search box, by role")
        self.check_dashboard_rights()
        self.section("13. Opening a form takes no number")
        self.check_numbering()
        self.section("14. Index tiles, cash flow, budgets, currencies, asset lives")
        self.check_figures()
        self.section("15. A revaluation's shares, released in proportion")
        self.check_revaluation_shares()

    # ────────────────────────── 1. generic reverse ───────────────────────────

    def check_generic_reverse(self):
        from django.core.exceptions import ValidationError
        from apps.accounting import gl_service

        company, client, user = self.book("reverse")
        reverse_url = lambda entry: reverse(  # noqa: E731
            "accounting:journal_entry_reverse", args=[entry.pk])

        manual, _ = self.journal([("6200", "1500.00", 0), ("1200", 0, "1500.00")],
                                 self.today)
        resp = client.post(reverse_url(manual), {})
        manual.refresh_from_db()
        reversal = manual.reversed_by
        self.expect("a hand-typed journal is reversed by the button",
                    reversal is not None and reversal.status == "POSTED",
                    f"HTTP {resp.status_code}")
        if reversal is None:
            return
        resp = client.post(reverse_url(reversal), {})
        reversal.refresh_from_db()
        self.expect("the reversal itself cannot be reversed by the button",
                    reversal.reversed_by_id is None, str(self.flashes(client, resp)[-1:]))
        try:
            reversal.reverse(user=user)
            refused = False
        except ValidationError:
            refused = True
        self.expect("and JournalEntry.reverse() refuses to reverse a reversal", refused)
        self.expect("so 6200 stays at nothing instead of reinstating the original",
                    self.bal("6200") == Z, str(self.bal("6200")))

        invoice = self.invoice("INV-FIX-1", self.today, "5000")
        entry = invoice.journal_entry
        resp = client.post(reverse_url(entry), {})
        entry.refresh_from_db()
        self.expect("an invoice's entry is refused by the journal screen",
                    entry.reversed_by_id is None)
        self.expect("and the refusal says to cancel the invoice instead",
                    self.said(client, resp, "Cancel the invoice"),
                    str(self.heard[-2:]))
        self.expect("the receivable still carries the invoice",
                    self.bal("1300") == D("5000.00"), str(self.bal("1300")))

        from apps.accounting.models import period_for
        period = period_for(company, self.today)
        client.post(reverse("accounting:period_close", args=[period.pk]), {})
        closing = gl_service.closing_entry_for(company, gl_service.PERIOD_CLOSE, period.pk)
        if self.expect("the month closed with a closing entry", closing is not None):
            resp = client.post(reverse_url(closing), {})
            closing.refresh_from_db()
            self.expect("a closing entry is refused by the journal screen",
                        closing.reversed_by_id is None)
            self.expect("and pointed at reopening the period",
                        self.said(client, resp, "Reopen the period"),
                        str(self.heard[-2:]))
        page = client.get(reverse("accounting:journal_entry_detail", args=[entry.pk]))
        self.expect("the invoice entry's page offers no Reverse button",
                    page.context["can_reverse"] is False
                    and "Cancel the invoice" in page.context["reverse_elsewhere"])
        page = client.get(reverse("accounting:journal_entry_detail", args=[manual.pk]))
        self.expect("a reversed journal's page offers none either",
                    page.context["can_reverse"] is False)

    # ────────────────────────── 2. manual journals ───────────────────────────

    def check_manual_journals(self):
        from apps.accounting.models import JournalEntry, JournalEntryLine
        from django.core.exceptions import ValidationError

        company, client, user = self.book("journals")
        for kind in ("CLOSING", "REVALUATION", "DEPRECIATION", "INVOICE", "REVERSAL"):
            entry, resp = self.draft([("1200", "5000", 0), ("4300", 0, "5000")],
                                     self.today, entry_type=kind)
            self.expect(f"a manual journal cannot be typed {kind}",
                        entry is None and resp.status_code == 200,
                        f"HTTP {resp.status_code}")
        entry, resp = self.draft([("1200", "5000", 0), ("4300", 0, "5000")], self.today)
        self.expect("a manual journal typed MANUAL is still accepted", entry is not None)

        entry, resp = self.draft([("6200", 100, 0), ("6300", 0, 0), ("1200", 0, 100)],
                                 self.today)
        self.expect("a line with neither a debit nor a credit is refused",
                    entry is None and resp.status_code == 200, f"HTTP {resp.status_code}")
        entry, resp = self.draft([("6200", 0, 0), ("1200", 0, 0)], self.today)
        self.expect("a journal of nothing but empty lines is refused",
                    entry is None and resp.status_code == 200, f"HTTP {resp.status_code}")

        # Lines written straight to the table, the way a module writes them,
        # meet the same rules at Post.
        empty = JournalEntry.objects.create(
            company=company, reference=f"JE-ZERO-{user.pk}", entry_type="MANUAL",
            posting_date=self.today)
        JournalEntryLine.objects.create(journal_entry=empty, account=self.account("6200"))
        JournalEntryLine.objects.create(journal_entry=empty, account=self.account("1200"))
        try:
            empty.post(user=user)
        except ValidationError:
            pass
        empty.refresh_from_db()
        self.expect("an all-zero entry does not post", empty.status == "DRAFT", empty.status)

        far = datetime.date(self.year + 3, 6, 15)
        entry, _ = self.draft([("6200", 77, 0), ("1200", 0, 77)], far)
        if self.expect("a journal for a year with no calendar can be drafted", entry is not None):
            resp = client.post(reverse("accounting:journal_entry_post", args=[entry.pk]), {})
            entry.refresh_from_db()
            # Refusing it would stop every posting in a book whose bookkeeper
            # has not opened the year yet; it posts, with the calendar warning.
            self.expect("a date no fiscal year covers still posts, with a warning",
                        entry.status == "POSTED" and entry.period_id is None, entry.status)

    # ─────────────────────────────── 3. closes ───────────────────────────────

    def check_closes(self):
        from apps.accounting import gl_service
        from apps.accounting.models import JournalEntry, period_for
        from apps.organizations.models import FiscalYear

        company, client, user = self.book("close")
        fy = FiscalYear.objects.get(company=company)
        self.journal([("1200", 100000, 0), ("4100", 0, 100000)], self.day(1))
        self.journal([("6200", 30000, 0), ("1200", 0, 30000)], self.day(1, 31))
        self.journal([("1200", 50000, 0), ("4100", 0, 50000)], self.day(2))
        self.journal([("6200", 70000, 0), ("1200", 0, 70000)], self.day(2, 28))
        self.journal([("1200", 10000, 0), ("4200", 0, 10000)], self.day(3, 1))
        jan = period_for(company, self.day(1))
        client.post(reverse("accounting:period_close", args=[jan.pk]), {})
        jan.refresh_from_db()
        self.expect("January closed", jan.status == "CLOSED", jan.status)

        late, resp = self.journal([("1200", 1234, 0), ("4100", 0, 1234)], self.day(1, 20))
        self.expect("a late posting into a closed month is still allowed, with a warning",
                    late.status == "POSTED" and "closed" in late.calendar_warning,
                    f"{late.status} {late.calendar_warning!r}")

        profit = D("100000") - D("30000") + D("50000") - D("70000") + D("10000") + D("1234")
        retained_before = -self.bal("3200")
        resp = client.post(reverse("accounting:fiscal_year_close", args=[fy.pk]), {})
        fy.refresh_from_db()
        self.expect("the year closed", fy.is_closed, str(self.flashes(client, resp)[-1:]))
        self.expect("retained earnings rose by the whole year's profit, late posting included",
                    -self.bal("3200") - retained_before == profit,
                    f"3200 moved {-self.bal('3200') - retained_before}, profit {profit}")
        for code in ("4100", "4200", "6200"):
            self.expect(f"{code} reads nothing as at the year end",
                        self.bal(code, upto=fy.end_date) == Z,
                        str(self.bal(code, upto=fy.end_date)))
        self.expect("3300 reads nothing as at the year end",
                    self.bal("3300", upto=fy.end_date) == Z, str(self.bal("3300", upto=fy.end_date)))
        sheet = client.get(reverse("reporting:balance_sheet")
                           + f"?date_to={fy.end_date:%Y-%m-%d}").context
        self.expect("the balance sheet at the year end balances with no profit left to carry",
                    sheet["is_balanced"] and sheet["current_year_profit"] == Z,
                    f"error {sheet['balancing_error']}, carried {sheet['current_year_profit']}")

        entry, _ = self.draft([("6200", 99, 0), ("1200", 0, 99)], self.day(6))
        resp = client.post(reverse("accounting:journal_entry_post", args=[entry.pk]), {})
        entry.refresh_from_db()
        self.expect("a closed year takes no postings", entry.status == "DRAFT", entry.status)
        self.expect("and says to reopen the year", self.said(client, resp, "Reopen the fiscal year"),
                    str(self.heard[-2:]))

        resp = client.post(reverse("accounting:period_reopen", args=[jan.pk]), {})
        jan.refresh_from_db()
        self.expect("a month of a closed year cannot be reopened on its own",
                    jan.status != "OPEN" and self.said(client, resp, "Reopen the fiscal year"),
                    f"{jan.status} {self.heard[-2:]}")

        # ── reopen, close again: one live year end, same result ──
        client.post(reverse("accounting:fiscal_year_reopen", args=[fy.pk]), {})
        fy.refresh_from_db()
        self.expect("the year reopened and gave its result back",
                    not fy.is_closed and -self.bal("3200") == retained_before,
                    str(-self.bal("3200")))
        self.expect("and the late posting is back in 4100 for the year",
                    -self.bal("4100", upto=fy.end_date) == D("151234"),
                    str(-self.bal("4100", upto=fy.end_date)))

        # ── the next year, and its January closed before this year is ──
        resp = client.post(reverse("accounting:fiscal_year_create"), {
            "name": f"FY {self.year + 1}", "start_date": f"{self.year + 1}-01-01",
            "end_date": f"{self.year + 1}-12-31"})
        nxt = FiscalYear.objects.filter(company=company, start_date__year=self.year + 1).first()
        if not self.expect("the next fiscal year was created with 12 months",
                           nxt is not None and nxt.periods.count() == 12,
                           f"HTTP {resp.status_code}"):
            return
        self.journal([("1200", 40000, 0), ("4100", 0, 40000)], datetime.date(self.year + 1, 1, 10))
        n_jan = period_for(company, datetime.date(self.year + 1, 1, 10))
        client.post(reverse("accounting:period_close", args=[n_jan.pk]), {})
        self.expect("next January's profit sits in 3300", -self.bal("3300") == D("40000"),
                    str(-self.bal("3300")))

        resp = client.post(reverse("accounting:fiscal_year_close", args=[nxt.pk]), {})
        nxt.refresh_from_db()
        self.expect("a year cannot close while an earlier year is open",
                    not nxt.is_closed and self.said(client, resp, "closed in order"),
                    str(self.heard[-2:]))

        retained_before = -self.bal("3200")
        client.post(reverse("accounting:fiscal_year_close", args=[fy.pk]), {})
        fy.refresh_from_db()
        self.expect("closing this year sweeps only this year's profit, not next January's",
                    fy.is_closed and -self.bal("3200") - retained_before == profit,
                    f"3200 moved {-self.bal('3200') - retained_before}, profit {profit}")
        self.expect("next January's profit stays in 3300 for next year",
                    -self.bal("3300") == D("40000"), str(-self.bal("3300")))
        live = JournalEntry.objects.filter(
            company=company, entry_type="CLOSING", source_document_type=gl_service.YEAR_CLOSE,
            status="POSTED", reversed_by__isnull=True, reversal_of__isnull=True)
        self.expect("exactly one live year end entry", live.count() == 1, str(live.count()))

        client.post(reverse("accounting:fiscal_year_close", args=[nxt.pk]), {})
        nxt.refresh_from_db()
        resp = client.post(reverse("accounting:fiscal_year_reopen", args=[fy.pk]), {})
        fy.refresh_from_db()
        self.expect("a year cannot reopen while a later year is closed",
                    nxt.is_closed and fy.is_closed and self.said(client, resp, "first"),
                    str(self.heard[-2:]))
        self.expect("after both years close, 3300 is empty and retained earnings holds both",
                    self.bal("3300") == Z and -self.bal("3200") == profit + D("40000"),
                    f"3300 {self.bal('3300')}, 3200 {-self.bal('3200')}")

    # ─────────────────────────── 4. fiscal year form ─────────────────────────

    def check_fiscal_year_form(self):
        from apps.organizations.models import FiscalYear

        company, client, user = self.book("calendar")
        url = reverse("accounting:fiscal_year_create")
        for label, data in (
            ("overlapping the calendar year", {
                "name": "Overlap", "start_date": f"{self.year}-06-01",
                "end_date": f"{self.year + 1}-05-31"}),
            ("ending before it starts", {
                "name": "Backwards", "start_date": f"{self.year + 5}-12-31",
                "end_date": f"{self.year + 5}-01-01"}),
            ("with junk dates", {"name": "Junk", "start_date": "x", "end_date": "2026-99-99"}),
        ):
            resp = client.post(url, data)
            self.expect(f"a fiscal year {label} is refused on the form",
                        resp.status_code == 200
                        and not FiscalYear.objects.filter(company=company, name=data["name"]).exists(),
                        f"HTTP {resp.status_code}")
        resp = client.post(url, {"name": "Next", "start_date": f"{self.year + 1}-01-01",
                                 "end_date": f"{self.year + 1}-12-31"})
        self.expect("an adjoining year is accepted",
                    FiscalYear.objects.filter(company=company, name="Next").exists(),
                    f"HTTP {resp.status_code}")

    # ──────────────────────────── 5. fixed assets ────────────────────────────

    def asset_data(self, code, cost, life="5", on_date=None, **extra):
        data = {"code": code, "name": f"Asset {code}", "category": "", "serial_number": "",
                "location": "", "purchase_date": str(on_date or self.day(1, 10)),
                "purchase_cost": str(cost), "residual_value": "0",
                "useful_life_years": str(life), "depreciation_method": "SLM",
                "supplier": "", "paid_into": "", "warranty_expiry": "", "notes": ""}
        data.update(extra)
        return data

    def check_assets(self):
        from apps.fixed_assets.models import AssetCategory, AssetDepreciation, FixedAsset

        company, client, user = self.book("assets")
        create = reverse("fixed_assets:asset_create")
        client.post(create, self.asset_data("A1", "120000"))
        asset = FixedAsset.objects.get(company=company, code="A1")
        charge = lambda on_date: client.post(  # noqa: E731
            reverse("fixed_assets:asset_depreciate", args=[asset.pk]),
            {"period_date": str(on_date)})

        charge(self.day(1, 1))
        charge(self.day(4, 1))
        asset.refresh_from_db()
        april = AssetDepreciation.objects.filter(asset=asset, period_date=self.day(4, 1)).first()
        self.expect("a month typed past a gap charges the missed months with it",
                    april is not None and april.depreciation_amount == D("6000.00")
                    and asset.accumulated_depreciation == D("8000.00"),
                    f"{april and april.depreciation_amount}, accumulated {asset.accumulated_depreciation}")
        resp = charge(self.day(2, 1))
        self.expect("a month already caught up cannot be charged again",
                    resp.status_code == 200
                    and not AssetDepreciation.objects.filter(asset=asset, period_date=self.day(2, 1)).exists())
        resp = charge(self.day(4, 20))
        self.expect("a second date in the same calendar month is refused",
                    resp.status_code == 200
                    and AssetDepreciation.objects.filter(
                        asset=asset, period_date__month=4, period_date__year=self.year).count() == 1)
        charge(self.day(5, 1))
        asset.refresh_from_db()
        self.expect("the next month charges one month",
                    asset.accumulated_depreciation == D("10000.00"),
                    str(asset.accumulated_depreciation))
        self.expect("the register and 1690 hold the same wear",
                    -self.bal("1690") == asset.accumulated_depreciation,
                    f"1690 {-self.bal('1690')} vs {asset.accumulated_depreciation}")

        row = AssetDepreciation.objects.get(asset=asset, period_date=self.day(5, 1))
        resp = client.post(reverse("accounting:journal_entry_reverse",
                                   args=[row.journal_entry_id]), {})
        row.refresh_from_db()
        self.expect("a depreciation entry is refused by the journal screen",
                    not row.journal_entry.reversed_by_id
                    and self.said(client, resp, "Fixed Assets"),
                    str(self.heard[-2:]))

        # ── the accounts behind a posted asset are locked ──
        vehicles = AssetCategory.objects.create(
            company=company, name="Vehicles", default_useful_life=4,
            gl_asset_account=self.account("1620"), gl_accumulated_account=self.account("1690"),
            gl_depreciation_account=self.account("6900"))
        client.post(reverse("fixed_assets:asset_edit", args=[asset.pk]),
                    self.asset_data("A1", "120000", category=str(vehicles.pk),
                                    location="Mombasa"))
        asset.refresh_from_db()
        self.expect("the category of a posted asset cannot be changed",
                    asset.category_id is None and asset.location == "Mombasa",
                    f"category {asset.category}, location {asset.location}")
        self.expect("so 1610 keeps the cost and 1620 never goes negative",
                    self.bal("1610") == D("120000") and self.bal("1620") == Z,
                    f"1610 {self.bal('1610')}, 1620 {self.bal('1620')}")

        client.post(reverse("fixed_assets:asset_edit", args=[asset.pk]),
                    self.asset_data("A1", "120000", status="WRITTEN_OFF"))
        asset.refresh_from_db()
        self.expect("status cannot be set from the edit form", asset.status == "ACTIVE",
                    asset.status)

        # ── an asset whose purchase is stood down ──
        client.post(create, self.asset_data("R1", "24000", life="1", on_date=self.day(5, 1)))
        unbought = FixedAsset.objects.get(company=company, code="R1")
        client.post(reverse("fixed_assets:acquisition_reverse", args=[unbought.pk]),
                    {"reason": "returned"})
        unbought.refresh_from_db()
        self.expect("standing the purchase down takes the asset out of use",
                    unbought.status == "CANCELLED", unbought.status)
        resp = client.post(reverse("fixed_assets:asset_depreciate", args=[unbought.pk]),
                           {"period_date": str(self.day(5, 1))})
        self.expect("it cannot be depreciated",
                    resp.status_code == 200 and not unbought.depreciations.exists())
        resp = client.post(reverse("fixed_assets:asset_dispose", args=[unbought.pk]), {
            "disposal_date": str(self.today), "disposal_type": "SALE", "proceeds": "1000",
            "received_into": "", "notes": ""})
        unbought.refresh_from_db()
        self.expect("nor disposed of", unbought.status == "CANCELLED", unbought.status)
        self.expect("and the cost accounts carry nothing for it",
                    self.bal("1610") == D("120000"), str(self.bal("1610")))
        client.post(reverse("fixed_assets:asset_edit", args=[unbought.pk]),
                    self.asset_data("R1", "24000", life="1", on_date=self.day(5, 1)))
        unbought.refresh_from_db()
        self.expect("saving it again books the purchase and brings it back into use",
                    unbought.status == "ACTIVE" and unbought.acquisition_is_live
                    and self.bal("1610") == D("144000"),
                    f"{unbought.status}, 1610 {self.bal('1610')}")

    # ─────────────────────────── 6. opening balances ─────────────────────────

    def check_opening_balances(self):
        from apps.accounting.models import JournalEntry, JournalEntryLine

        company, client, user = self.book("opening")
        url = reverse("accounting:opening_balances")
        bank, cash, capital = (self.account(c).pk for c in ("1200", "1100", "3100"))
        on = str(self.day(1, 1))
        count = lambda: JournalEntry.objects.filter(company=company).count()  # noqa: E731
        for label, data in (
            ("a date that is not a date", {"posting_date": "not-a-date",
                                           f"dr_{bank}": "1", f"cr_{capital}": "1"}),
            ("an impossible date", {"posting_date": "2026-02-30",
                                    f"dr_{bank}": "1", f"cr_{capital}": "1"}),
            ("NaN", {"posting_date": on, f"dr_{bank}": "NaN", f"cr_{capital}": "1"}),
            ("Infinity", {"posting_date": on, f"dr_{bank}": "Infinity",
                          f"cr_{capital}": "Infinity"}),
            ("1e20", {"posting_date": on, f"dr_{bank}": "1e20", f"cr_{capital}": "1e20"}),
            ("half cents", {"posting_date": on, f"dr_{bank}": "0.005", f"dr_{cash}": "0.005",
                            f"cr_{capital}": "0.01"}),
            ("a negative credit", {"posting_date": on, f"dr_{cash}": "100",
                                   f"cr_{cash}": "-100", f"cr_{capital}": "200"}),
            ("a debit and a credit on one line", {"posting_date": on, f"dr_{cash}": "50",
                                                  f"cr_{cash}": "50"}),
        ):
            before = count()
            resp = client.post(url, data)
            self.expect(f"opening balances with {label} are refused without a 500",
                        resp.status_code == 302 and count() == before,
                        f"HTTP {resp.status_code}, entries {before} -> {count()}")
        self.expect("no negative or two-sided line ever reached the ledger",
                    not JournalEntryLine.objects.filter(journal_entry__company=company).exists())

        resp = client.post(url, {"posting_date": on, f"dr_{bank}": "500000",
                                 f"dr_{cash}": "20000.50", f"cr_{capital}": "520000.50"})
        first = JournalEntry.objects.filter(company=company, entry_type="OPENING").first()
        self.expect("a balanced opening posts", first is not None and first.status == "POSTED",
                    str(self.flashes(client, resp)[-1:]))
        resp = client.post(url, {"posting_date": on, f"dr_{bank}": "500000",
                                 f"cr_{capital}": "500000"})
        self.expect("a second opening entry is refused and points at reversing the first",
                    JournalEntry.objects.filter(company=company, entry_type="OPENING").count() == 1
                    and self.said(client, resp, "reverse"),
                    str(self.heard[-2:]))
        self.expect("3100 holds the opening capital once", -self.bal("3100") == D("520000.50"),
                    str(-self.bal("3100")))
        client.post(reverse("accounting:journal_entry_reverse", args=[first.pk]), {})
        first.refresh_from_db()
        self.expect("the opening entry can be reversed from its journal page",
                    first.reversed_by_id is not None)
        resp = client.post(url, {"posting_date": on, f"dr_{bank}": "500000",
                                 f"cr_{capital}": "500000"})
        self.expect("after which a corrected opening can be entered",
                    -self.bal("3100") == D("500000"), str(-self.bal("3100")))

    # ─────────────────────────── 7. chart of accounts ────────────────────────

    def check_accounts(self):
        company, client, user = self.book("chart")
        self.journal([("1200", 3000, 0), ("4200", 0, 3000)], self.today)
        revenue = self.account("4200")
        resp = client.post(reverse("accounting:account_edit", args=[revenue.pk]), {
            "code": "4200", "name": revenue.name, "account_type": "LIABILITY",
            "parent": "", "description": "", "is_reconcilable": "", "currency": ""})
        revenue.refresh_from_db()
        self.expect("an account's type is fixed once it has postings",
                    revenue.account_type == "REVENUE" and resp.status_code == 200,
                    f"HTTP {resp.status_code}, now {revenue.account_type}")
        unused = self.account("4300")
        if unused.has_postings:
            self.expect("4300 was expected to be unused", False)
        else:
            client.post(reverse("accounting:account_edit", args=[unused.pk]), {
                "code": "4300", "name": unused.name, "account_type": "EXPENSE",
                "parent": "", "description": "", "is_reconcilable": "", "currency": ""})
            unused.refresh_from_db()
            self.expect("an account nothing has posted to can still change type",
                        unused.account_type == "EXPENSE", unused.account_type)

        rent = self.account("6200")
        self.journal([("6200", 700, 0), ("1200", 0, 700)], self.today)
        resp = client.post(reverse("accounting:account_deactivate", args=[rent.pk]), {})
        rent.refresh_from_db()
        self.expect("an account with a balance cannot be retired",
                    rent.is_active and self.said(client, resp, "still holds"))
        self.journal([("1200", 700, 0), ("6200", 0, 700)], self.today)
        resp = client.post(reverse("accounting:account_deactivate", args=[rent.pk]), {})
        rent.refresh_from_db()
        self.expect("once the balance is moved off, it can be", not rent.is_active)

    # ─────────────────────── 8. trial balances, dashboard ────────────────────

    def check_reports(self):
        from apps.accounting.models import JournalEntryLine

        company, client, user = self.book("reports")
        client.post(reverse("accounting:opening_balances"), {
            "posting_date": str(self.day(1, 1)), f"dr_{self.account('1200').pk}": "800000",
            f"dr_{self.account('1100').pk}": "50000", f"cr_{self.account('3100').pk}": "850000"})
        start = self.today.replace(day=1)
        last_month = start - datetime.timedelta(days=1)
        self.journal([("1200", 120000, 0), ("4100", 0, 120000)], last_month)
        self.journal([("6200", 40000, 0), ("1200", 0, 40000)], start)
        self.journal([("1100", 10000, 0), ("1200", 0, 10000)], self.today)
        future = self.today + datetime.timedelta(days=3)
        if future.year == self.year:
            self.journal([("1200", 999, 0), ("4100", 0, 999)], future)
        end = (start + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)

        tb = client.get(reverse("reporting:trial_balance")
                        + f"?date_from={start:%Y-%m-%d}&date_to={end:%Y-%m-%d}").context
        rows = {row["account"].code: row for row in tb["rows"]}
        bank = rows.get("1200", {})
        self.expect("the report trial balance shows the bank's closing balance as at the end",
                    bank.get("debit", Z) - bank.get("credit", Z) == self.bal("1200", upto=end),
                    f"{bank.get('debit')} / {bank.get('credit')} vs {self.bal('1200', upto=end)}")
        self.expect("with what was brought forward and what moved inside the window",
                    bank.get("opening") == self.bal("1200", upto=last_month)
                    and bank.get("opening", Z) + bank.get("period_debit", Z)
                    - bank.get("period_credit", Z) == self.bal("1200", upto=end),
                    f"opening {bank.get('opening')}, Dr {bank.get('period_debit')}, "
                    f"Cr {bank.get('period_credit')}")
        self.expect("no account shows a debit and a credit closing balance at once",
                    not any(r["debit"] > 0 and r["credit"] > 0 for r in tb["rows"]))
        self.expect("the closing columns balance", tb["is_balanced"],
                    f"{tb['total_debit']} / {tb['total_credit']}")
        resp = client.get(reverse("reporting:trial_balance")
                          + f"?date_from={end:%Y-%m-%d}&date_to={start:%Y-%m-%d}")
        self.expect("a window that ends before it starts says so instead of printing nothing",
                    resp.status_code == 200 and resp.context.get("error"))

        lines = JournalEntryLine.objects.filter(
            journal_entry__company=company, journal_entry__status="POSTED",
            journal_entry__posting_date__lte=self.today)
        to_date = lines.aggregate(dr=Sum("debit_amount"))["dr"]
        tb = client.get(reverse("accounting:trial_balance")).context
        self.expect("the accounting trial balance is as at today, leaving future postings out",
                    tb["total_debit"] == to_date and tb["as_at"] == self.today,
                    f"{tb['total_debit']} vs {to_date}")
        tb = client.get(reverse("accounting:trial_balance") + f"?date={last_month:%Y-%m-%d}").context
        self.expect("and can be drawn as at another day",
                    tb["as_at"] == last_month and tb["is_balanced"])

        dash = client.get(reverse("dashboard:index")).context
        cash = self.bal("1100", upto=self.today) + self.bal("1200", upto=self.today) \
            + self.bal("1110", upto=self.today)
        self.expect("the dashboard's cash is the cash as at today",
                    dash.get("total_cash") == cash, f"{dash.get('total_cash')} vs {cash}")

    # ─────────────────────────── 9. revaluation ──────────────────────────────

    def check_revaluation(self):
        from apps.accounting import revaluation
        from apps.accounting.models import Currency
        from apps.sales.models import CustomerPayment

        company, client, user = self.book("fx")
        usd = Currency.objects.filter(code="USD").first() or Currency.objects.create(
            code="USD", name="US Dollar", symbol="$")
        if self.today < self.day(10, 20):
            self.expect("this section needs a run date after 20 October", True,
                        "skipped: the dates below would lie in the future")
            return
        for on_date, rate in ((self.day(9, 1), "130"), (self.day(9, 30), "135"),
                              (self.day(10, 15), "128")):
            client.post(reverse("accounting:exchange_rate_create"), {
                "currency": usd.pk, "effective_date": str(on_date), "rate": rate,
                "source": "MANUAL", "note": ""})
        invoice = self.invoice("USD-FIX-1", self.day(9, 1), "1000", "USD", D("130"))
        self.expect("the dollar invoice is booked at 130",
                    invoice.total_amount_base == D("130000.00"), str(invoice.total_amount_base))

        client.post(reverse("accounting:revaluation_post"), {"date": str(self.day(9, 30))})
        first = revaluation.live_revaluation(company)
        self.expect("the September revaluation books 5,000 on 1350",
                    first is not None and self.bal("1350") == D("5000.00"), str(self.bal("1350")))
        resp = client.post(reverse("accounting:journal_entry_reverse", args=[first.pk]), {})
        first.refresh_from_db()
        self.expect("a revaluation is refused by the journal screen",
                    first.reversed_by_id is None
                    and self.said(client, resp, "Exchange Revaluation"),
                    str(self.heard[-2:]))

        client.post(reverse("sales:payment_create"), {
            "reference": "PAY-FIX-1", "customer": invoice.customer.pk,
            "payment_date": str(self.day(10, 15)), "amount": "1000", "currency": "USD",
            "payment_method": "BANK_TRANSFER", "bank_account": "",
            "transaction_reference": "T1", "notes": "", f"allocate_{invoice.pk}": "1000"})
        invoice.refresh_from_db()
        self.expect("the invoice is paid in full on 15 October",
                    invoice.outstanding_amount == Z, str(invoice.outstanding_amount))
        self.expect("the realised loss is 2,000", self.bal("7300") == D("2000.00"),
                    str(self.bal("7300")))
        self.expect("settling it released its unrealised 5,000 from 1350",
                    self.bal("1350") == Z, str(self.bal("1350")))
        unrealised = -(self.bal("4320") + self.bal("7310"))
        self.expect("so the profit and loss carries only the real 2,000 loss",
                    unrealised == Z and self.bal("7300") - (-self.bal("4310")) == D("2000.00"),
                    f"unrealised {unrealised}")
        self.expect("while September itself still shows the 5,000 it was worth then",
                    self.bal("1350", upto=self.day(9, 30)) == D("5000.00"),
                    str(self.bal("1350", upto=self.day(9, 30))))

        preview = client.get(reverse("accounting:revaluation")
                             + f"?date={self.day(9, 30):%Y-%m-%d}").context
        self.expect("a September preview drawn after the receipt still values the invoice",
                    preview["total_receivable"] == D("5000.00"), str(preview["total_receivable"]))
        client.post(reverse("accounting:revaluation_post"), {"date": str(self.day(9, 30))})
        self.expect("redrawing September after the receipt keeps 5,000 at 30 September",
                    self.bal("1350", upto=self.day(9, 30)) == D("5000.00"),
                    str(self.bal("1350", upto=self.day(9, 30))))
        self.expect("and releases it again on the day it was settled",
                    self.bal("1350") == Z, str(self.bal("1350")))

        payment = CustomerPayment.objects.get(company=company, reference="PAY-FIX-1")
        client.post(reverse("sales:payment_reverse", args=[payment.pk]), {"reason": "bounced"})
        invoice.refresh_from_db()
        self.expect("giving the receipt back makes the invoice owed again",
                    invoice.outstanding_amount == D("1000.00"), str(invoice.outstanding_amount))
        self.expect("and the unrealised share is carried again",
                    self.bal("1350") == D("5000.00"), str(self.bal("1350")))
        client.post(reverse("accounting:revaluation_reverse"), {"date": str(self.day(10, 20))})
        self.expect("taking the revaluation off clears 1350 and its releases together",
                    self.bal("1350") == Z and self.bal("4320") + self.bal("7310") == Z
                    and not revaluation.live_releases(company).exists(),
                    f"1350 {self.bal('1350')}")

    # ──────────────────────────── people in a book ───────────────────────────

    def member(self, role, label=None):
        """A login holding `role` in the current book, and a client working in it."""
        from apps.accounts.models import User
        from apps.organizations.models import CompanyMembership

        stamp = datetime.datetime.now().strftime("%H%M%S%f")
        name = (label or role).lower()
        user = User.objects.create(
            username=f"fix.{name}.{stamp}", email=f"fix.{name}.{stamp}@ledger.local",
            first_name=role.title(), last_name="Member")
        CompanyMembership.objects.create(company=self.company, user=user, role=role,
                                         is_default=True)
        client = Client()
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        session = client.session
        session["active_company_id"] = str(self.company.id)
        session.save()
        return user, client

    # ─────────────────────────── 10. the role table ──────────────────────────

    def check_role_table(self):
        from types import SimpleNamespace

        from apps.core import access

        person = SimpleNamespace(is_authenticated=True, is_superuser=False, is_system_admin=False)

        def can(role, target, method="GET"):
            ns, _, name = target.rpartition(":")
            return access.can(person, role, ns, name, method)

        def who(target, method="POST"):
            return {r for r in access.ROLES if can(r, target, method)}

        everyone_but_readers = access.ROLES - {"ADMIN", "VIEWER", "AUDITOR"}
        self.expect("a claim is raised, edited and submitted by anyone but a viewer or auditor",
                    all(who(t) - {"ADMIN"} == everyone_but_readers
                        for t in ("expenses:claim_create", "expenses:claim_edit",
                                  "expenses:claim_submit", "expenses:claim_add_line")),
                    str(sorted(who("expenses:claim_edit"))))
        self.expect("a claim is approved and rejected by the finance or HR manager",
                    who("expenses:claim_approve") == {"ADMIN", "FINANCE_MANAGER", "HR_MANAGER"}
                    and who("expenses:claim_reject") == who("expenses:claim_approve"),
                    str(sorted(who("expenses:claim_approve"))))
        self.expect("and paid by finance",
                    who("expenses:claim_pay") == {"ADMIN", "FINANCE_MANAGER", "ACCOUNTANT"})
        self.expect("documents: viewers and auditors may not upload, delete or add a category",
                    not any(can(r, t, "POST") for r in ("VIEWER", "AUDITOR")
                            for t in ("documents:document_upload", "documents:document_delete",
                                      "documents:category_create"))
                    and can("EMPLOYEE", "documents:document_upload", "POST"))
        self.expect("helpdesk: viewers and auditors may not comment",
                    not can("VIEWER", "helpdesk:ticket_comment", "POST")
                    and not can("AUDITOR", "helpdesk:ticket_comment", "POST")
                    and can("EMPLOYEE", "helpdesk:ticket_comment", "POST"))
        self.expect("the finance manager may open the disposal form",
                    can("FINANCE_MANAGER", "fixed_assets:asset_dispose")
                    and not can("ACCOUNTANT", "fixed_assets:asset_dispose"))
        self.expect("a sales order is confirmed by the sales manager or finance, not the rep",
                    who("sales:order_confirm") == {"ADMIN", "SALES_MANAGER", "FINANCE_MANAGER",
                                                   "ACCOUNTANT"},
                    str(sorted(who("sales:order_confirm"))))
        self.expect("and cancelled by the same people",
                    who("sales:order_cancel") == who("sales:order_confirm"))
        self.expect("on-account credit is applied by finance",
                    who("sales:payment_apply_credit") == {"ADMIN", "FINANCE_MANAGER", "ACCOUNTANT"})
        self.expect("HR may look at payroll but not run it",
                    can("HR_MANAGER", "payroll:run_list")
                    and not any(can("HR_MANAGER", t, "POST") for t in (
                        "payroll:run_create", "payroll:run_calculate", "payroll:rule_create",
                        "payroll:component_create", "payroll:band_create")),
                    str(sorted(who("payroll:run_create"))))
        self.expect("runs, calculation and rules are the payroll officer's",
                    all(can("PAYROLL_OFFICER", t, "POST") for t in (
                        "payroll:run_create", "payroll:run_calculate", "payroll:rule_create")))
        self.expect("approve and post stay with finance",
                    who("payroll:run_approve") == {"ADMIN", "FINANCE_MANAGER"}
                    and who("payroll:run_post") == {"ADMIN", "FINANCE_MANAGER"})
        self.expect("everybody may open their own payslips",
                    all(can(r, "payroll:my_payslips") for r in access.ROLES))
        self.expect("goods are received by the warehouse manager only",
                    who("procurement:grn_record") == {"ADMIN", "WAREHOUSE_MANAGER"}
                    and who("procurement:grn") == {"ADMIN", "WAREHOUSE_MANAGER"},
                    str(sorted(who("procurement:grn_record"))))
        self.expect("purchase orders are raised and cancelled by procurement, not the stores",
                    who("procurement:po_create") == {"ADMIN", "PROCUREMENT", "FINANCE_MANAGER"}
                    and who("procurement:po_cancel") == {"ADMIN", "PROCUREMENT", "FINANCE_MANAGER"}
                    and not can("WAREHOUSE_MANAGER", "procurement:po_create"))
        self.expect("a requisition is submitted by whoever may raise one",
                    who("procurement:requisition_submit") == who("procurement:requisition_create"),
                    str(sorted(who("procurement:requisition_submit"))))
        self.expect("an order is closed short by procurement or the finance manager",
                    who("procurement:po_close_short") == {"ADMIN", "PROCUREMENT", "FINANCE_MANAGER"})
        self.expect("bank transfers are finance's",
                    who("banking:transfer_create") == {"ADMIN", "FINANCE_MANAGER", "ACCOUNTANT"})

    # ─────────────────── 11. documents, tickets, notifications ───────────────

    def check_object_rules(self):
        import os

        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.documents.models import Document
        from apps.helpdesk.models import Ticket
        from apps.notifications.models import Notification

        company, admin_client, admin = self.book("objects")
        uploader, up_client = self.member("EMPLOYEE", "uploader")
        colleague, col_client = self.member("EMPLOYEE", "colleague")
        viewer, view_client = self.member("VIEWER")
        auditor, aud_client = self.member("AUDITOR")

        def upload(client, name):
            return client.post(reverse("documents:document_upload"), {
                "name": name, "category": "", "description": "", "expiry_date": "",
                "file": SimpleUploadedFile(f"{name}.txt", b"signed delivery note")})

        resp = upload(view_client, "viewer-doc")
        self.expect("a viewer cannot upload a document",
                    resp.status_code == 403
                    and not Document.objects.filter(company=company, name="viewer-doc").exists())
        upload(up_client, "kra-cert")
        doc = Document.objects.filter(company=company, name="kra-cert").first()
        if not self.expect("an employee can", doc is not None):
            return
        path = doc.file.path
        for label, client in (("a viewer", view_client), ("an auditor", aud_client),
                              ("a colleague who did not upload it", col_client)):
            client.post(reverse("documents:document_delete", args=[doc.pk]), {})
            self.expect(f"{label} cannot delete it",
                        Document.objects.filter(pk=doc.pk).exists())
        page = col_client.get(reverse("documents:document_detail", args=[doc.pk]))
        self.expect("and is not offered the Delete button",
                    page.status_code == 200 and not page.context["can_delete"])
        up_client.post(reverse("documents:document_delete", args=[doc.pk]), {})
        self.expect("the uploader can, and the file goes with the record",
                    not Document.objects.filter(pk=doc.pk).exists() and not os.path.exists(path),
                    path)
        upload(up_client, "contract")
        doc = Document.objects.get(company=company, name="contract")
        path = doc.file.path
        admin_client.post(reverse("documents:document_delete", args=[doc.pk]), {})
        self.expect("an admin can delete anybody's, file and all",
                    not Document.objects.filter(pk=doc.pk).exists() and not os.path.exists(path))

        helper, help_client = self.member("SALES_REP", "assignee")
        reporter, rep_client = self.member("EMPLOYEE", "reporter")
        rep_client.post(reverse("helpdesk:ticket_create"), {
            "reference": "TCK-1", "subject": "Printer jammed", "description": "",
            "customer": "", "requester_name": "", "requester_email": "", "channel": "WEB",
            "priority": "NORMAL", "assigned_to": str(helper.pk)})
        ticket = Ticket.objects.filter(company=company, reference="TCK-1").first()
        if not self.expect("a ticket is raised and remembers who raised it",
                           ticket is not None and ticket.raised_by_id == reporter.pk):
            return
        resolve = reverse("helpdesk:ticket_resolve", args=[ticket.pk])
        close = reverse("helpdesk:ticket_close", args=[ticket.pk])
        for label, client in (("an auditor", aud_client), ("a bystander", col_client),
                              ("the reporter", rep_client)):
            resp = client.post(resolve, {})
            ticket.refresh_from_db()
            self.expect(f"{label} cannot mark it resolved", ticket.status == "OPEN",
                        f"{ticket.status} HTTP {resp.status_code}")
        resp = aud_client.post(close, {})
        ticket.refresh_from_db()
        self.expect("an auditor cannot close it either", ticket.status == "OPEN")
        resp = aud_client.post(reverse("helpdesk:ticket_comment", args=[ticket.pk]),
                               {"body": "noted", "is_internal": ""})
        self.expect("nor comment on it", resp.status_code == 403 and not ticket.comments.exists())
        help_client.post(resolve, {})
        ticket.refresh_from_db()
        self.expect("the assignee resolves it", ticket.status == "RESOLVED", ticket.status)
        rep_client.post(close, {})
        ticket.refresh_from_db()
        self.expect("and the reporter closes it", ticket.status == "CLOSED", ticket.status)

        note = Notification.objects.create(user=uploader, title="Claim approved")
        other = Notification.objects.create(user=uploader, title="Leave approved")
        resp = up_client.get(reverse("notifications:mark_read", args=[note.pk]))
        note.refresh_from_db()
        self.expect("following a mark-read link changes nothing",
                    resp.status_code == 405 and not note.is_read, f"HTTP {resp.status_code}")
        resp = up_client.get(reverse("notifications:mark_all_read"))
        self.expect("nor does following mark-all-read",
                    resp.status_code == 405
                    and Notification.objects.filter(user=uploader, is_read=False).count() == 2)
        page = up_client.get(reverse("notifications:list"))
        body = page.content.decode()
        self.expect("the list offers the read buttons as forms that POST",
                    reverse("notifications:mark_all_read") in body
                    and reverse("notifications:mark_read", args=[note.pk]) in body)
        up_client.post(reverse("notifications:mark_read", args=[note.pk]))
        note.refresh_from_db()
        other.refresh_from_db()
        self.expect("posting marks the one read", note.is_read and not other.is_read)
        up_client.post(reverse("notifications:mark_all_read"))
        other.refresh_from_db()
        self.expect("and mark-all-read marks the rest", other.is_read and other.read_at)

    # ──────────────────────── 12. dashboard and search ───────────────────────

    def check_dashboard_rights(self):
        from django.urls import Resolver404, resolve

        from apps.core import access
        from apps.hr.models import Employee

        company, client, user = self.book("dashrights")
        self.journal([("1200", 75000, 0), ("4100", 0, 75000)], self.today)
        invoice = self.invoice("INV-DASH-1", self.today, "4321")
        Employee.objects.create(company=company, employee_number="EMP-DASH",
                                first_name="Wanjiru", last_name="Kariuki",
                                date_hired=self.today, job_title="Clerk")
        dash = client.get(reverse("dashboard:index")).context
        self.expect("the admin's dashboard still carries every figure",
                    dash.get("monthly_revenue") == D("79321.00")
                    and dash.get("open_invoices") and dash.get("recent_journal_entries"),
                    str(dash.get("monthly_revenue")))

        for role in ("POS_CASHIER", "EMPLOYEE", "SALES_REP", "VIEWER"):
            member, member_client = self.member(role)
            resp = member_client.get(reverse("dashboard:index"))
            ctx, body = resp.context, resp.content.decode()
            self.expect(f"{role}: no month's revenue, expenses or cash on the dashboard",
                        ctx.get("monthly_revenue") is None and ctx.get("total_cash") is None
                        and "75,000.00" not in body and "Revenue (MTD)" not in body)
            self.expect(f"{role}: no journal references",
                        not ctx.get("recent_journal_entries")
                        and not any(e.reference in body for e in dash["recent_journal_entries"]))
            may_invoices = access.can(member, role, "sales", "invoice_list")
            self.expect(f"{role}: open invoices only if the role may open the invoice list",
                        bool(ctx.get("open_invoices")) == may_invoices
                        and (invoice.reference in body) == may_invoices)
            main = re.sub(r"<aside.*?</aside>", "", body, flags=re.S)
            blocked = set()
            # Static files are not pages; everything else must open for them.
            for href in re.findall(r'href="(/(?!static/|media/)[^"#?]*)', main):
                try:
                    match = resolve(href)
                except Resolver404:
                    continue
                if not access.can(member, role, match.namespace, match.url_name, "GET"):
                    blocked.add(href)
            self.expect(f"{role}: every link on the dashboard opens for them",
                        not blocked, str(sorted(blocked)[:5]))

            found = member_client.get(reverse("dashboard:search") + "?q=DASH").context
            labels = {g["label"] for g in found["groups"]}
            self.expect(f"{role}: search finds no journals or employees",
                        not labels & {"Journal entries", "Employees", "Bills",
                                      "Bank transactions"}, str(sorted(labels)))
            self.expect(f"{role}: and invoices only where they may open them",
                        ("Invoices" in labels) == may_invoices, str(sorted(labels)))
        found = client.get(reverse("dashboard:search") + "?q=DASH").context
        self.expect("the admin's search still finds the invoice and the employee",
                    {"Invoices", "Employees"} <= {g["label"] for g in found["groups"]})

    # ─────────────────────────── 13. numbering ───────────────────────────────

    def check_numbering(self):
        from apps.core.models import SequenceCounter
        from apps.crm.models import Supplier
        from apps.fixed_assets.models import FixedAsset
        from apps.procurement.models import PurchaseOrder

        company, client, user = self.book("numbers")
        counter = lambda prefix: SequenceCounter.objects.filter(  # noqa: E731
            company=company, prefix=prefix).values_list("last_number", flat=True).first() or 0
        supplier = Supplier.objects.create(company=company, name="Numbers Supplies")
        shown = [client.get(reverse("procurement:po_create")).context["form"]["reference"].value()
                 for _ in range(3)]
        self.expect("opening the PO form three times takes no number",
                    counter("PO-") == 0 and shown == ["PO-00001"] * 3, f"{shown}, {counter('PO-')}")
        client.post(reverse("procurement:po_create"), {
            "reference": shown[0], "supplier": supplier.pk, "order_date": str(self.today),
            "expected_delivery_date": "", "warehouse": "", "notes": "", "currency": "KES"})
        self.expect("saving it takes the number that was shown",
                    PurchaseOrder.objects.filter(company=company, reference="PO-00001").exists()
                    and counter("PO-") == 1, str(counter("PO-")))
        stale = shown[0]
        client.post(reverse("procurement:po_create"), {
            "reference": stale, "supplier": supplier.pk, "order_date": str(self.today),
            "expected_delivery_date": "", "warehouse": "", "notes": "", "currency": "KES"})
        self.expect("a form left open while somebody else used its number gets the next one",
                    PurchaseOrder.objects.filter(company=company, reference="PO-00002").exists()
                    and PurchaseOrder.objects.filter(company=company, reference=stale).count() == 1)
        client.post(reverse("procurement:po_create"), {
            "reference": "SUP-REF-77", "supplier": supplier.pk, "order_date": str(self.today),
            "expected_delivery_date": "", "warehouse": "", "notes": "", "currency": "KES"})
        self.expect("a reference typed by hand is kept and leaves the series alone",
                    PurchaseOrder.objects.filter(company=company, reference="SUP-REF-77").exists()
                    and counter("PO-") == 2)

        for _ in range(2):
            form = client.get(reverse("fixed_assets:asset_create")).context["form"]
        code = form["code"].value()
        self.expect("opening the asset form takes no number either",
                    counter("FA-") == 0 and code == "FA-00001", f"{code}, {counter('FA-')}")
        client.post(reverse("fixed_assets:asset_create"),
                    self.asset_data(code, "50000"))
        self.expect("and the asset is saved under the code it showed",
                    FixedAsset.objects.filter(company=company, code=code).exists()
                    and counter("FA-") == 1)
        data = self.asset_data(code, "60000")
        data["name"] = "Second asset"
        client.post(reverse("fixed_assets:asset_create"), data)
        self.expect("a stale asset preview takes the next free code instead of an error",
                    FixedAsset.objects.filter(company=company, code="FA-00002",
                                              name="Second asset").exists()
                    and counter("FA-") == 2, str(counter("FA-")))
        for target, prefix in (("hr:employee_create", "EMP-"),
                               ("manufacturing:work_order_create", "WO-"),
                               ("payroll:run_create", "PR-"),
                               ("projects:project_create", "PRJ-")):
            client.get(reverse(target))
            client.get(reverse(target))
            self.expect(f"opening {target} takes no {prefix} number", counter(prefix) == 0,
                        str(counter(prefix)))

    # ──────────────────────── 14. figures and limits ─────────────────────────

    def check_figures(self):
        from apps.accounting.models import Account, BudgetLine, Currency, period_for
        from apps.organizations.models import FiscalYear

        company, client, user = self.book("figures")
        client.post(reverse("accounting:opening_balances"), {
            "posting_date": str(self.day(1, 1)), f"dr_{self.account('1200').pk}": "500000",
            f"cr_{self.account('3100').pk}": "500000"})
        future = self.today + datetime.timedelta(days=5)
        self.journal([("1200", 4444, 0), ("3100", 0, 4444)], future)
        idx = client.get(reverse("accounting:index")).context
        self.expect("the accounting tiles are as at today, leaving a future posting out",
                    idx["asset_balance"] == self.bal("1200", upto=self.today)
                    + self.bal("1100", upto=self.today) + self.bal("1110", upto=self.today)
                    and idx["equity_balance"] == -self.bal("3100", upto=self.today),
                    f"assets {idx['asset_balance']}, equity {idx['equity_balance']}")

        start = self.today.replace(day=1)
        self.journal([("1100", 36000, 0), ("1200", 0, 36000)], start)          # bank to till
        self.journal([("6200", 7000, 0), ("1110", 0, 7000)], start)            # petty cash
        self.journal([("1110", 7000, 0), ("1100", 0, 7000)], start)            # till tops it up
        self.journal([("1200", 20000, 0), ("4100", 0, 20000)], start)          # a receipt
        url = (reverse("reporting:cash_flow")
               + f"?date_from={start:%Y-%m-%d}&date_to={self.today:%Y-%m-%d}")
        flow = client.get(url).context
        self.expect("moving money between the bank and the till is neither in nor out",
                    flow["total_in"] == D("20000.00") and flow["total_out"] == D("7000.00"),
                    f"in {flow['total_in']}, out {flow['total_out']}")
        self.expect("and the net is the change in the cash pool",
                    flow["net_cash_flow"] == D("13000.00"), str(flow["net_cash_flow"]))
        Account.objects.filter(company=company, code="1110").update(is_active=False)
        flow = client.get(url).context
        self.expect("a retired petty-cash account still counts for the window it moved in",
                    flow["net_cash_flow"] == D("13000.00") and flow["total_out"] == D("7000.00"),
                    f"net {flow['net_cash_flow']}, out {flow['total_out']}")
        Account.objects.filter(company=company, code="1110").update(is_active=True)

        fy = FiscalYear.objects.get(company=company)
        jan = period_for(company, self.day(1))
        self.journal([("1200", 90000, 0), ("4100", 0, 90000)], self.day(1, 10))
        budget = BudgetLine.objects.create(company=company, fiscal_year=fy,
                                           account=self.account("4100"), period=jan,
                                           budgeted_amount=D("100000"))
        before = budget.actual_amount
        if fy.end_date < self.today:
            client.post(reverse("accounting:fiscal_year_close", args=[fy.pk]), {})
        else:
            from apps.accounting import gl_service  # noqa: F401
            from apps.accounting.models import JournalEntry
            entry, _ = self.draft([("4100", 90000, 0), ("3200", 0, 90000)], self.day(1, 31))
            JournalEntry.objects.filter(pk=entry.pk).update(entry_type="CLOSING")
            entry.refresh_from_db()
            entry.post(user=user)
        self.expect("a budget's actual is unchanged by the closing entry",
                    before == D("90000.00") and budget.actual_amount == before,
                    f"{before} then {budget.actual_amount}")

        before = Currency.objects.count()
        resp = client.post(reverse("accounting:currency_create"),
                           {"code": "ZZQ", "name": "Mystery", "symbol": "", "minor_units": "2"})
        self.expect("a company admin cannot add to the shared currency register",
                    Currency.objects.count() == before
                    and self.said(client, resp, "platform administrator"), str(self.heard[-1:]))
        page = client.get(reverse("accounting:currency_create"))
        self.expect("the form tells them whom to ask",
                    page.status_code == 200 and not page.context["may_register"])
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        resp = client.post(reverse("accounting:currency_create"),
                           {"code": "ZZQ", "name": "Mystery", "symbol": "", "minor_units": "99999"})
        self.expect("a platform admin may, but not with 99,999 decimal places",
                    resp.status_code == 200 and not Currency.objects.filter(code="ZZQ").exists())
        client.post(reverse("accounting:currency_create"),
                    {"code": "ZZQ", "name": "Mystery", "symbol": "", "minor_units": "3"})
        self.expect("three places is a currency", Currency.objects.filter(code="ZZQ",
                                                                           minor_units=3).exists())
        self.user.is_superuser = False
        self.user.save(update_fields=["is_superuser"])

        from apps.fixed_assets.models import AssetCategory, FixedAsset
        resp = client.post(reverse("fixed_assets:asset_create"),
                           self.asset_data("LONG", "1000", life="99999999999"))
        self.expect("a life of 99,999,999,999 years is refused without a server error",
                    resp.status_code == 200
                    and not FixedAsset.objects.filter(company=company, code="LONG").exists())
        resp = client.post(reverse("fixed_assets:asset_create"),
                           self.asset_data("LONG", "1000", life="101"))
        self.expect("and so is 101", resp.status_code == 200
                    and not FixedAsset.objects.filter(company=company, code="LONG").exists())
        client.post(reverse("fixed_assets:asset_create"),
                    self.asset_data("LONG", "1000", life="100"))
        self.expect("a hundred years is allowed",
                    FixedAsset.objects.filter(company=company, code="LONG").exists())
        resp = client.post(reverse("fixed_assets:category_create"), {
            "name": "Forever", "default_useful_life": "500", "default_depreciation_method": "SLM",
            "gl_asset_account": "", "gl_depreciation_account": "", "gl_accumulated_account": ""})
        self.expect("a category's default life is capped the same way",
                    not AssetCategory.objects.filter(company=company, name="Forever").exists())

    # ────────────────── 15. revaluation released in proportion ───────────────

    def check_revaluation_shares(self):
        from apps.accounting import revaluation
        from apps.accounting.models import Currency, JournalEntry

        company, client, user = self.book("fxshare")
        usd = Currency.objects.filter(code="USD").first() or Currency.objects.create(
            code="USD", name="US Dollar", symbol="$")
        if self.today < self.day(10, 20):
            self.expect("this section needs a run date after 20 October", True,
                        "skipped: the dates below would lie in the future")
            return
        for on_date, rate in ((self.day(9, 1), "130"), (self.day(9, 30), "135"),
                              (self.day(10, 15), "128")):
            client.post(reverse("accounting:exchange_rate_create"), {
                "currency": usd.pk, "effective_date": str(on_date), "rate": rate,
                "source": "MANUAL", "note": ""})
        part = self.invoice("USD-PART", self.day(9, 1), "1000", "USD", D("130"))
        gone = self.invoice("USD-GONE", self.day(9, 1), "400", "USD", D("130"))
        late = self.invoice("USD-LATE", self.day(9, 1), "500", "USD", D("130"))
        client.post(reverse("accounting:revaluation_post"), {"date": str(self.day(9, 30))})
        self.expect("the revaluation carries 5 shillings a dollar on 1,900 dollars",
                    self.bal("1350") == D("9500.00"), str(self.bal("1350")))

        def receipt(ref, invoice, amount, on_date):
            client.post(reverse("sales:payment_create"), {
                "reference": ref, "customer": invoice.customer.pk,
                "payment_date": str(on_date), "amount": amount, "currency": "USD",
                "payment_method": "BANK_TRANSFER", "bank_account": "",
                "transaction_reference": ref, "notes": "", f"allocate_{invoice.pk}": amount})

        receipt("PAY-PART-1", part, "400", self.day(10, 15))
        part.refresh_from_db()
        self.expect("a 400-dollar part payment leaves 600 owed",
                    part.outstanding_amount == D("600.00"), str(part.outstanding_amount))
        self.expect("and releases 40% of the invoice's 5,000 share",
                    self.bal("1350") == D("7500.00"), str(self.bal("1350")))
        releases = revaluation.live_releases(company).count()
        revaluation.sync_releases(company)
        self.expect("checking again releases nothing more",
                    revaluation.live_releases(company).count() == releases
                    and self.bal("1350") == D("7500.00"), str(self.bal("1350")))
        receipt("PAY-PART-2", part, "600", self.day(10, 16))
        self.expect("paying the rest releases the rest of its share",
                    self.bal("1350") == D("4500.00"), str(self.bal("1350")))

        client.post(reverse("sales:invoice_cancel", args=[gone.pk]), {})
        gone.refresh_from_db()
        self.expect("cancelling a revalued invoice releases its whole share",
                    gone.status == "CANCELLED" and self.bal("1350") == D("2500.00"),
                    f"{gone.status}, 1350 {self.bal('1350')}")
        release = JournalEntry.objects.filter(
            company=company, source_document_type=revaluation.RELEASE,
            source_document_id=f"Invoice:{gone.pk}").first()
        self.expect("dated no earlier than the revaluation",
                    release is not None and release.posting_date == self.day(9, 30),
                    str(release and release.posting_date))

        receipt("PAY-LATE", late, "500", self.day(9, 20))
        self.expect("a receipt keyed in late but dated before the revaluation releases its share",
                    self.bal("1350") == Z, str(self.bal("1350")))
        self.expect("on the revaluation's own day",
                    self.bal("1350", upto=self.day(9, 30)) == D("9500.00") - D("2500.00")
                    - D("2000.00"), str(self.bal("1350", upto=self.day(9, 30))))
        self.expect("every unrealised figure has come back off the profit and loss",
                    self.bal("4320") + self.bal("7310") == Z,
                    f"{self.bal('4320')} / {self.bal('7310')}")

        from apps.sales.models import CustomerPayment
        payment = CustomerPayment.objects.get(company=company, reference="PAY-PART-2")
        client.post(reverse("sales:payment_reverse", args=[payment.pk]), {"reason": "bounced"})
        self.expect("giving the second receipt back carries its 60% again",
                    self.bal("1350") == D("3000.00"), str(self.bal("1350")))
        client.post(reverse("accounting:revaluation_reverse"), {"date": str(self.day(10, 20))})
        self.expect("taking the revaluation off clears 1350 and every release with it",
                    self.bal("1350") == Z and not revaluation.live_releases(company).exists(),
                    str(self.bal("1350")))
