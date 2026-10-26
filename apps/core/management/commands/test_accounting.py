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
    4. stock receipt 100 @ 50  -> Dr 1400 5,000 / Cr 2110 5,000
       stock delivery 10 @ 50  -> Dr 5000   500 / Cr 1400   500
    5. supplier bill 2,000+320 -> Dr 1400 2,000, Dr 2210 320 / Cr 2100 2,320
       bill payment 2,320      -> Dr 2100 2,320 / Cr 1100 2,320
    6. sales order 10@100 +VAT, 1@200, 5% header discount
       -> subtotal 1,200, tax 152 (on the discounted value), total 1,292
          and no ledger entry
    7. invoice 5@100 +VAT, freight 300 -> Dr 1300 880 / Cr 4200 500, Cr 4100 300, Cr 2200 80
       customer payment 500    -> Dr 1200 500 / Cr 1300 500
    8. expense claim 1,200+300 -> Dr 6400 1,200, Dr 6000 300 / Cr 1100 1,500
    9. currency and tenant walls -> one book currency, no sight of another company's rows
   10. bank statement           -> tick the items, the difference must close, posting locks it
   11. chart maintenance        -> unique codes, retire and restore, dated ledger windows, colour
   12. period close             -> P and L carried to 3300, year end sweeps it to 3200,
                                  the balance sheet agrees at every step and reopen unwinds it
   13. draft journals           -> only drafts edit, references never change, cancel
                                  takes the paper out of the book without deleting it
   14. units of measure         -> standard units seeded, one base per kind, derived
                                  units convert through it, the anchor cannot be retired
   15. foreign currency         -> dated rates, one conversion per document, realised
                                  exchange gain and loss, reports still in one currency
   16. document lifecycle       -> orders convert to invoices and bills against counted
                                  quantities, credit and debit notes answer posted papers,
                                  cancellation offsets the entry instead of erasing it
   17. exchange revaluation     -> open foreign balances read at the reported day's rate
                                  on 1350/2150 against the unrealised gain and loss
                                  accounts, one reading live at a time, control accounts
                                  untouched, statements and ageing still in one money
   18. stock cost and margin     -> goods in at the order's own cost against the
                                  received-not-invoiced accrual, goods out at the average
                                  the shelf holds into cost of sales, a shortage written
                                  off through the adjustment account, and a movement the
                                  ledger refuses leaving neither a row nor an entry behind
   19. the counter              -> a till priced off the product card rather than the
                                  browser, debiting the drawer or the bank and crediting
                                  revenue and VAT in one receipt that never touches
                                  receivables; a refund as a second ticket putting the
                                  goods and the revenue back; and short notes, short
                                  stock, over-refunds and split tenders refused with
                                  nothing left behind
   20. receipts and reversals   -> a customer payment names which of the account's
                                  papers it settles, an allocation that cannot be true
                                  is refused before anything is saved, money that
                                  settles nothing rests on the account as a credit
                                  rather than an exchange gain, and a receipt given
                                  back is answered by an offsetting entry in its own
                                  period that puts every paper back on account
   21. the month's pay          -> basic pay from a contract and rates from the
                                  company's own rows, with every slip carrying the
                                  rate and the pay it was taken from; a tiered table
                                  read after the allowable shares, a pension ceiling
                                  that only bites above the pay it caps, pay elements
                                  that add or withhold and disappear when retired; a
                                  run refused while its take-home would be negative,
                                  approved before it is posted, booked as one balanced
                                  entry that charges the pay, owes both halves of each
                                  contribution and pays out only the net; and stood
                                  down again by a reversal in the pay's own period
   22. the asset register       -> saving an asset posts its purchase, and the charge
                                  each month is worked out of the price, the life and
                                  the residual rather than typed; a month is charged
                                  once, caught up across the register in one pass, and
                                  stood down by an offsetting entry in its own month
                                  that moves the book value back; a disposal clears
                                  both the cost and the wear and prices what came
                                  back as gain or loss; and the cost accounts, the
                                  contra asset and the charge hold exactly what the
                                  register says they hold
   23. the accuracy sweep        -> the finished book read as an auditor reads it:
                                  every entry balanced, every posted entry stamped
                                  with its period and in its own company's accounts,
                                  every source-document pointer resolving to a real
                                  row, and the receivable and payable control
                                  accounts agreeing with the subledgers they hold

Sections 1-8 therefore total Dr 113,020 = Cr 113,020. The later sections add more
postings (section 15's foreign documents alone add Dr 111,740 = Cr 111,740, and
section 16 adds the converted invoice, its credit note, a reversal and a bill
with its debit note), section 17 raises three open foreign documents and then
puts a revaluation on the book, redraws it, and takes it off again, section
18 takes a second product from purchase order through goods issue to invoice,
section 19 rings four tickets over a counter, section 20 raises three papers
on one account, takes two receipts against them and gives both of them back,
section 21 books a month's pay of Dr 127,260 = Cr 127,260 and then reverses
the whole of it, and section 22 brings four assets into the book, wears them
down over six months, unbuys and rebuys one of them, sells three of them at a
gain, a loss and a write-off, and answers one of those disposals with a
reversal, and section 23 stops building and starts reading: every entry in the
finished book balanced and stamped with its period and its company, every
source-document pointer resolving to a real row, and the receivable and
payable control accounts agreeing to the shilling with the papers underneath
them —
so the run ends by checking the whole book still balances to itself, the
valuation accounts are back to zero, every stock movement cost the ledger
exactly what the shelf moved, every ticket the till rang has a posted entry of
its own, the account that was paid and unpaid again is the account it was at
the start, every payroll account is back exactly where the month left it, the
fixed asset accounts hold neither more nor less than the register carries,
and the book as a disk-full of rows answers to the same numbers the screens
print.
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
    ("2110", "Stock Received Not Invoiced"),
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
    "2100": ("2320.00", "2320.00"),
    "2110": ("0.00", "5000.00"),
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


def edit_formset(prefix, rows, blank=None, blanks=1):
    """Formset data for rows that already exist on the instance.

    An inline formset only treats a row as an edit when it arrives as an
    initial form carrying its primary key; without those two things a saved
    line is re-created instead of updated.
    """
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(rows) + blanks),
        f"{prefix}-INITIAL_FORMS": str(len(rows)),
        f"{prefix}-MIN_NUM_FORMS": "1",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for i, row in enumerate(rows):
        for key, value in row.items():
            data[f"{prefix}-{i}-{key}"] = "" if value is None else str(value)
    for j in range(blanks):
        if blank:
            for key, value in blank.items():
                data[f"{prefix}-{len(rows) + j}-{key}"] = "" if value is None else str(value)
    return data


def merge(*parts):
    out = {}
    for part in parts:
        out.update(part)
    return out


class _FormTokenClient:
    """A test client that sends the till's one-time form token like a browser.

    Added with the rule that the terminal and refund views refuse a post
    without one (section 19, the counter). Before a POST to the terminal or
    to a ticket's refund it opens the page that renders that form and copies
    its token in, unless the caller sent one. Everything else passes straight
    through to the wrapped client.
    """

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _token(self, page, name):
        response = self.inner.get(page)
        ctx = getattr(response, "context", None)
        return (ctx.get(name) if ctx else None) or ""

    def post(self, path, data=None, **kwargs):
        from django.urls import Resolver404, resolve

        data = dict(data or {})
        try:
            match = resolve(path)
        except Resolver404:
            match = None
        name = match.view_name if match else ""
        if name == "pos:terminal" and "ticket_token" not in data:
            data["ticket_token"] = self._token(path, "ticket_token")
        elif name == "pos:sale_refund" and "refund_token" not in data:
            data["refund_token"] = self._token(
                reverse("pos:sale_detail", kwargs=match.kwargs), "refund_token")
        return self.inner.post(path, data, **kwargs)


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
        hit = any(needle.lower() in str(m).lower() for m in messages_)
        return self.expect(label, hit, str(messages_))

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

    def created_entry(self, client, name, data):
        """POST the manual journal form and hand back the saved entry.

        The reference is minted by the ledger rather than typed by the browser,
        so the entry is found through the redirect the view issues on success.
        """
        from apps.accounting.models import JournalEntry

        resp = self.post(client, name, reverse("accounting:journal_entry_create"), data)
        if resp.status_code not in (301, 302, 303):
            return None
        match = re.search(r"/journal-entries/([0-9a-fA-F-]{36})/?$", resp["Location"])
        if not match:
            return None
        return JournalEntry.objects.filter(pk=match.group(1)).first()

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
        self.section("10. Bank reconciliation through the screens")
        self.verify_reconciliation(client)

        self.section("11. Chart of accounts: codes, retiring, windows and colour")
        self.verify_account_module(client)

        self.section("12. Period close, year end and a balance sheet that agrees")
        self.verify_period_close(client)

        self.section("13. Draft journals: edit, cancel, and the wall around posted entries")
        self.verify_draft_editing(client)

        self.section("14. Units of measure: kinds, conversions and the counting rules")
        self.verify_units_of_measure(client)

        self.section("15. Foreign currency: dated rates, conversion and realised exchange")
        self.verify_multi_currency(client)

        self.section("16. Document lifecycle: convert, credit, reverse")
        self.verify_lifecycle(client)

        self.section("17. Exchange revaluation: reading open paper at the day's rate")
        self.verify_revaluation(client)

        self.section("18. Stock cost and margin: what the shelf is worth")
        self.verify_stock_costing(client)

        self.section("19. The counter: a till, its drawer and goods that come back")
        self.verify_counter_sale(client)

        self.section("20. Receipts, allocations and what a reversal puts back")
        self.verify_allocations(client)

        self.section("21. The month's pay: rates from a table, one journal for the run")
        self.verify_payroll(client)

        self.section("22. Fixed assets: the register and the ledger are one asset twice")
        self.verify_fixed_assets(client)

        self.section("23. The accuracy sweep: the finished book, read the way an auditor reads it")
        self.verify_transaction_accuracy(client)


    # ──────────────────────────── period close ───────────────────────────────

    def verify_period_close(self, client):
        """Closing a period is a journal entry, not a flag on the calendar.

        The profit and loss accounts have to empty into current year profit,
        year end has to sweep that into retained earnings, the balance sheet
        must agree before and after both moves, and reopening must unwind
        everything by reversal instead of by editing history.
        """
        from apps.accounting import gl_service
        from apps.accounting.models import JournalEntry, period_for

        company = self.company
        period = period_for(company, self.today)
        if not self.expect("today sits inside an accounting period", period is not None):
            return

        unstamped = JournalEntry.objects.filter(
            company=company, status="POSTED", period__isnull=True,
            posting_date__gte=period.start_date, posting_date__lte=period.end_date,
        )
        self.expect("posting stamped the period on every entry", not unstamped.exists(),
                    f"{unstamped.count()} posted entries dated inside {period.name} carry no period")

        activity = {row["code"]: row["net"]
                    for row in gl_service.period_activity(company, period.start_date, period.end_date)}
        if not self.expect("the period holds profit and loss activity", bool(activity),
                           str(sorted(activity))):
            return
        profit = gl_service.profit_for(company, period.start_date, period.end_date)
        self.expect("period profit is the sum of that activity, sign and all",
                    profit == -sum(activity.values(), Z), f"profit {profit} from {len(activity)} accounts")

        before = {code: self.account(code).current_balance for code in activity}
        self.expect("the window holds each of those accounts whole",
                    all(abs(before[code]) == abs(net) for code, net in activity.items()),
                    str({code: (before[code], activity[code]) for code in activity
                         if abs(before[code]) != abs(activity[code])}))
        retained_before = self.account("3200").current_balance

        sheet = client.get(reverse("reporting:balance_sheet")).context or {}
        if self.expect("balance sheet renders while the year is open", bool(sheet)):
            self.expect("current year profit is carried so the sheet agrees",
                        sheet["is_balanced"] and sheet["shows_profit"],
                        f"error {sheet['balancing_error']}, carried {sheet['current_year_profit']}")
            self.expect("the carried figure is this period's own profit",
                        sheet["current_year_profit"] == profit,
                        f"{sheet['current_year_profit']} vs {profit}")

        self.post(client, "period close",
                  reverse("accounting:period_close", args=[period.pk]), {})
        period.refresh_from_db()
        self.expect("closing the period marked it closed", period.status == "CLOSED", period.status)

        closing = gl_service.closing_entry_for(company, gl_service.PERIOD_CLOSE, period.pk)
        if not self.expect("a real closing entry was posted", closing is not None):
            return
        self.expect("closing entry is posted and balanced",
                    closing.status == "POSTED" and closing.is_balanced(),
                    f"{closing.status}, Dr {closing.total_debit} / Cr {closing.total_credit}")
        self.expect("closing entry is typed CLOSING and dated at the period end",
                    closing.entry_type == "CLOSING" and closing.posting_date == period.end_date,
                    f"{closing.entry_type} on {closing.posting_date}")
        self.expect("closing entry names the period it closes",
                    closing.source_document_type == gl_service.PERIOD_CLOSE
                    and closing.source_document_id == str(period.pk))
        self.expect("closing entry is stamped with its own period",
                    closing.period_id == period.pk, str(closing.period_id))
        want_lines = len(activity) + (1 if profit else 0)
        self.expect("one line per profit and loss account, plus the carry-over",
                    closing.lines.count() == want_lines,
                    f"{closing.lines.count()} lines, wanted {want_lines}")

        for code, net in activity.items():
            self.expect(f"{code} emptied into current year profit",
                        self.account(code).current_balance == Z,
                        f"{code} still holds {self.account(code).current_balance}")
        self.expect("current year profit holds the carried result",
                    self.account("3300").current_balance == profit,
                    f"3300 {self.account('3300').current_balance} vs profit {profit}")

        totals = self.posted_lines().aggregate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        self.expect("the ledger still balances after the carry-over",
                    totals["dr"] == totals["cr"], f"Dr {totals['dr']} / Cr {totals['cr']}")
        tb = client.get(reverse("accounting:trial_balance")).context
        self.expect("trial balance still reports itself balanced",
                    tb is not None and tb["is_balanced"])
        # The closing entry is dated at the period end, so it only shows on a
        # sheet drawn up as at that date; as at today the profit is legitimately
        # still sitting in the profit and loss accounts.
        sheet = client.get(reverse("reporting:balance_sheet")
                           + f"?date_to={period.end_date:%Y-%m-%d}").context or {}
        self.expect("balance sheet as at the period end puts the result inside equity",
                    sheet["is_balanced"] and not sheet["shows_profit"],
                    f"error {sheet['balancing_error']}, carried {sheet['current_year_profit']}")
        sheet = client.get(reverse("reporting:balance_sheet")).context or {}
        self.expect("the same books still agree before the period end arrives",
                    sheet["is_balanced"] and sheet["shows_profit"],
                    f"error {sheet['balancing_error']}, carried {sheet['current_year_profit']}")

        # A late adjustment into a closed period is allowed but must say so:
        # closing is reported here, never enforced (the accountant's choice).
        cash_before = self.account("1100").current_balance
        bank_before = self.account("1200").current_balance
        probe = {
            "reference": "IGNORE-ME", "entry_type": "MANUAL",
            "posting_date": str(period.start_date),
            "notes": "Late adjustment into a closed period",
        }
        probe.update(formset("lines", [
            {"account": self.account("1100").pk, "description": "Till short",
             "debit_amount": "1", "credit_amount": "0"},
            {"account": self.account("1200").pk, "description": "Bank over",
             "debit_amount": "0", "credit_amount": "1"},
        ], blank=BLANK_JOURNAL_LINE))
        late = self.created_entry(client, "late adjustment drafted into a closed period", probe)
        if late is not None:
            resp = client.post(reverse("accounting:journal_entry_post", args=[late.pk]), {})
            late.refresh_from_db()
            self.expect("a booking into a closed period is allowed, not blocked",
                        late.status == "POSTED", late.status)
            self.expect("the entry itself flags the closed period",
                        "closed" in late.calendar_warning, late.calendar_warning)
            self.expect_flash(client, resp, "closed", "the screen warns as the entry is posted")
            undo = late.reverse(user=self.user)
            self.expect("reversal of a dated-inside entry lands in the same period",
                        undo.period_id == late.period_id and undo.posting_date == late.posting_date,
                        f"{undo.posting_date} vs {late.posting_date}")
            from django.core.exceptions import ValidationError
            try:
                late.reverse(user=self.user)
                double = "accepted a second reversal"
            except ValidationError as exc:
                double = ""
            self.expect("an entry cannot be reversed twice", not double, double)
            self.expect("reversing the adjustment put the closed period back as it was",
                        self.account("1100").current_balance == cash_before
                        and self.account("1200").current_balance == bank_before,
                        f"1100 {self.account('1100').current_balance} vs {cash_before}, "
                        f"1200 {self.account('1200').current_balance} vs {bank_before}")

        self.post(client, "closing an already closed period",
                  reverse("accounting:period_close", args=[period.pk]), {})
        self.expect("a second close made no duplicate entry",
                    JournalEntry.objects.filter(
                        company=company, entry_type="CLOSING",
                        source_document_id=str(period.pk)).count() == 1)

        self.post(client, "period reopen",
                  reverse("accounting:period_reopen", args=[period.pk]), {})
        period.refresh_from_db()
        closing.refresh_from_db()
        self.expect("reopening hands the month its figures back",
                    period.status == "OPEN", period.status)
        self.expect("the closing entry stays in the ledger and gains a reversal",
                    closing.status == "POSTED" and closing.reversed_by_id is not None,
                    f"{closing.status}, reversal {closing.reversed_by_id}")
        self.expect("the reversal of the closing entry balances on its own",
                    closing.reversed_by.is_balanced() and closing.reversed_by.status == "POSTED",
                    str(closing.reversed_by.reference))
        for code, net in activity.items():
            self.expect(f"{code} came back to {before[code]}",
                        self.account(code).current_balance == before[code],
                        str(self.account(code).current_balance))
        self.expect("current year profit is empty again",
                    self.account("3300").current_balance == Z,
                    str(self.account("3300").current_balance))
        self.expect("reopening an open period reverses nothing twice",
                    gl_service.closing_entry_for(company, gl_service.PERIOD_CLOSE, period.pk) is None)

        # Year end: close for real, sweep the result into retained earnings,
        # then reopen and check the books end up exactly where they started.
        self.post(client, "period close before year end",
                  reverse("accounting:period_close", args=[period.pk]), {})
        fy = period.fiscal_year
        self.post(client, "fiscal year close",
                  reverse("accounting:fiscal_year_close", args=[fy.pk]), {})
        fy.refresh_from_db()
        self.expect("fiscal year marked closed", fy.is_closed)
        self.expect("every period in the year is shut",
                    not fy.periods.filter(status="OPEN").exists(),
                    str(sorted({p.status for p in fy.periods.all()})))
        year_entry = gl_service.closing_entry_for(company, gl_service.YEAR_CLOSE, fy.pk)
        if self.expect("year end swept current profit into retained earnings",
                       year_entry is not None):
            self.expect("year end entry is posted and balanced",
                        year_entry.status == "POSTED" and year_entry.is_balanced(),
                        f"{year_entry.status}, Dr {year_entry.total_debit} / Cr {year_entry.total_credit}")
            self.expect("retained earnings took the year's result",
                        self.account("3200").current_balance == retained_before + profit,
                        f"3200 {self.account('3200').current_balance}, was {retained_before}, profit {profit}")
            self.expect("current year profit cleared into it",
                        self.account("3300").current_balance == Z,
                        str(self.account("3300").current_balance))
        sheet = client.get(reverse("reporting:balance_sheet")).context or {}
        self.expect("balance sheet still agrees after the year end sweep", sheet["is_balanced"],
                    f"error {sheet['balancing_error']}")

        self.post(client, "fiscal year reopen",
                  reverse("accounting:fiscal_year_reopen", args=[fy.pk]), {})
        fy.refresh_from_db()
        period.refresh_from_db()
        self.expect("fiscal year reopened", not fy.is_closed)
        self.expect("the year's periods are open again",
                    not fy.periods.exclude(status="OPEN").exists(),
                    str(sorted({p.status for p in fy.periods.all()})))
        self.expect("retained earnings gave the year's result back",
                    self.account("3200").current_balance == retained_before,
                    str(self.account("3200").current_balance))
        self.expect("no live closing entry is left anywhere",
                    gl_service.closing_entry_for(company, gl_service.PERIOD_CLOSE, period.pk) is None
                    and gl_service.closing_entry_for(company, gl_service.YEAR_CLOSE, fy.pk) is None)
        for code, net in activity.items():
            self.expect(f"{code} is where the year started it",
                        self.account(code).current_balance == before[code],
                        str(self.account(code).current_balance))
        totals = self.posted_lines().aggregate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        self.expect("close and reopen leave the ledger balanced",
                    totals["dr"] == totals["cr"], f"Dr {totals['dr']} / Cr {totals['cr']}")
        sheet = client.get(reverse("reporting:balance_sheet")).context or {}
        self.expect("the sheet carries the profit again once the year is reopened",
                    sheet["is_balanced"] and sheet["shows_profit"],
                    f"error {sheet['balancing_error']}, carried {sheet['current_year_profit']}")
        self.note("Closing entries are dated at the period end and land in the ledger like any "
                  "other journal; a booking into a closed period is allowed and flagged, never "
                  "blocked, so a late adjustment can still be filed.")

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
        self.expect("invoice currency starts on the company book currency",
                    field.initial == self.company.currency, str(field.initial))
        self.expect("a currency with no filed rate is not offered",
                    [code for code, _ in field.choices] == [self.company.currency],
                    str(list(field.choices)))
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
        self.fx_company = fx
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
            self.expect("their invoice opens on their own book currency",
                        fx_form.fields["currency"].initial == "UGX",
                        str(fx_form.fields["currency"].initial))
            self.expect("this book's rates are not offered to them",
                        [code for code, _ in fx_form.fields["currency"].choices] == ["UGX"],
                        str(list(fx_form.fields["currency"].choices)))
            self.expect("their own branch is readable",
                        fx_client.get(reverse("organizations:branch_detail",
                                              args=[fx_branch.pk])).status_code == 200)

        # What the browser is actually handed, not only what the form object says.
        page = client.get(reverse("sales:invoice_create")).content.decode()
        before = page.split('name="currency"')[0][-60:] if 'name="currency"' in page else ""
        after_currency = page.split('name="currency"')[1][:260] if 'name="currency"' in page else ""
        self.expect("currency reaches the browser as a dropdown on the book currency",
                    "<select" in before and self.company.currency in after_currency,
                    f"{before[-24:].strip()} | {after_currency[:40].strip()}")
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
        self.expect("a foreign currency with no filed rate cannot be submitted",
                    "currency" in tamper.errors,
                    f"cleaned={tamper.cleaned_data.get('currency')} errors={dict(tamper.errors)}")

        blank = InvoiceForm(company=self.company, data={
            "reference": f"NOCCY{self.stamp}", "customer": cust.pk,
            "invoice_date": self.today.isoformat(), "due_date": self.today.isoformat(),
            "notes": "",
        })
        blank.full_clean()
        self.expect("a document that never mentions a currency books in the book currency",
                    blank.cleaned_data.get("currency") == self.company.currency,
                    f"cleaned={blank.cleaned_data.get('currency')} errors={dict(blank.errors)}")

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
        # One person keeps this whole book — stock, tills, payroll as well as the
        # ledger — which only the company's administrator is allowed to do now
        # that every screen checks the role. Role limits are tested elsewhere.
        self.expect("creator is the company administrator",
                    membership is not None and membership.role == "ADMIN",
                    membership.role if membership else "no membership")
        return company

    def setup_master_data(self):
        from apps.accounting.models import Account, TaxRate
        from apps.crm.models import Customer, Supplier
        from apps.hr.models import Employee
        from apps.inventory.models import ProductCategory, UnitOfMeasure
        from apps.organizations.models import Warehouse

        company = self.company
        self.account = lambda code: Account.objects.get(company=company, code=code)
        self.unit = lambda abbr: UnitOfMeasure.objects.get(company=company, abbreviation=abbr)
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
            "reference": "TYPED-BY-BROWSER",
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
        entry = self.created_entry(client, "opening journal", data)
        if not self.expect("manual journal saved", entry is not None):
            return
        self.expect("ledger mints its own reference",
                    entry.reference.startswith("JE-") and entry.reference != "TYPED-BY-BROWSER",
                    entry.reference)
        self.expect("typed reference never reached the books",
                    not JournalEntry.objects.filter(company=self.company,
                                                    reference="TYPED-BY-BROWSER").exists())
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
        self.reject(client, "the same supplier invoice entered a second time",
                    reverse("procurement:bill_create"),
                    dict(bill_data, reference="BILL-SMOKE-1B", supplier_reference="sup-inv-77"))

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
        self.expect("order tax from the lines", order.tax_amount == D("152.00"), str(order.tax_amount))
        self.expect("header discount taken once, not per line",
                   order.total_amount == D("1292.00"), str(order.total_amount))
        self.expect("VAT charged on the discounted value, not before it",
                   order.tax_amount == D("160.00") * D("0.95"), str(order.tax_amount))
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

    def book_totals(self):
        """(debit, credit) over every posted line in this company's book."""
        totals = self.posted_lines().aggregate(dr=Sum("debit_amount"),
                                               cr=Sum("credit_amount"))
        return totals["dr"] or Z, totals["cr"] or Z

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
        before = JournalEntry.objects.filter(company=self.company).count()
        data.update(formset("lines", [
            {"account": self.account("1100").pk, "description": "Dr only",
             "debit_amount": "100", "credit_amount": "0"},
            {"account": self.account("6000").pk, "description": "Cr short",
             "debit_amount": "0", "credit_amount": "50"},
        ], blank=BLANK_JOURNAL_LINE))
        entry = self.created_entry(client, "unbalanced journal save", data)
        if not self.expect("unbalanced entry saved as DRAFT",
                          entry is not None and entry.status == "DRAFT"):
            return
        self.expect("an unbalanced draft is kept, not posted",
                    JournalEntry.objects.filter(company=self.company).count() == before + 1)

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
        rows = JournalEntry.objects.filter(company=self.company).count()
        self.reject(client, "line that is both debit and credit",
                    reverse("accounting:journal_entry_create"), both)
        self.expect("double-sided entry not saved",
                    JournalEntry.objects.filter(company=self.company).count() == rows)

        too_thin = {
            "reference": "JE-BAD-3", "entry_type": "MANUAL",
            "posting_date": str(self.today), "notes": "",
        }
        too_thin.update(formset("lines", [
            {"account": self.account("1100").pk, "description": "only line",
             "debit_amount": "10", "credit_amount": "0"},
        ], blank=BLANK_JOURNAL_LINE, blanks=2))
        rows = JournalEntry.objects.filter(company=self.company).count()
        self.reject(client, "single-line journal",
                    reverse("accounting:journal_entry_create"), too_thin)
        self.expect("single-line journal not saved",
                    JournalEntry.objects.filter(company=self.company).count() == rows)

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
        self.expect("AP control ties to the bills that stand on it",
                    ap.current_balance == bills, f"GL {ap.current_balance} vs bills {bills}")
        # Goods that arrived are on the shelf and owed for, but no supplier has
        # billed them yet. They sit on the received-not-invoiced accrual, which
        # is exactly what keeps payables a bill book and nothing else.
        received = sum((m.total_cost for m in StockMovement.objects.filter(
            product__company=self.company, movement_type="RECEIPT")), Z)
        cleared = sum((b.accrued_cleared for b in
                       Bill.objects.filter(company=self.company,
                                           journal_entry__isnull=False)), Z)
        accrual = self.account("2110")
        self.expect("the accrual holds what was received and not yet billed",
                    accrual.current_balance == received - cleared,
                    f"2110 {accrual.current_balance} against {received} received "
                    f"less {cleared} billed")

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
        self.expect(f"all {len(RECOMMENDED)} recommended accounts were transacted",
                    not missing, "; ".join(missing))

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

    def verify_reconciliation(self, client):
        """A bank statement through the screens: start it, tick the items that
        appear on it, watch the difference close, post it, and try to break the
        lock that putting it on creates."""
        from django.core.exceptions import ValidationError

        from apps.banking import services
        from apps.banking.models import BankAccount, BankTransaction, Reconciliation

        bank_gl = self.account("1200")
        statement_date = self.today - datetime.timedelta(days=5)
        details = {
            "name": f"Equity Current {self.stamp}",
            "bank_name": "Equity Bank",
            "account_number": f"01{self.stamp}",
            "account_type": "CURRENT",
            "branch": "Westlands",
            "opening_balance": "80000.00",
        }

        self.reject(client, "bank account mapped to a tax account",
                    reverse("banking:account_create"),
                    merge(details, {"gl_account": self.account("2210").pk}))
        self.expect("the rejected bank account saved nothing",
                    not BankAccount.objects.filter(company=self.company,
                                                    name=details["name"]).exists())

        self.post(client, "bank account", reverse("banking:account_create"),
                  merge(details, {"gl_account": bank_gl.pk}))
        bank = BankAccount.objects.filter(company=self.company, name=details["name"]).first()
        if not self.expect("bank account created through the form", bank is not None):
            return
        self.expect("it points at the asset account it was mapped to",
                    bank.gl_account_id == bank_gl.pk)

        # A manual bank line now books its own ledger entry, so the form needs
        # the account on the other side: owner's capital for the money in,
        # professional fees for the money out. The figures under test are the
        # bank book's, which this does not change.
        def record(day, kind, amount, description):
            self.post(client, "bank transaction", reverse("banking:transaction_create"), {
                "bank_account": bank.pk,
                "transaction_date": day.isoformat(),
                "value_date": "",
                "transaction_type": kind,
                "amount": amount,
                "counter_account": self.account("3100" if kind == "CREDIT" else "6800").pk,
                "description": description,
                "reference": "",
            })
            txn = BankTransaction.objects.filter(bank_account=bank,
                                                 description=description).first()
            if not self.expect(f"{description} recorded", txn is not None):
                raise SystemExit(1)
            return txn

        deposit = record(statement_date - datetime.timedelta(days=15), "CREDIT", "12000.00", "Client transfer in")
        cheque = record(statement_date - datetime.timedelta(days=10), "DEBIT", "5000.00", "Supplier cheque")
        cashup = record(statement_date - datetime.timedelta(days=7), "CREDIT", "3000.00", "Branch cashup")
        late = record(statement_date + datetime.timedelta(days=3), "DEBIT", "1000.00", "Standing order")

        self.get_ok(client, "reconciliation list", reverse("banking:reconciliation_list"))
        self.get_ok(client, "reconciliation create form", reverse("banking:reconciliation_create"))

        statement = {
            "bank_account": bank.pk,
            "reference": f"STM-{self.stamp}",
            "statement_date": statement_date.isoformat(),
            "closing_balance": "90000.00",
            "notes": "",
        }
        resp = self.post(client, "statement", reverse("banking:reconciliation_create"), statement)
        recon = Reconciliation.objects.filter(bank_account=bank).first()
        if not self.expect("statement draft created through the form", recon is not None):
            return
        self.expect("it opens as a draft", recon.status == "DRAFT", recon.status)
        self.expect("its opening balance is brought forward from the account",
                    recon.opening_balance == D("80000.00"), str(recon.opening_balance))
        self.expect_flash(client, resp, "started", "the screen invites you to tick the items")

        detail_url = reverse("banking:reconciliation_detail", args=[recon.pk])
        page = client.get(detail_url)
        ctx = getattr(page, "context", None) or {}
        self.expect("the statement screen renders", page.status_code == 200, f"HTTP {page.status_code}")
        pending = list(ctx.get("pending") or [])
        self.expect("the three in-range items are offered for clearing",
                    len(pending) == 3, f"{len(pending)} offered")
        self.expect("an item dated after the statement is kept off the tick list",
                    ctx.get("after_statement") == 1, str(ctx.get("after_statement")))
        self.expect("an empty statement is out by the whole movement",
                    ctx.get("difference") == D("10000.00"), str(ctx.get("difference")))
        self.expect("the statement is not balanced yet", not ctx.get("is_balanced"))

        clear_url = reverse("banking:reconciliation_clear", args=[recon.pk])
        post_url = reverse("banking:reconciliation_post", args=[recon.pk])

        def tick(*txns):
            return client.post(clear_url, {"cleared": [str(t.pk) for t in txns]})

        resp = tick(deposit, cheque)
        self.expect_flash(client, resp, "2 item(s) cleared", "two ticked items clear")
        deposit.refresh_from_db()
        cheque.refresh_from_db()
        self.expect("cleared items are flagged and owned by the statement",
                    deposit.is_reconciled and deposit.reconciliation_id == recon.pk
                    and cheque.reconciliation_id == recon.pk)
        recon.refresh_from_db()
        self.expect("the missing item shows as a measured difference",
                    recon.difference == D("3000.00"), str(recon.difference))

        resp = client.post(post_url)
        self.expect_flash(client, resp, "Out of balance",
                          "posting is refused while the statement is out")
        recon.refresh_from_db()
        self.expect("the refused statement is still a draft", recon.status == "DRAFT", recon.status)

        try:
            services.clear(late, recon)
            too_new_blocked = False
        except ValidationError:
            too_new_blocked = True
        self.expect("an item newer than the statement date cannot be cleared on it",
                    too_new_blocked)

        resp = tick(deposit, cheque, cashup)
        self.expect_flash(client, resp, "1 item(s) cleared", "the last item clears")
        recon.refresh_from_db()
        self.expect("the cleared set now ties to the printed closing balance",
                    recon.is_balanced and recon.difference == Z, str(recon.difference))
        self.expect("re-ticking an already cleared item does not double-count it",
                    recon.transactions.count() == 3, f"{recon.transactions.count()} items")
        late.refresh_from_db()
        self.expect("the item after the statement date is still outstanding",
                    not late.is_reconciled)

        resp = client.post(post_url)
        self.expect_flash(client, resp, "posted", "a balanced statement posts")
        recon.refresh_from_db()
        self.expect("the posted statement records who posted it and when",
                    recon.status == "POSTED" and recon.reconciled_by_id == self.user.pk
                    and recon.reconciled_at is not None, str(recon.status))

        resp = tick(deposit, cheque)
        self.expect_flash(client, resp, "no longer be changed", "a posted statement is locked")
        self.expect("the locked statement still owns all three items",
                    recon.transactions.count() == 3)
        resp = client.post(post_url)
        self.expect_flash(client, resp, "Only a draft can be posted",
                          "posting the same statement twice is refused")

        resp = client.post(reverse("banking:transaction_delete", args=[deposit.pk]))
        self.expect_flash(client, resp, "reverse the statement",
                          "a cleared transaction cannot be deleted under a posted statement")
        self.expect("the cleared transaction survived the attempt",
                    BankTransaction.objects.filter(pk=deposit.pk).exists())

        self.reject(client, "a second statement for the same date",
                    reverse("banking:reconciliation_create"),
                    merge(statement, {"reference": f"STM-B{self.stamp}"}))
        self.expect("only one statement exists for that date",
                    Reconciliation.objects.filter(
                        bank_account=bank, statement_date=statement_date).count() == 1)
        self.reject(client, "a statement dated in the future",
                    reverse("banking:reconciliation_create"),
                    merge(statement, {
                        "reference": f"STM-F{self.stamp}",
                        "statement_date": (self.today + datetime.timedelta(days=30)).isoformat(),
                    }))

        cleared_rows = client.get(
            reverse("banking:transaction_list") + "?reconciled=true").context["transactions"]
        self.expect("the cleared filter returns cleared rows only",
                    cleared_rows.count() == 3 and all(t.is_reconciled for t in cleared_rows),
                    f"{cleared_rows.count()} rows")
        open_rows = client.get(
            reverse("banking:transaction_list") + "?reconciled=false").context["transactions"]
        self.expect("the outstanding filter returns uncleared rows only",
                    open_rows.count() == 1 and not any(t.is_reconciled for t in open_rows),
                    f"{open_rows.count()} rows")
        scoped = client.get(
            reverse("banking:transaction_list") + f"?account={bank.pk}").context["transactions"]
        self.expect("the account filter is honoured", scoped.count() == 4, f"{scoped.count()} rows")
        junk = client.get(reverse("banking:transaction_list") + "?account=not-a-uuid")
        self.expect("a hand-typed account id does not take the list down",
                    junk.status_code == 200, f"HTTP {junk.status_code}")
        self.expect("a hand-typed account id is ignored rather than trusted",
                    junk.context["transactions"].count() == 4)
        other_bank = BankAccount.objects.exclude(company=self.company).first()
        if other_bank:
            tampered = client.get(
                reverse("banking:transaction_list") + f"?account={other_bank.pk}")
            self.expect("another company's bank account shows no transactions of theirs",
                        all(t.bank_account.company_id == self.company.pk
                            for t in tampered.context["transactions"]))

        for name, url in [
            ("statement", detail_url.replace(str(recon.pk), "not-a-uuid")),
            ("transaction", reverse("banking:transaction_detail", args=[deposit.pk])
                           .replace(str(deposit.pk), "not-a-uuid")),
            ("missing statement", reverse("banking:reconciliation_detail",
                                          args=["00000000-0000-0000-0000-000000000000"])),
        ]:
            resp = client.get(url)
            self.expect(f"a hand-typed {name} url answers 404, not a crash",
                        resp.status_code == 404, f"HTTP {resp.status_code}")

        resp = self.post(client, "second statement", reverse("banking:reconciliation_create"), {
            "bank_account": bank.pk,
            "reference": f"STM2-{self.stamp}",
            "statement_date": self.today.isoformat(),
            "closing_balance": "89000.00",
            "notes": "",
        })
        second = Reconciliation.objects.filter(bank_account=bank, statement_date=self.today).first()
        if self.expect("the next statement starts where the posted one ended",
                       second is not None
                       and second.opening_balance == D("90000.00"),
                       str(getattr(second, "opening_balance", None))):
            self.expect("the service carries the same figure forward",
                        services.carried_forward_balance(bank, self.today) == D("90000.00"))
            resp = client.post(reverse("banking:reconciliation_clear", args=[second.pk]),
                               {"cleared": [str(late.pk)]})
            self.expect_flash(client, resp, "1 item(s) cleared",
                              "the item after the first statement clears on the second")
            second.refresh_from_db()
            self.expect("the second statement ties out too",
                        second.is_balanced, str(second.difference))
            resp = client.post(reverse("banking:reconciliation_post", args=[second.pk]))
            self.expect_flash(client, resp, "posted", "the second statement posts")

        reverse_url = reverse("banking:reconciliation_reverse", args=[recon.pk])
        # Statements are a chain — the second opens on the first's closing
        # balance — so the first cannot be reversed while the second stands.
        # This section used to reverse the first with the second still posted;
        # it now proves that is refused, then reverses the second and the
        # first in that order, and still proves the items go back to
        # outstanding.
        if second is not None:
            resp = client.post(reverse_url)
            self.expect_flash(client, resp, f"Reverse {second.reference} first",
                              "the earlier statement is not reversed while a later one stands")
            recon.refresh_from_db()
            self.expect("and it is still posted", recon.status == "POSTED", recon.status)
            resp = client.post(reverse("banking:reconciliation_reverse", args=[second.pk]))
            self.expect_flash(client, resp, "outstanding again",
                              "the later statement reverses first")
            second.refresh_from_db()
            late.refresh_from_db()
            self.expect("the later statement releases its item",
                        second.status == "REVERSED" and not late.is_reconciled)
        resp = client.post(reverse_url)
        self.expect_flash(client, resp, "outstanding again", "reversing releases the items")
        recon.refresh_from_db()
        self.expect("the reversed statement owns nothing and is marked reversed",
                    recon.status == "REVERSED" and recon.transactions.count() == 0)
        for txn in (deposit, cheque, cashup):
            txn.refresh_from_db()
        self.expect("released items are outstanding again",
                    all(not t.is_reconciled and t.reconciliation_id is None
                        for t in (deposit, cheque, cashup)))
        open_rows = client.get(
            reverse("banking:transaction_list") + "?reconciled=false").context["transactions"]
        # Four, not three: the standing order released by the second statement
        # is outstanding again too, now that both statements are reversed.
        self.expect("the released items reappear on the outstanding list",
                    open_rows.count() == 4, f"{open_rows.count()} rows")
        resp = client.post(reverse_url)
        self.expect_flash(client, resp, "Only a posted statement can be reversed",
                          "reversing twice is refused")

        resp = self.post(client, "statement on a reversed date",
                         reverse("banking:reconciliation_create"),
                         merge(statement, {"reference": f"STM3-{self.stamp}"}))
        reopened = Reconciliation.objects.filter(bank_account=bank, status="DRAFT",
                                                 statement_date=statement_date).first()
        self.expect("a reversed statement frees its date for a fresh one",
                    reopened is not None, str(resp.status_code))
        if reopened:
            self.expect("the fresh draft on the earlier date opens from the account again",
                        reopened.opening_balance == D("80000.00"),
                        str(reopened.opening_balance))
            # The old note here described reversing the first statement while
            # the second stayed posted; that is now refused, so the note says
            # what the rule is instead.
            self.note("Statements are reversed newest first, so a fresh draft for an earlier "
                      "date always opens on the last posted statement before it — here none, "
                      "so the account's own opening balance.")

        resp = client.post(reverse("banking:transaction_delete", args=[cashup.pk]))
        self.expect("an outstanding transaction can be deleted",
                    resp.status_code in (301, 302, 303)
                    and not BankTransaction.objects.filter(pk=cashup.pk).exists(),
                    f"HTTP {resp.status_code}")

        self.expect("the ledger account behind a bank account is the balance you reconcile",
                    bank.current_balance == bank_gl.current_balance
                    and services.gl_balance_at(bank_gl, self.today) == bank_gl.current_balance,
                    str(bank.current_balance))

    def verify_account_module(self, client):
        """The chart of accounts as a working module: the reconcilable rule,
        unique codes, retiring and restoring, dated ledger windows and colour."""
        from apps.accounting.forms import AccountForm, JournalEntryLineForm
        from apps.accounting.models import Account, JournalEntry, JournalEntryLine
        from apps.banking.models import BankAccount
        from apps.core.templatetags import ui_extras

        company = self.company

        self.post(client, "mobile money account", reverse("accounting:account_create"), {
            "code": "1250", "name": "Mobile money float", "account_type": "BANK",
            "parent": self.account("1000").pk, "description": "", "currency": "",
        })
        mobile = Account.objects.filter(company=company, code="1250").first()
        if not self.expect("new account created through the form", mobile is not None):
            return
        self.expect("a bank account is reconcilable whether the box was ticked or not",
                    mobile.is_reconcilable)

        self.post(client, "suspense account", reverse("accounting:account_create"), {
            "code": "A100", "name": "Suspense clearing", "account_type": "ASSET",
            "parent": "", "description": "", "currency": "",
        })
        suspense = Account.objects.filter(company=company, code="A100").first()
        self.expect("an ordinary asset stays unflagged unless asked",
                    suspense is not None and not suspense.is_reconcilable)

        clash = AccountForm(company=company, data={
            "code": "a100", "name": "Second suspense", "account_type": "ASSET", "currency": "",
        })
        self.expect("codes are unique per company whatever the case", not clash.is_valid(),
                    str(dict(clash.errors)))
        self.reject(client, "duplicate code", reverse("accounting:account_create"), {
            "code": "1250", "name": "Duplicate float", "account_type": "BANK",
            "parent": "", "description": "", "currency": "",
        })
        self.expect("the rejected duplicate saved nothing",
                    Account.objects.filter(company=company, code="1250").count() == 1)

        # --- retiring an account the books still lean on
        parent = self.account("1000")
        resp = client.post(reverse("accounting:account_deactivate", args=[parent.pk]))
        self.expect_flash(client, resp, "active child account",
                          "an account with active children cannot be retired")
        self.expect("the parent account is still active",
                    Account.objects.get(pk=parent.pk).is_active)

        mapped = self.account("1200")
        resp = client.post(reverse("accounting:account_deactivate", args=[mapped.pk]))
        self.expect_flash(client, resp, "remap those before retiring",
                          "an account behind a live bank account cannot be retired")
        self.expect("the bank ledger account is still active",
                    Account.objects.get(pk=mapped.pk).is_active)

        spare = Account.objects.create(company=company, code="9990", name="Retirement test",
                                       account_type="ASSET")
        self.expect("an active account is offered on journal lines",
                    JournalEntryLineForm(company=company).fields["account"].queryset
                    .filter(pk=spare.pk).exists())

        resp = client.post(reverse("accounting:account_deactivate", args=[spare.pk]))
        self.expect_flash(client, resp, "retired", "a spare account can be retired")
        spare.refresh_from_db()
        self.expect("retiring clears the active flag instead of deleting history",
                    not spare.is_active)
        self.expect("a retired account stops being offered on entry forms",
                    not JournalEntryLineForm(company=company).fields["account"].queryset
                    .filter(pk=spare.pk).exists())
        active_rows = client.get(reverse("accounting:account_list")).context["accounts"]
        self.expect("the default chart hides retired accounts",
                    not any(a.pk == spare.pk for a in active_rows))
        self.expect("retiring one account does not empty the chart",
                    active_rows.count() >= 50, f"{active_rows.count()} rows")
        retired_rows = client.get(
            reverse("accounting:account_list") + "?state=inactive").context["accounts"]
        self.expect("the retired filter finds it again",
                    any(a.pk == spare.pk for a in retired_rows))

        resp = client.post(reverse("accounting:account_activate", args=[spare.pk]))
        self.expect_flash(client, resp, "is active again", "a retired account can be restored")
        spare.refresh_from_db()
        self.expect("restoring puts it back on the entry forms",
                    spare.is_active and JournalEntryLineForm(company=company)
                    .fields["account"].queryset.filter(pk=spare.pk).exists())
        self.get_ok(client, "chart showing every state",
                    reverse("accounting:account_list") + "?state=all")
        typed = client.get(reverse("accounting:account_list") + "?type=BANK").context["accounts"]
        self.expect("the type filter only returns that type",
                    typed.count() >= 2 and all(a.account_type == "BANK" for a in typed),
                    f"{typed.count()} rows")

        # --- the general ledger for one account, over a window
        from apps.accounting.models import period_for

        cash = self.account("1100")
        month_ago = self.today - datetime.timedelta(days=30)
        backdated = JournalEntry.objects.create(
            company=company, reference=f"BACK{self.stamp}", entry_type="OPENING",
            posting_date=month_ago, status="POSTED", posted_by=self.user,
            notes="Filed a month ago so the ledger has a window to open on",
            # the row is planted straight into the ledger, so it has to carry
            # the stamp the posting rule would have given it
            period=period_for(company, month_ago),
        )
        JournalEntryLine.objects.create(journal_entry=backdated, account=cash,
                                        debit_amount=D("100.00"), credit_amount=Z,
                                        description="Opening float")
        JournalEntryLine.objects.create(journal_entry=backdated, account=self.account("3100"),
                                        debit_amount=Z, credit_amount=D("100.00"),
                                        description="Opening float")

        ledger_url = reverse("accounting:account_ledger", args=[cash.pk])
        sign = 1 if cash.normal_balance == "DEBIT" else -1
        carried = D("100.00") * sign
        posted = JournalEntryLine.objects.filter(
            account=cash, journal_entry__status="POSTED").select_related("journal_entry")

        full = client.get(ledger_url).context
        self.expect("the ledger lists every posted line",
                    full["line_count"] == posted.count(), f"{full['line_count']} of {posted.count()}")
        self.expect("the running balance ends on the account balance",
                    full["closing_balance"] == cash.current_balance,
                    f"{full['closing_balance']} vs {cash.current_balance}")

        emptied = client.get(
            ledger_url + f"?from={(self.today + datetime.timedelta(days=1)).isoformat()}").context
        self.expect("a window with nothing in it opens at the brought-forward balance",
                    emptied["line_count"] == 0 and emptied["opening_balance"] == cash.current_balance,
                    str(emptied["opening_balance"]))

        junk = client.get(ledger_url + "?from=not-a-date&to=nope")
        self.expect("hand-typed dates cannot take the ledger down",
                    junk.status_code == 200, f"HTTP {junk.status_code}")
        self.expect("unparseable dates are ignored rather than passed to the query",
                    junk.context["opening_balance"] == Z
                    and junk.context["closing_balance"] == cash.current_balance,
                    str(junk.context["closing_balance"]))

        window = client.get(ledger_url + f"?from={self.today.isoformat()}").context
        self.expect("a filtered window opens on the balance brought forward from before it",
                    window["opening_balance"] == carried,
                    f"{window['opening_balance']} vs {carried}")
        self.expect("the window shows only the lines inside it",
                    window["line_count"] == posted.count() - 1,
                    f"{window['line_count']} of {posted.count()}")
        self.expect("opening + movement inside the window = closing",
                    window["closing_balance"] == window["opening_balance"]
                    + (window["total_debit"] - window["total_credit"]) * sign,
                    str(window["closing_balance"]))
        self.expect("a window that runs to today ends on the account balance",
                    window["closing_balance"] == cash.current_balance,
                    f"{window['closing_balance']} vs {cash.current_balance}")

        upto = client.get(ledger_url + f"?to={month_ago.isoformat()}").context
        self.expect("a window ending in the past opens from nothing",
                    upto["opening_balance"] == Z, str(upto["opening_balance"]))
        self.expect("a window ending in the past closes on the balance up to that date",
                    upto["closing_balance"] == carried,
                    f"{upto['closing_balance']} vs {carried}")
        self.expect("the ending window hides today's lines", upto["line_count"] == 1,
                    str(upto["line_count"]))

        gap = client.get(ledger_url + f"?from={(month_ago + datetime.timedelta(days=1)).isoformat()}"
                           f"&to={(self.today - datetime.timedelta(days=1)).isoformat()}").context
        self.expect("a window with no activity holds its balance flat",
                    gap["line_count"] == 0 and gap["opening_balance"] == carried
                    and gap["closing_balance"] == carried,
                    f"{gap['opening_balance']} -> {gap['closing_balance']}")

        # --- every account wears its own colour, everywhere it appears
        for label, url in [
            ("chart of accounts", reverse("accounting:account_list")),
            ("trial balance", reverse("accounting:trial_balance")),
            ("bank accounts", reverse("banking:account_list")),
            ("transactions", reverse("banking:transaction_list")),
            ("statements", reverse("banking:reconciliation_list")),
            ("general ledger", reverse("accounting:account_ledger", args=[cash.pk])),
            ("journal entry", reverse("accounting:journal_entry_detail", args=[backdated.pk])),
        ]:
            body = client.get(url).content.decode()
            self.expect(f"{label} shades rows in the account's colour",
                        "box-shadow:inset 3px 0 0 hsl(" in body)

        entry = client.get(reverse("accounting:journal_entry_detail",
                                   args=[backdated.pk])).content.decode()
        entry_hues = set(re.findall(r"box-shadow:inset 3px 0 0 hsl\((\d+),", entry))
        self.expect("the two sides of a journal entry are shaded apart",
                    len(entry_hues) == 2, f"{len(entry_hues)} hues on the entry")

        chart = client.get(reverse("accounting:account_list")).content.decode()
        hues = set(re.findall(r"box-shadow:inset 3px 0 0 hsl\((\d+),", chart))
        self.expect("different accounts really are shaded in different colours",
                    len(hues) >= 10, f"{len(hues)} distinct hues on the chart")
        self.expect("account codes are painted as chips too",
                    bool(re.search(r"background:hsl\(\d+, 60%, 92%", chart)))

        row_of = ui_extras.account_row
        codes = ["1000", "1100", "1200", "1300", "2100", "2200", "3100",
                 "4200", "5000", "6100"]
        self.expect("no two accounts in the chart share a shade",
                    len({row_of(self.account(code)) for code in codes}) == len(codes))
        self.expect("the same account always reads the same colour",
                    row_of(cash) == row_of(Account.objects.get(pk=cash.pk)))
        bank = BankAccount.objects.filter(company=company).first()
        if bank:
            self.expect("a bank account is shaded in its ledger account's colour",
                        row_of(bank) == row_of(bank.gl_account))

        bank_hue = ui_extras.account_hue(self.account("1200"))
        cogs_hue = ui_extras.account_hue(self.account("5000"))
        apart = abs(bank_hue - cogs_hue) % 360
        self.expect("different families sit in visibly different colour ranges",
                    min(apart, 360 - apart) > 30,
                    f"bank {bank_hue} vs cost of goods {cogs_hue}")

    # ──────────────────────── draft journals: edit and cancel ─────────────────

    def verify_draft_editing(self, client):
        """A draft is a working paper; a posted entry is history.

        The edit screen may only be reached by drafts, must never renumber an
        entry, and cancelling takes the paper out of the book rather than
        pretending it was never written.
        """
        from django.db import IntegrityError, transaction as db_transaction

        from apps.accounting.models import JournalEntry

        company = self.company

        def line_rows(entry, amounts):
            """Formset rows for a saved entry, with new debit/credit pairs."""
            return [{"id": line.pk, "account": line.account_id,
                     "description": line.description,
                     "debit_amount": amounts[i][0], "credit_amount": amounts[i][1]}
                    for i, line in enumerate(entry.lines.all())]

        draft = self.created_entry(client, "draft for editing", merge({
            "entry_type": "MANUAL",
            "posting_date": str(self.today),
            "notes": "Insurance premium, first attempt",
        }, formset("lines", [
            {"account": self.account("6600").pk, "description": "Office insurance",
             "debit_amount": "500", "credit_amount": "0"},
            {"account": self.account("1100").pk, "description": "Paid by cheque",
             "debit_amount": "0", "credit_amount": "500"},
        ], blank=BLANK_JOURNAL_LINE)))
        if not self.expect("draft journal created", draft is not None):
            return
        reference = draft.reference
        edit_url = reverse("accounting:journal_entry_edit", args=[draft.pk])

        resp = client.get(edit_url)
        form = resp.context.get("form") if resp.context else None
        self.expect("draft edit page renders", resp.status_code == 200 and form is not None,
                    f"HTTP {resp.status_code}")
        if form is None:
            return
        self.expect("the reference is locked once the entry has one",
                    form.fields["reference"].disabled,
                    f"disabled={form.fields['reference'].disabled}")
        self.expect("the edit screen knows it is editing",
                    resp.context.get("editing") is True, str(resp.context.get("editing")))
        self.expect("the edit page prefills the lines as saved",
                    str(form.instance.lines.first().debit_amount) == "500.00",
                    str(form.instance.lines.first().debit_amount))

        self.post(client, "draft edit", edit_url, merge({
            "entry_type": "MANUAL",
            "posting_date": str(self.today),
            "notes": "Insurance premium, corrected amount",
        }, edit_formset("lines", line_rows(draft, [("550", "0"), ("0", "550")]),
                        blank=BLANK_JOURNAL_LINE)))
        draft.refresh_from_db()
        self.expect("edited draft keeps its reference", draft.reference == reference, draft.reference)
        self.expect("edited draft is still a draft", draft.status == "DRAFT", draft.status)
        self.expect("the corrected amounts saved",
                    draft.total_debit == 550 and draft.total_credit == 550,
                    f"Dr {draft.total_debit} / Cr {draft.total_credit}")
        self.expect("the edit left exactly two lines, not four", draft.lines.count() == 2,
                    str(draft.lines.count()))
        self.expect("the note was updated", draft.notes == "Insurance premium, corrected amount",
                    draft.notes)

        tampered = merge({
            "reference": "HACKED-1", "entry_type": "MANUAL",
            "posting_date": str(self.today), "notes": "tamper",
        }, edit_formset("lines", line_rows(draft, [("550", "0"), ("0", "550")]),
                        blank=BLANK_JOURNAL_LINE))
        self.post(client, "draft edit with a typed reference", edit_url, tampered)
        draft.refresh_from_db()
        self.expect("a typed reference cannot overwrite the ledger's number",
                    draft.reference == reference, draft.reference)
        self.expect("a disabled reference field ignores whatever the browser sent",
                    draft.notes != "tamper" or draft.reference == reference,
                    f"{draft.reference} / {draft.notes}")

        # A draft may sit unbalanced while the bookkeeper works on it, but it
        # must not be postable until the two sides agree.
        self.post(client, "draft edit to an unbalanced state", edit_url, merge({
            "entry_type": "MANUAL", "posting_date": str(self.today), "notes": "off by 10",
        }, edit_formset("lines", line_rows(draft, [("550", "0"), ("0", "540")]),
                        blank=BLANK_JOURNAL_LINE)))
        draft.refresh_from_db()
        self.expect("an unbalanced edit is still only a draft",
                    draft.status == "DRAFT" and draft.total_credit == 540,
                    f"{draft.status}, Cr {draft.total_credit}")
        refused = client.post(reverse("accounting:journal_entry_post", args=[draft.pk]), {})
        self.expect_flash(client, refused, "not balanced",
                          "Post refuses the unbalanced draft from the edit screen")

        self.post(client, "draft edit back to balance", edit_url, merge({
            "entry_type": "MANUAL", "posting_date": str(self.today),
            "notes": "Insurance premium, final",
        }, edit_formset("lines", line_rows(draft, [("550", "0"), ("0", "550")]),
                        blank=BLANK_JOURNAL_LINE)))
        self.post(client, "balanced draft posts",
                  reverse("accounting:journal_entry_post", args=[draft.pk]), {})
        draft.refresh_from_db()
        self.expect("the edited draft posts with its corrected amounts",
                    draft.status == "POSTED" and draft.total_debit == 550,
                    f"{draft.status}, Dr {draft.total_debit}")
        self.expect("posting stamped the period", draft.period_id is not None,
                    str(draft.period))

        self.expect("a posted entry cannot be opened for editing",
                    client.get(edit_url).status_code == 404)
        cancel = client.post(reverse("accounting:journal_entry_cancel", args=[draft.pk]), {})
        self.expect_flash(client, cancel, "only a draft can be cancelled",
                          "cancelling a posted entry is refused")
        draft.refresh_from_db()
        self.expect("the refused cancel left the entry posted",
                    draft.status == "POSTED", draft.status)
        self.expect("the edit url is not merely hidden — it is closed",
                    not JournalEntry.objects.filter(
                        company=company, pk=draft.pk, status="DRAFT").exists())

        # A second draft, thrown away instead of posted.
        scrap = self.created_entry(client, "draft to cancel", merge({
            "entry_type": "MANUAL",
            "posting_date": str(self.today),
            "notes": "Booked to the wrong account",
        }, formset("lines", [
            {"account": self.account("6600").pk, "description": "Wrong code",
             "debit_amount": "300", "credit_amount": "0"},
            {"account": self.account("1100").pk, "description": "Cash",
             "debit_amount": "0", "credit_amount": "300"},
        ], blank=BLANK_JOURNAL_LINE)))
        if scrap is None:
            return
        cancelled_ref = scrap.reference
        resp = client.post(reverse("accounting:journal_entry_cancel", args=[scrap.pk]), {})
        self.expect("cancelling a draft redirects away from it",
                    resp.status_code in (301, 302, 303), f"HTTP {resp.status_code}")
        scrap.refresh_from_db()
        self.expect("the draft is cancelled, not deleted", scrap.status == "CANCELLED",
                    scrap.status)
        self.expect("a cancelled draft never reached the ledger",
                    not self.posted_lines().filter(journal_entry=scrap).exists())
        self.expect("cancelling leaves the balance rule untouched",
                    scrap.total_debit == scrap.total_credit == 300,
                    f"Dr {scrap.total_debit}")
        listed = client.get(reverse("accounting:journal_entry_list") + "?status=CANCELLED")
        rows = list(listed.context["entries"]) if listed.context else []
        self.expect("cancelled drafts are findable, not invisible",
                    any(e.pk == scrap.pk for e in rows), f"{len(rows)} rows listed")
        re_post = client.post(reverse("accounting:journal_entry_post", args=[scrap.pk]), {})
        self.expect_flash(client, re_post, "Cannot post",
                          "a cancelled draft cannot be posted")
        scrap.refresh_from_db()
        self.expect("the refused post left it cancelled", scrap.status == "CANCELLED",
                    scrap.status)
        self.expect("the edit screen is shut on a cancelled draft too",
                    client.get(reverse("accounting:journal_entry_edit",
                                       args=[scrap.pk])).status_code == 404)

        try:
            with db_transaction.atomic():
                JournalEntry.objects.create(
                    company=company, reference=cancelled_ref, entry_type="MANUAL",
                    posting_date=self.today, status="DRAFT")
                clash = None
        except IntegrityError:
            clash = "duplicate"
        self.expect("one reference cannot exist twice in a company's book",
                    clash == "duplicate", str(clash))

        # The same number in another company's book is a different ledger.
        other_ref = JournalEntry.objects.filter(company=company).first().reference
        fx = getattr(self, "fx_company", None)
        if fx is not None:
            created = False
            try:
                JournalEntry.objects.create(
                    company=fx, reference=other_ref, entry_type="MANUAL",
                    posting_date=self.today, status="DRAFT")
                created = True
            finally:
                JournalEntry.objects.filter(company=fx, reference=other_ref).delete()
            self.expect("the same reference is allowed in a different company", created,
                        other_ref)
        self.note("Drafts are editable working papers; a posted entry is history that "
                  "can only be reversed, never rewritten. References are minted by the "
                  "book and are unique per company, so two people saving at once cannot "
                  "collide — and one company's numbers never constrain another's.")

    # ───────────────────────────── units of measure ───────────────────────────

    def verify_units_of_measure(self, client):
        """A quantity means nothing without the unit it is counted in.

        The book seeds the standard units for a new company, keeps one base
        unit per kind so derived units have something to convert through, and
        refuses to retire the anchor out from under them.
        """
        from django.core.exceptions import ValidationError

        from apps.inventory.models import Product, UnitOfMeasure, convert_quantity

        company = self.company

        self.expect("a new company can count things immediately",
                    UnitOfMeasure.objects.filter(company=company, abbreviation="PCS").exists(),
                    f"{UnitOfMeasure.objects.filter(company=company).count()} units on file")
        for abbrev, kind in [("KG", "WEIGHT"), ("LTR", "VOLUME"), ("MTR", "LENGTH"),
                             ("HR", "TIME")]:
            unit = UnitOfMeasure.objects.filter(company=company, abbreviation=abbrev).first()
            self.expect(f"{abbrev} seeded as the base of {kind}",
                        unit is not None and unit.is_base and unit.base_factor == 1,
                        str(unit and (unit.is_base, unit.base_factor)))

        self.get_ok(client, "units list", reverse("inventory:unit_list"))
        listed = client.get(reverse("inventory:unit_list")).context or {}
        groups = listed.get("groups") or []
        self.expect("units are grouped by kind for the reader",
                    len(groups) >= 5, f"{len(groups)} kinds listed")
        self.expect("every kind on the page has a base unit to convert through",
                    not any(g["missing_base"] for g in groups),
                    str([g["kind"] for g in groups if g["missing_base"]]))

        self.post(client, "bag unit", reverse("inventory:unit_create"), {
            "name": "50kg Bag", "abbreviation": "bag", "kind": "WEIGHT",
            "base_factor": "50", "is_base": "",
        })
        bag = UnitOfMeasure.objects.filter(company=company, abbreviation="BAG").first()
        if not self.expect("a derived unit is stored upper-cased", bag is not None):
            return
        self.expect("the derived unit keeps its conversion factor",
                    bag.base_factor == 50 and not bag.is_base,
                    f"factor {bag.base_factor}, base {bag.is_base}")

        self.reject(client, "a second base unit for weight", reverse("inventory:unit_create"), {
            "name": "Stone", "abbreviation": "STN", "kind": "WEIGHT",
            "base_factor": "1", "is_base": "on",
        })
        self.reject(client, "two units with the same code", reverse("inventory:unit_create"), {
            "name": "Sack", "abbreviation": "bag", "kind": "WEIGHT",
            "base_factor": "25", "is_base": "",
        })
        self.reject(client, "a zero conversion factor", reverse("inventory:unit_create"), {
            "name": "Nothing", "abbreviation": "NIL", "kind": "COUNT",
            "base_factor": "0", "is_base": "",
        })
        self.reject(client, "a base unit that is not worth one", reverse("inventory:unit_create"), {
            "name": "Odd base", "abbreviation": "ODD", "kind": "COUNT",
            "base_factor": "7", "is_base": "on",
        })

        # Conversions: the arithmetic a storekeeper relies on.
        pcs = UnitOfMeasure.objects.get(company=company, abbreviation="PCS")
        ton = UnitOfMeasure.objects.get(company=company, abbreviation="TON")
        self.expect("3 dozen counts as 36 pieces",
                    convert_quantity(3, self.unit("DZN"), pcs) == 36,
                    str(convert_quantity(3, self.unit("DZN"), pcs)))
        self.expect("half a tonne weighs 500 kilograms",
                    convert_quantity("0.5", ton, self.unit("KG")) == 500,
                    str(convert_quantity("0.5", ton, self.unit("KG"))))
        self.expect("750 millilitres is 0.75 litre",
                    convert_quantity(750, self.unit("ML"), self.unit("LTR")) == Decimal("0.75"),
                    str(convert_quantity(750, self.unit("ML"), self.unit("LTR"))))
        self.expect("a unit converts to itself unchanged",
                    convert_quantity(7, pcs, pcs) == 7)
        try:
            bad = convert_quantity(1, ton, pcs)
        except ValidationError as e:
            bad = str(e.messages[0])
        self.expect("weight cannot be converted into pieces",
                    isinstance(bad, str) and "different kinds" in bad, str(bad))

        # Retiring the anchor of a kind is refused while units hang off it.
        self.post(client, "try to retire a base unit",
                  reverse("inventory:unit_deactivate", args=[self.unit("KG").pk]), {})
        self.expect("a base unit with derived units cannot be retired",
                    self.unit("KG").is_active,
                    f"active={self.unit('KG').is_active}")
        self.post(client, "promote the bag to base",
                  reverse("inventory:unit_set_base", args=[bag.pk]), {})
        bag.refresh_from_db()
        kg = self.unit("KG")
        self.expect("promoting a unit demotes the unit it replaces",
                    bag.is_base and bag.base_factor == 1 and not kg.is_base,
                    f"bag {bag.is_base}/{bag.base_factor}, kg base {kg.is_base}")
        self.expect("the old base now converts through the new one",
                    UnitOfMeasure.objects.filter(company=company, kind="WEIGHT").count() >= 2)
        # Put the kilogram back as the anchor so later checks read sensibly.
        self.post(client, "restore kilogram as base",
                  reverse("inventory:unit_set_base", args=[kg.pk]), {})
        self.post(client, "retire the bag",
                  reverse("inventory:unit_deactivate", args=[bag.pk]), {})
        bag.refresh_from_db()
        self.expect("a unit nobody converts through can be retired", not bag.is_active)
        self.expect("retiring does not delete the row",
                    UnitOfMeasure.objects.filter(pk=bag.pk).exists())

        # Products must be counted in one of this company's own units.
        form = client.get(reverse("inventory:product_create")).context.get("form")
        offered = list(form.fields["unit_of_measure"].queryset) if form else []
        self.expect("the product form offers only this company's units",
                    offered and all(u.company_id == company.pk for u in offered),
                    f"{len(offered)} units offered")
        self.expect("a retired unit is not offered on a new product",
                    bag.pk not in [u.pk for u in offered], str([u.abbreviation for u in offered]))

        self.reject(client, "a product with no unit", reverse("inventory:product_create"), merge({
            "sku": "NO-UOM", "name": "Countless thing", "product_type": "GOODS",
            "unit_of_measure": "", "cost_price": "1", "selling_price": "2",
            "category": "", "tax_rate": "", "reorder_level": "0", "reorder_quantity": "0",
            "valuation_method": "WAVG", "barcode": "", "description": "",
            "income_account": "", "cogs_account": "", "inventory_account": "",
        }, {}))

        # Stock levels and movements read with their unit attached.
        from apps.core.templatetags import ui_extras
        product = getattr(self, "product", None) or Product.objects.filter(
            company=company).first()
        if product:
            self.expect("a quantity renders with the product's unit",
                        ui_extras.with_unit(Decimal("12.0000"), product.unit_of_measure)
                        == f"12 {product.unit_of_measure.abbreviation}",
                        ui_extras.with_unit(Decimal("12.0000"), product.unit_of_measure))
        self.expect("trailing zeros do not clutter a quantity",
                    ui_extras.trim_qty(Decimal("2.5000")) == "2.5"
                    and ui_extras.trim_qty(Decimal("0.0000")) == "0",
                    f"{ui_extras.trim_qty(Decimal('2.5000'))}, {ui_extras.trim_qty(Decimal('0.0000'))}")
        # A recipe written in dozens must draw pieces out of the store.
        from apps.manufacturing.models import BillOfMaterials, WorkOrder, WorkOrderConsumption

        dzn = self.unit("DZN")
        self.post(client, "bill of materials", reverse("manufacturing:bom_create"), merge({
            "name": f"Assembly {self.stamp}", "product": self.product.pk, "version": "1.0",
            "status": "ACTIVE", "production_quantity": "2", "notes": "Two widgets per run",
        }, formset("lines", [
            {"component": self.product.pk, "quantity": "2",
             "unit_of_measure": dzn.pk, "scrap_percent": "50"},
        ], blank={"component": "", "quantity": "", "unit_of_measure": "", "scrap_percent": "0"})))
        bom = BillOfMaterials.objects.filter(company=company, product=self.product).first()
        if not self.expect("bill of materials created", bom is not None):
            return
        line = bom.lines.first()
        self.expect("the scrap allowance is part of the line",
                    line is not None and line.scrap_percent == 50,
                    str(line and line.scrap_percent))
        self.expect("50% scrap on 2 dozen is 3 dozen",
                    line.effective_quantity == 3, str(line.effective_quantity))

        self.post(client, "work order from that bill", reverse("manufacturing:work_order_create"), {
            "reference": f"WO-UOM-{self.stamp}", "bom": bom.pk, "planned_quantity": "6",
            "planned_start": str(self.today), "planned_end": str(self.today),
            "warehouse": self.warehouse.pk, "notes": "Three runs",
        })
        wo = WorkOrder.objects.filter(company=company, bom=bom).first()
        if not self.expect("work order planned", wo is not None):
            return
        consumption = WorkOrderConsumption.objects.filter(work_order=wo).first()
        # 2 dozen per run + 50% scrap = 3 dozen = 36 pieces, for 3 runs.
        self.expect("the plan converts the recipe into the unit the store keeps",
                    consumption is not None and consumption.planned_quantity == 108,
                    f"planned {consumption and consumption.planned_quantity}, expected 108 pieces")

        wrong = self.reject(client, "a tonne for a piece-counted item",
                            reverse("manufacturing:bom_create"), merge({
                                "name": f"Bad recipe {self.stamp}",
                                "product": self.product.pk, "version": "1.0",
                                "status": "ACTIVE", "production_quantity": "1", "notes": "",
                            }, formset("lines", [
                                {"component": self.product.pk, "quantity": "1",
                                 "unit_of_measure": self.unit("TON").pk, "scrap_percent": "0"},
                            ], blank={"component": "", "quantity": "",
                                      "unit_of_measure": "", "scrap_percent": "0"})))
        self.expect("a weight unit is refused on a component counted in pieces",
                    wrong.status_code == 200 and not BillOfMaterials.objects.filter(
                        company=company, name=f"Bad recipe {self.stamp}").exists(),
                    f"HTTP {wrong.status_code}")

        self.note("Units are per company, one base per kind, and every derived unit "
                  "converts through that base — so a recipe written in dozens can draw "
                  "stock counted in pieces without anyone doing the arithmetic by hand.")

    # ──────────────────────────── multi-currency ─────────────────────────────

    def ar_position(self):
        """(receivables control account, what the invoice book says is owed)."""
        from apps.sales.models import Invoice

        book = sum((invoice.outstanding_base for invoice in Invoice.objects.filter(
            company=self.company).exclude(status__in=["DRAFT", "CANCELLED"])), Z)
        return self.account("1300").current_balance, book

    def ap_position(self):
        """(payables control account, what the bill book says is owed)."""
        from apps.procurement.models import Bill

        book = sum((bill.outstanding_base for bill in Bill.objects.filter(
            company=self.company).exclude(status__in=["DRAFT", "CANCELLED"])), Z)
        return self.account("2100").current_balance, book

    def line_total(self, entry, code, side):
        """What one account took on one side of one entry."""
        return sum((getattr(line, f"{side}_amount") for line in entry.lines.all()
                    if line.account.code == code), Z)

    def balanced(self, entry):
        dr = sum((line.debit_amount for line in entry.lines.all()), Z)
        cr = sum((line.credit_amount for line in entry.lines.all()), Z)
        return dr, cr, dr == cr and dr > Z

    def verify_multi_currency(self, client):
        """Foreign paper is raised in its own money and booked in ours.

        A rate is a fact about a day, not a constant: a dollar invoice raised
        while the dollar bought 130 shillings keeps that value forever, the cash
        that settles it a week later is worth a different number of shillings,
        and the gap between the two is real money that has to arrive in the
        exchange gain and loss accounts. Everything downstream of the conversion
        — control accounts, subledgers, reports — only ever counts one currency.
        """
        from django.core.exceptions import ValidationError
        from django.db import transaction as db_transaction

        from apps.accounting import gl_service
        from apps.accounting.models import Currency, ExchangeRate, JournalEntry
        from apps.procurement.models import Bill
        from apps.sales.models import CustomerPayment, Invoice, PaymentAllocation

        company = self.company
        book_code = (company.currency or "KES").upper()
        usd = Currency.objects.filter(code="USD").first()
        eur = Currency.objects.filter(code="EUR").first()
        kes = Currency.objects.filter(code=book_code).first()
        if not self.expect("the shared currency register was seeded", usd and eur and kes,
                           f"USD {bool(usd)}, EUR {bool(eur)}, {book_code} {bool(kes)}"):
            return

        old = self.today - datetime.timedelta(days=7)
        ahead = self.today + datetime.timedelta(days=20)
        rate_url = reverse("accounting:exchange_rate_create")

        # ── the screens a bookkeeper uses to file rates ──
        self.get_ok(client, "currency register", reverse("accounting:currency_list"))
        self.get_ok(client, "new currency form", reverse("accounting:currency_create"))
        self.get_ok(client, "new rate form", rate_url)

        filing = lambda currency, on_date, value, source="MANUAL": {
            "currency": currency.pk, "effective_date": str(on_date), "rate": value,
            "source": source, "note": "Filed through the rate form",
        }
        self.reject(client, "a rate of zero", rate_url, filing(usd, self.today, "0"))
        self.reject(client, "a negative rate", rate_url, filing(usd, self.today, "-1"))
        self.reject(client, "a rate for the book's own currency", rate_url,
                    filing(kes, self.today, "1"))
        self.expect("nothing was filed by the rejected postings",
                    not ExchangeRate.objects.filter(company=company).exists(),
                    f"{ExchangeRate.objects.filter(company=company).count()} rates")

        self.expect("the book's own money is always worth one of itself",
                    ExchangeRate.rate_for(company, book_code, self.today) == D("1"),
                    str(ExchangeRate.rate_for(company, book_code, self.today)))
        try:
            ExchangeRate.rate_for(company, "USD", self.today)
            self.expect("an unpriced currency cannot be valued", False, "a rate came back")
        except ValidationError as exc:
            self.expect("an unpriced currency cannot be valued",
                        "No exchange rate for USD" in " ".join(exc.messages),
                        str(exc.messages))

        # ── filing the first rate, and the walls around it ──
        self.post(client, "exchange rate", rate_url, filing(usd, old, "130", "CBK"))
        filed = ExchangeRate.objects.filter(company=company, currency=usd).first()
        if not self.expect("the rate was filed", filed is not None):
            return
        self.expect("the filing is this book's own",
                    ExchangeRate.objects.filter(company=company).count() == 1,
                    f"{ExchangeRate.objects.filter(company=company).count()} rates")
        try:
            ExchangeRate.rate_for(self.fx_company, "USD", self.today)
            self.expect("another company's book does not inherit this rate", False,
                        "it read our filing")
        except ValidationError:
            self.expect("another company's book does not inherit this rate", True)
        self.reject(client, "a second rate for the same day", rate_url,
                    filing(usd, old, "131"))

        offered = [code for code, _ in client.get(
            reverse("sales:invoice_create")).context["form"].fields["currency"].choices]
        self.expect("a priced currency is now offered on the invoice form",
                    "USD" in offered, str(offered))
        self.expect("an unpriced currency is still withheld",
                    "EUR" not in offered, str(offered))

        # ── a dollar invoice into a shilling book ──
        ar_ledger, ar_book = self.ar_position()
        self.expect("receivables agree with the invoice book before any foreign paper",
                    ar_ledger == ar_book, f"{ar_ledger} against {ar_book}")

        self.post(client, "dollar invoice", reverse("sales:invoice_create"), merge({
            "reference": "INV-USD-1", "customer": self.customer.pk,
            "invoice_date": str(self.today),
            "due_date": str(self.today + datetime.timedelta(days=25)),
            "currency": "USD", "notes": "Raised while the dollar bought 130",
        }, formset("lines", [
            {"product": self.product.pk, "description": "Widgets", "quantity": "1",
             "unit_price": "100", "discount_percent": "0", "tax_rate": self.vat.pk},
        ], blank=BLANK_INVOICE_LINE)))
        dollar = Invoice.objects.filter(company=company, reference="INV-USD-1").first()
        if not self.expect("dollar invoice created", dollar is not None):
            return
        self.expect("the invoice holds the rate of its own day",
                    dollar.exchange_rate == D("130"), str(dollar.exchange_rate))
        self.expect("its value in our money is stored beside the foreign total",
                    dollar.total_amount_base == D("15080.00"), str(dollar.total_amount_base))

        entry = dollar.journal_entry
        if self.expect("the dollar invoice reached the ledger", entry is not None):
            self.expect("the entry says what money it came from",
                        entry.source_currency == "USD" and entry.exchange_rate == D("130"),
                        f"{entry.source_currency} @ {entry.exchange_rate}")
            self.expect("the receivable is booked in our money",
                        self.line_total(entry, "1300", "debit") == D("15080.00"),
                        str(self.line_total(entry, "1300", "debit")))
            self.expect("revenue converts at the same rate",
                        self.line_total(entry, "4200", "credit") == D("13000.00"),
                        str(self.line_total(entry, "4200", "credit")))
            self.expect("the tax on it converts too",
                        self.line_total(entry, "2200", "credit") == D("2080.00"),
                        str(self.line_total(entry, "2200", "credit")))
            dr, cr, ok = self.balanced(entry)
            self.expect("a converted journal still balances", ok and dr == D("15080.00"),
                        f"Dr {dr} / Cr {cr}")
        ar_ledger, ar_book = self.ar_position()
        self.expect("the converted invoice moved the control account and the book alike",
                    ar_ledger == ar_book == D("15460.00"), f"{ar_ledger} against {ar_book}")

        # ── the rate moves; the booked invoice does not ──
        self.post(client, "today's dollar rate", rate_url, filing(usd, self.today, "135", "BANK"))
        self.expect("the newest rate on or before the day is the one used",
                    ExchangeRate.rate_for(company, "USD", self.today) == D("135"),
                    str(ExchangeRate.rate_for(company, "USD", self.today)))
        dollar.refresh_from_db()
        self.expect("a document keeps the rate it was raised at",
                    dollar.rate == D("130"), str(dollar.rate))

        self.post(client, "dollar receipt", reverse("sales:payment_create"), {
            "reference": "PAY-USD-1", "customer": self.customer.pk,
            "payment_date": str(self.today), "amount": "116", "currency": "USD",
            "payment_method": "MPESA", "transaction_reference": "FX-0001",
            "notes": "Settles the dollar invoice a week on",
        })
        receipt = CustomerPayment.objects.filter(company=company, reference="PAY-USD-1").first()
        if not self.expect("dollar receipt created", receipt is not None):
            return
        self.expect("the cash coming in is worth today's dollars",
                    receipt.amount_base == D("15660.00"), str(receipt.amount_base))
        alloc = PaymentAllocation.objects.filter(payment=receipt).first()
        self.expect("a dollar receipt settles a dollar invoice, and only that one",
                    alloc is not None and alloc.invoice_id == dollar.pk,
                    str(alloc and alloc.invoice.reference))
        self.expect("the receivable is cleared at the value it was raised at",
                    alloc is not None and alloc.base_amount == D("15080.00"),
                    str(alloc and alloc.base_amount))
        shilling_invoice = Invoice.objects.filter(company=company,
                                                  reference="INV-SMOKE-1").first()
        self.expect("a shilling invoice is untouched by a dollar receipt",
                    shilling_invoice is not None
                    and shilling_invoice.paid_amount == D("500.00"),
                    str(shilling_invoice and shilling_invoice.paid_amount))
        entry = receipt.journal_entry
        if self.expect("the receipt reached the ledger", entry is not None):
            self.expect("the bank is taken in at the day's rate",
                        self.line_total(entry, "1200", "debit") == D("15660.00"),
                        str(self.line_total(entry, "1200", "debit")))
            self.expect("receivables leave at the booked rate",
                        self.line_total(entry, "1300", "credit") == D("15080.00"),
                        str(self.line_total(entry, "1300", "credit")))
            self.expect("the gap is realised exchange gain",
                        self.line_total(entry, "4310", "credit") == D("580.00")
                        and self.line_total(entry, "7300", "debit") == Z,
                        f"gain {self.line_total(entry, '4310', 'credit')}")
            dr, cr, ok = self.balanced(entry)
            self.expect("a settled foreign document balances", ok and dr == D("15660.00"),
                        f"Dr {dr} / Cr {cr}")

        # ── the other direction: a weaker dollar at settlement ──
        self.post(client, "forward dollar rate", rate_url, filing(usd, ahead, "128", "MANUAL"))
        self.post(client, "second dollar invoice", reverse("sales:invoice_create"), merge({
            "reference": "INV-USD-2", "customer": self.customer.pk,
            "invoice_date": str(self.today), "due_date": str(ahead),
            "currency": "USD", "notes": "Untaxed, raised at today's rate",
        }, formset("lines", [
            {"product": self.product.pk, "description": "Widgets", "quantity": "2",
             "unit_price": "100", "discount_percent": "0", "tax_rate": ""},
        ], blank=BLANK_INVOICE_LINE)))
        second = Invoice.objects.filter(company=company, reference="INV-USD-2").first()
        if not self.expect("second dollar invoice created", second is not None):
            return
        self.expect("a document raised today is valued at today's rate",
                    second.exchange_rate == D("135")
                    and second.total_amount_base == D("27000.00"),
                    f"{second.exchange_rate} -> {second.total_amount_base}")

        # ── a report must not add dollars to shillings ──
        buckets = client.get(reverse("sales:ar_aging")).context["buckets"]
        self.expect("AR ageing carries the open dollar invoice at its converted value",
                    buckets["current"] == D("27380.00"),
                    f"current {buckets['current']} — 380 shilling plus 27,000 converted dollars")

        self.post(client, "dollar receipt at a weaker dollar",
                  reverse("sales:payment_create"), {
                      "reference": "PAY-USD-2", "customer": self.customer.pk,
                      "payment_date": str(ahead), "amount": "200", "currency": "USD",
                      "payment_method": "MPESA", "transaction_reference": "FX-0002",
                      "notes": "Twenty dollars short per unit",
                  })
        receipt = CustomerPayment.objects.filter(company=company, reference="PAY-USD-2").first()
        entry = receipt and receipt.journal_entry
        if self.expect("the second receipt reached the ledger", entry is not None):
            self.expect("cash in at the weaker rate",
                        self.line_total(entry, "1200", "debit") == D("25600.00"),
                        str(self.line_total(entry, "1200", "debit")))
            self.expect("receivable out at the booked rate",
                        self.line_total(entry, "1300", "credit") == D("27000.00"),
                        str(self.line_total(entry, "1300", "credit")))
            self.expect("the shortfall is realised exchange loss, not a silent rounding",
                        self.line_total(entry, "7300", "debit") == D("1400.00")
                        and self.line_total(entry, "4310", "credit") == Z,
                        f"loss {self.line_total(entry, '7300', 'debit')}")
            dr, cr, ok = self.balanced(entry)
            self.expect("a loss-making settlement still balances", ok and dr == D("27000.00"),
                        f"Dr {dr} / Cr {cr}")

        # ── the same discipline on the payable side ──
        ap_ledger, ap_book = self.ap_position()
        self.post(client, "dollar bill", reverse("procurement:bill_create"), {
            "reference": "BILL-USD-1", "supplier": self.supplier.pk, "purchase_order": "",
            "supplier_reference": "USD-77", "bill_date": str(self.today),
            "due_date": str(ahead), "currency": "USD",
            "subtotal": "100", "tax_amount": "0", "total_amount": "100",
            "notes": "Bought in dollars",
        })
        bill = Bill.objects.filter(company=company, reference="BILL-USD-1").first()
        if not self.expect("dollar bill created", bill is not None):
            return
        self.expect("the bill is worth its day's rate in our money",
                    bill.exchange_rate == D("135")
                    and bill.total_amount_base == D("13500.00"),
                    f"{bill.exchange_rate} -> {bill.total_amount_base}")
        if self.expect("the dollar bill reached the ledger", bill.journal_entry_id is not None):
            self.expect("payables are credited in our money",
                        self.line_total(bill.journal_entry, "2100", "credit") == D("13500.00")
                        and bill.journal_entry.source_currency == "USD",
                        str(self.line_total(bill.journal_entry, "2100", "credit")))
        ap_ledger_now, ap_book_now = self.ap_position()
        self.expect("payables and the bill book both grew by the converted amount",
                    ap_ledger_now - ap_ledger == D("13500.00")
                    and ap_book_now - ap_book == D("13500.00"),
                    f"account {ap_ledger_now - ap_ledger}, book {ap_book_now - ap_book}")

        resp = self.post(client, "dollar bill payment",
                         reverse("procurement:bill_pay", args=[bill.pk]),
                         {"amount": "100", "payment_date": str(ahead)})
        bill.refresh_from_db()
        self.expect_flash(client, resp, "posted", "dollar payment surfaced")
        self.expect("the dollar bill is paid off",
                    bill.status == "PAID" and bill.outstanding_amount == Z,
                    f"{bill.status} / {bill.outstanding_amount}")
        payment_entry = JournalEntry.objects.filter(
            company=company, entry_type="PAYMENT",
            notes__icontains="BILL-USD-1").first()
        if self.expect("the bill payment has its own journal", payment_entry is not None):
            self.expect("payables leave at the rate the bill was raised at",
                        self.line_total(payment_entry, "2100", "debit") == D("13500.00"),
                        str(self.line_total(payment_entry, "2100", "debit")))
            self.expect("cash goes out at the payment day's rate",
                        self.line_total(payment_entry, "1100", "credit") == D("12800.00"),
                        str(self.line_total(payment_entry, "1100", "credit")))
            self.expect("a cheaper dollar to pay with is a gain",
                        self.line_total(payment_entry, "4310", "credit") == D("700.00"),
                        str(self.line_total(payment_entry, "4310", "credit")))
            dr, cr, ok = self.balanced(payment_entry)
            self.expect("the paid-off bill balances", ok and dr == D("13500.00"),
                        f"Dr {dr} / Cr {cr}")
        ap_ledger, ap_book = self.ap_position()
        self.expect("the payable side closes back to the bill book",
                    ap_ledger - ap_ledger_now == D("-13500.00")
                    and ap_book == ap_book_now - D("13500.00"),
                    f"account {ap_ledger}, book {ap_book}")

        # ── a currency nobody priced must never reach the book ──
        orphan = Invoice.objects.create(
            company=company, customer=self.customer, reference="INV-EUR-1",
            invoice_date=self.today, due_date=self.today, currency="EUR",
            subtotal=D("100.00"), tax_amount=Z, total_amount=D("100.00"),
            total_amount_base=D("100.00"))
        try:
            with db_transaction.atomic():
                gl_service.post_invoice(orphan, self.user)
            self.expect("an unpriced currency cannot be posted to the ledger", False,
                        "the euro invoice posted")
        except ValidationError as exc:
            self.expect("an unpriced currency cannot be posted to the ledger",
                        "No exchange rate for EUR" in " ".join(exc.messages),
                        str(exc.messages))
        self.expect("no half-written euro journal survives",
                    not JournalEntry.objects.filter(
                        company=company, source_currency="EUR").exists(),
                    f"{JournalEntry.objects.filter(company=company, source_currency='EUR').count()}")

        # ── the paper trail ──
        history = client.get(reverse("accounting:exchange_rate_history", args=["USD"]))
        self.expect("the rate history lists every filing for that currency",
                    history.status_code == 200 and len(history.context["rates"]) == 3,
                    f"HTTP {history.status_code}, {len(history.context['rates'])} rows")
        rows = {row["currency"].code: row for row in
                client.get(reverse("accounting:currency_list")).context["rows"]}
        self.expect("the register carries the newest filing on record",
                    rows["USD"]["rate"].rate == D("128")
                    and rows["USD"]["count"] == 3 and rows["USD"]["usable"],
                    f"{rows['USD']['rate'].rate} from {rows['USD']['count']} filings")
        self.expect("an unpriced currency is named but marked unusable",
                    not rows["EUR"]["usable"] and "No rate filed" in rows["EUR"]["note"],
                    rows["EUR"]["note"])
        self.expect("the book currency is marked as the one this book counts in",
                    rows[book_code]["is_book"] and rows[book_code]["usable"])
        self.expect("a filed rate can be corrected",
                    client.get(reverse("accounting:exchange_rate_edit",
                                       args=[rows["USD"]["rate"].pk])).status_code == 200)

        # ── the whole book, after all of it ──
        lines = self.posted_lines()
        dr = sum((line.debit_amount for line in lines), Z)
        cr = sum((line.credit_amount for line in lines), Z)
        self.expect("every converted posting kept the ledger balanced", dr == cr,
                    f"Dr {dr} / Cr {cr}")
        ar_ledger, ar_book = self.ar_position()
        self.expect("receivables still equal the invoice book, now in two currencies",
                    ar_ledger == ar_book, f"{ar_ledger} against {ar_book}")
        for label, url in [("AR ageing", reverse("sales:ar_aging")),
                           ("AP ageing", reverse("procurement:ap_aging")),
                           ("trial balance", reverse("accounting:trial_balance"))]:
            self.get_ok(client, f"{label} report with foreign documents in it", url)

        self.note("Foreign paper converts once, on the document, at the rate filed for its "
                  "day; the ledger, the control accounts and the reports only ever see this "
                  "book's money, so the trial balance and the invoice book still agree. A "
                  "balance still open at a period end is read at that period end's rate in "
                  "section 17, which is where the unrealised half of exchange is handled.")

    # ─────────────────────────── document lifecycle ──────────────────────────

    def verify_lifecycle(self, client):
        """Orders become documents, documents get answered, mistakes get reversed.

        Conversion is the everyday movement — an order is billed, goods arrive,
        a supplier's invoice is entered — and it has to be counted, not typed
        again: the order remembers what has already been invoiced so nobody
        bills twice, and a bill is raised against what actually arrived rather
        than what was ordered. When a document is wrong, the book is not edited.
        A credit or debit note answers it with a second entry, and a cancellation
        offsets it, so the trail still shows both sides of the correction.
        """
        from apps.accounting.models import JournalEntry
        from apps.inventory.models import StockLevel, StockMovement
        from apps.procurement.models import Bill, PurchaseOrder, PurchaseOrderLine
        from apps.sales.models import Invoice, SalesOrder

        company = self.company

        def bill_of(reference):
            return Bill.objects.filter(company=company, reference=reference).first()

        def order_lines(order):
            return list(order.lines.order_by("id"))

        # ── A. a sales order is billed, and remembers being billed ──
        order = SalesOrder.objects.filter(company=company, reference="SO-SMOKE-1").first()
        if not self.expect("the section 5 order is here to convert", order is not None):
            return
        lines_before = self.posted_lines().count()

        resp = self.post(client, "draft order conversion",
                         reverse("sales:order_invoice", args=[order.pk]), {})
        self.expect("an unconfirmed order cannot be invoiced",
                    order.status == "DRAFT" and not order.invoices.exists(),
                    f"{order.status}, {order.invoices.count()} invoices")
        self.expect_flash(client, resp, "confirm it before invoicing",
                          "the refusal says what to do about it")

        self.post(client, "confirm order", reverse("sales:order_confirm", args=[order.pk]), {})
        order.refresh_from_db()
        self.expect("a draft order is confirmed through the button",
                    order.status == "CONFIRMED", order.status)
        self.expect("confirming an order moves no money",
                    self.posted_lines().count() == lines_before)

        resp = self.post(client, "order to invoice",
                         reverse("sales:order_invoice", args=[order.pk]), {})
        made = None
        if resp.status_code in (301, 302, 303):
            match = re.search(r"/invoices/([0-9a-fA-F-]{36})/?$", resp["Location"])
            if match:
                made = Invoice.objects.filter(
                    company=company, pk=match.group(1)).first()
        if not self.expect("the order became an invoice", made is not None,
                           str(resp.get("Location"))):
            return
        self.expect("the invoice says which order it came from",
                    made.sales_order_id == order.pk and made.customer_id == order.customer_id)
        self.expect("the order's discount reached the lines",
                    made.subtotal == D("1140.00") and made.tax_amount == D("152.00"),
                    f"{made.subtotal} / {made.tax_amount}")
        self.expect("the invoice total matches the order it was raised from",
                    made.total_amount == order.total_amount,
                    f"{made.total_amount} against {order.total_amount}")
        self.expect("a converted invoice arrives as a draft",
                    made.status == "DRAFT" and made.journal_entry_id is None, made.status)
        self.expect("drafting an invoice moves no money",
                    self.posted_lines().count() == lines_before)

        billed = {l.product_id: l.invoiced_quantity for l in order_lines(order)}
        self.expect("the order recorded what has been invoiced",
                    all(l.invoiced_quantity == l.quantity for l in order_lines(order)),
                    str(billed))
        order.refresh_from_db()
        self.expect("a fully invoiced order is marked invoiced",
                    order.status == "INVOICED", order.status)

        resp = self.post(client, "second conversion of the same order",
                         reverse("sales:order_invoice", args=[order.pk]), {})
        self.expect("an order is not invoiced twice",
                    order.invoices.count() == 1, f"{order.invoices.count()} invoices")
        self.expect_flash(client, resp, "already invoiced",
                          "the second attempt explains itself")

        self.post(client, "post converted invoice",
                  reverse("sales:invoice_post", args=[made.pk]), {})
        made.refresh_from_db()
        entry = made.journal_entry
        if not self.expect("the converted invoice posted to the GL", entry is not None):
            return
        self.expect("the converted invoice books the receivable",
                    self.line_total(entry, "1300", "debit") == D("1292.00"),
                    str(self.line_total(entry, "1300", "debit")))
        self.expect("revenue is booked net of the order discount",
                    self.line_total(entry, "4200", "credit") == D("1140.00")
                    and self.line_total(entry, "2200", "credit") == D("152.00"),
                    f"4200 {self.line_total(entry, '4200', 'credit')}, "
                    f"2200 {self.line_total(entry, '2200', 'credit')}")
        dr, cr, ok = self.balanced(entry)
        self.expect("the converted entry balances", ok and dr == D("1292.00"), f"Dr {dr} / Cr {cr}")

        # ── B. a credit note answers the invoice, both stay in the book ──
        first_line = made.lines.order_by("id").first()
        resp = self.post(client, "credit note",
                         reverse("sales:invoice_credit_note", args=[made.pk]),
                         {f"credit_qty_{first_line.pk}": "2", "reason": "Two widgets returned"})
        if resp.status_code != 302:
            self.show_errors(resp)
        note = made.credit_notes.first()
        if not self.expect("a credit note is a separate document", note is not None):
            return
        self.expect("it answers the invoice it was raised against",
                    note.original_invoice_id == made.pk and note.currency == made.currency,
                    f"{note.original_invoice_id} / {note.currency}")
        self.expect("the credited quantity is priced like the original",
                    note.subtotal == D("190.00") and note.tax_amount == D("30.40")
                    and note.total_amount == D("220.40"),
                    f"{note.subtotal} / {note.tax_amount} / {note.total_amount}")
        self.expect("a credit note carries its own entry",
                    note.journal_entry_id is not None
                    and note.journal_entry.entry_type == "CREDIT_NOTE",
                    note.journal_entry.entry_type if note.journal_entry_id else "none")
        cn = note.journal_entry
        self.expect("the credit note puts revenue and VAT back",
                    self.line_total(cn, "4200", "debit") == D("190.00")
                    and self.line_total(cn, "2200", "debit") == D("30.40"),
                    f"4200 {self.line_total(cn, '4200', 'debit')}, "
                    f"2200 {self.line_total(cn, '2200', 'debit')}")
        self.expect("and clears the receivable by the same amount",
                    self.line_total(cn, "1300", "credit") == D("220.40"),
                    str(self.line_total(cn, "1300", "credit")))
        dr, cr, ok = self.balanced(cn)
        self.expect("the credit note entry balances", ok, f"Dr {dr} / Cr {cr}")
        self.expect("the invoice being credited keeps its own entry untouched",
                    made.journal_entry.status == "POSTED"
                    and made.journal_entry.reversed_by_id is None
                    and self.line_total(made.journal_entry, "1300", "debit") == D("1292.00"),
                    f"{made.journal_entry.status}, {made.journal_entry.reversed_by_id}")
        made.refresh_from_db()
        self.expect("the invoice remembers what has been credited",
                    made.credited_amount == D("220.40")
                    and made.outstanding_amount == D("1071.60"),
                    f"credited {made.credited_amount}, owed {made.outstanding_amount}")
        self.expect("a credit note is not itself money owed",
                    note.outstanding_amount == Z and note.status == "CREDIT_NOTE",
                    f"{note.outstanding_amount} / {note.status}")

        over = {f"credit_qty_{l.pk}": str(l.quantity) for l in made.lines.order_by("id")}
        over["reason"] = "too far"
        count_before = Invoice.objects.filter(company=company).count()
        resp = self.post(client, "over-generous credit note",
                         reverse("sales:invoice_credit_note", args=[made.pk]), over)
        self.expect("a credit note cannot exceed what is still owed",
                    Invoice.objects.filter(company=company).count() == count_before,
                    f"{over} against outstanding {made.outstanding_amount}")
        self.expect_flash(client, resp, "more than", "the refusal names the limit")
        made.refresh_from_db()
        self.expect("a refused credit note left no trace on the invoice",
                    made.credited_amount == D("220.40"), str(made.credited_amount))

        ar_ledger, ar_book = self.ar_position()
        self.expect("receivables equal the invoice book with a credit note in it",
                    ar_ledger == ar_book, f"{ar_ledger} against {ar_book}")

        resp = self.post(client, "cancel a credited invoice",
                         reverse("sales:invoice_cancel", args=[made.pk]), {})
        made.refresh_from_db()
        self.expect("an invoice with credit against it is not simply cancelled",
                    made.status != "CANCELLED", made.status)
        self.expect_flash(client, resp, "cannot cancel", "the refusal explains why")

        # ── C. cancelling a clean invoice offsets it and frees the order ──
        header = {
            "reference": "SO-SMOKE-2", "customer": self.customer.pk,
            "order_date": str(self.today),
            "delivery_date": str(self.today + datetime.timedelta(days=3)),
            "warehouse": self.warehouse.pk, "sales_rep": self.user.pk,
            "currency": "KES", "discount_percent": "0", "branch": "",
            "notes": "Second smoke order",
        }
        self.post(client, "second sales order", reverse("sales:order_create"),
                  merge(header, formset("lines", [
                      {"product": self.product.pk, "description": "Returnable batch",
                       "quantity": "4", "unit_price": "50", "discount_percent": "0",
                       "tax_rate": self.vat.pk},
                  ], blank=BLANK_INVOICE_LINE)))
        order2 = SalesOrder.objects.filter(company=company, reference="SO-SMOKE-2").first()
        if not self.expect("a second order exists to revert", order2 is not None):
            return
        self.post(client, "confirm second order",
                  reverse("sales:order_confirm", args=[order2.pk]), {})
        self.post(client, "convert second order",
                  reverse("sales:order_invoice", args=[order2.pk]), {})
        inv_b = order2.invoices.first()
        if not self.expect("the second order converted", inv_b is not None):
            return
        self.expect("a clean conversion of a discount-free order",
                    inv_b.total_amount == D("232.00"), str(inv_b.total_amount))
        self.post(client, "post second invoice",
                  reverse("sales:invoice_post", args=[inv_b.pk]), {})
        inv_b.refresh_from_db()
        entry_b = inv_b.journal_entry
        ar_after_b = self.account("1300").current_balance

        resp = self.post(client, "cancel a posted invoice",
                         reverse("sales:invoice_cancel", args=[inv_b.pk]), {})
        inv_b.refresh_from_db()
        entry_b.refresh_from_db()
        self.expect("cancelling a posted invoice does not delete it",
                    inv_b.status == "CANCELLED" and entry_b.status == "POSTED",
                    f"{inv_b.status} / {entry_b.status}")
        reversal = entry_b.reversed_by
        if not self.expect("an offsetting entry was posted instead", reversal is not None):
            return
        self.expect("the reversal is its own posted entry",
                    reversal.status == "POSTED" and reversal.entry_type == "REVERSAL",
                    f"{reversal.status} / {reversal.entry_type}")
        dr, cr, ok = self.balanced(reversal)
        self.expect("the reversal balances", ok and dr == D("232.00"), f"Dr {dr} / Cr {cr}")
        self.expect("the reversal runs the original backwards",
                    self.line_total(reversal, "1300", "credit") == D("232.00")
                    and self.line_total(reversal, "4200", "debit") == D("200.00")
                    and self.line_total(reversal, "2200", "debit") == D("32.00"),
                    f"1300 {self.line_total(reversal, '1300', 'credit')}")
        self.expect("the reversal is dated in the original's period",
                    reversal.period_id == entry_b.period_id
                    and reversal.posting_date == entry_b.posting_date,
                    f"{reversal.posting_date} in {reversal.period_id}")
        self.expect("cancelling put the receivable back where it was",
                    self.account("1300").current_balance == ar_after_b - D("232.00"),
                    str(self.account("1300").current_balance))
        order2.refresh_from_db()
        self.expect("the cancelled invoice gave its quantities back to the order",
                    all(l.invoiced_quantity == Z for l in order_lines(order2))
                    and order2.status == "CONFIRMED",
                    f"{[str(l.invoiced_quantity) for l in order_lines(order2)]} / {order2.status}")
        self.post(client, "re-convert after revert",
                  reverse("sales:order_invoice", args=[order2.pk]), {})
        order2.refresh_from_db()
        again = order2.invoices.exclude(pk=inv_b.pk).first()
        self.expect("the order can be billed again once the mistake is reversed",
                    again is not None and again.total_amount == D("232.00")
                    and again.status == "DRAFT",
                    f"{again.reference if again else 'none'} / "
                    f"{again.total_amount if again else ''}")
        self.expect("a cancelled invoice leaves no balance behind",
                    inv_b.outstanding_amount == D("232.00")
                    and inv_b.status == "CANCELLED",
                    "the ledger is net, so the book must exclude it too")
        ar_ledger, ar_book = self.ar_position()
        self.expect("receivables still equal the invoice book after a cancellation",
                    ar_ledger == ar_book, f"{ar_ledger} against {ar_book}")

        # ── D. purchasing: lines, goods in, three-way matched bill ──
        # Payables are a bill book and nothing else, so the two sides start and
        # end together however much paperwork moves in between: what is being
        # proved here is that a receipt, a bill, a cancellation and a debit note
        # leave the distance exactly as they found it.
        ap_ledger0, ap_book0 = self.ap_position()
        ap_gap = ap_ledger0 - ap_book0
        self.post(client, "purchase order", reverse("procurement:po_create"), {
            "reference": "PO-SMOKE-1", "supplier": self.supplier.pk,
            "order_date": str(self.today),
            "expected_delivery_date": str(self.today + datetime.timedelta(days=7)),
            "warehouse": self.warehouse.pk, "notes": "Buy ten drums and five small ones",
        })
        po = PurchaseOrder.objects.filter(company=company, reference="PO-SMOKE-1").first()
        if not self.expect("the order arrived without lines", po is not None
                           and po.lines.count() == 0):
            return
        self.post(client, "PO line", reverse("procurement:po_line_add", args=[po.pk]), {
            "product": self.product.pk, "quantity": "10", "unit_cost": "500",
            "tax_rate": self.vat.pk, "description": "Drums"})
        self.post(client, "PO line two", reverse("procurement:po_line_add", args=[po.pk]), {
            "product": self.product.pk, "quantity": "5", "unit_cost": "200", "tax_rate": ""})
        po.refresh_from_db()
        self.expect("lines added through the order screen", po.lines.count() == 2,
                    str(po.lines.count()))
        self.expect("the order total is the sum of its lines",
                    po.subtotal == D("6000.00") and po.tax_amount == D("800.00")
                    and po.total_amount == D("6800.00"),
                    f"{po.subtotal} / {po.tax_amount} / {po.total_amount}")

        resp = self.post(client, "receive a draft order",
                         reverse("procurement:grn_record", args=[po.pk]), {})
        self.expect("goods cannot arrive before the order is sent",
                    po.lines.filter(received_quantity__gt=0).count() == 0, po.status)
        self.expect_flash(client, resp, "send it to the supplier", "the refusal says why")

        self.post(client, "send PO", reverse("procurement:po_send", args=[po.pk]), {})
        po.refresh_from_db()
        self.expect("the order is on its way", po.status == "SENT", po.status)

        l1, l2 = list(po.lines.order_by("id"))
        level = StockLevel.objects.filter(product=self.product, warehouse=self.warehouse).first()
        qty_before = level.quantity if level else Z
        moves_before = StockMovement.objects.filter(product=self.product).count()
        je_before = self.posted_lines().count()
        stock_before = self.account("1400").current_balance
        accrual_before = self.account("2110").current_balance
        ap_before = self.account("2100").current_balance
        resp = self.post(client, "goods receipt", reverse("procurement:grn_record", args=[po.pk]),
                         {f"receive_qty_{l1.pk}": "10", f"receive_qty_{l2.pk}": "0",
                          "warehouse": self.warehouse.pk})
        l1.refresh_from_db()
        po.refresh_from_db()
        l2 = po.lines.order_by("id")[1]
        self.expect("the receipt counted what arrived against the line",
                    l1.received_quantity == D("10.0000") and l2.received_quantity == Z,
                    f"{l1.received_quantity} / {l2.received_quantity}")
        self.expect("a part receipt marks the order partially received",
                    po.status == "PARTIALLY_RECEIVED", po.status)
        level.refresh_from_db()
        self.expect("the goods are on the shelf", level.quantity == qty_before + D("10.0000"),
                    f"{qty_before} + 10 -> {level.quantity}")
        self.expect("one movement per received line",
                    StockMovement.objects.filter(product=self.product).count()
                    == moves_before + 1, str(level.quantity))
        self.expect("the receipt values the shelf and accrues for the goods",
                    self.posted_lines().count() == je_before + 2
                    and self.account("1400").current_balance == stock_before + D("5000.00")
                    and self.account("2110").current_balance == accrual_before + D("5000.00"),
                    f"1400 +{self.account('1400').current_balance - stock_before}, "
                    f"2110 +{self.account('2110').current_balance - accrual_before}")
        self.expect("a delivery note is not a payable",
                    self.account("2100").current_balance == ap_before,
                    f"2100 +{self.account('2100').current_balance - ap_before}")

        resp = self.post(client, "over-receipt",
                         reverse("procurement:grn_record", args=[po.pk]),
                         {f"receive_qty_{l1.pk}": "4", "warehouse": self.warehouse.pk})
        l1.refresh_from_db()
        self.expect("a line cannot receive more than was ordered",
                    l1.received_quantity == D("10.0000"), str(l1.received_quantity))
        self.expect_flash(client, resp, "still outstanding", "the refusal gives the number")

        resp = self.post(client, "bill a partly received order",
                         reverse("procurement:po_to_bill", args=[po.pk]), {})
        b1 = po.bills.filter(status="DRAFT").first()
        if not self.expect("the bill was raised from what arrived", b1 is not None):
            return
        self.expect("a bill is raised for the received quantity only",
                    b1.subtotal == D("5000.00") and b1.tax_amount == D("800.00")
                    and b1.total_amount == D("5800.00"),
                    f"{b1.subtotal} / {b1.tax_amount} / {b1.total_amount}")
        self.expect("the bill names the order it answers",
                    b1.purchase_order_id == po.pk and b1.supplier_id == po.supplier_id)
        self.expect("a converted bill arrives as a draft with no entry",
                    b1.journal_entry_id is None and b1.status == "DRAFT", b1.status)
        resp = self.post(client, "debit note on an unposted bill",
                         reverse("procurement:bill_debit_note", args=[b1.pk]),
                         {"amount": "100"})
        self.expect("an unposted bill cannot be debited",
                    not Bill.objects.filter(company=company, original_bill=b1).exists(),
                    "a debit note was raised against paper the ledger never saw")
        self.expect_flash(client, resp, "not been posted", "the refusal points at posting")
        accrual_before = self.account("2110").current_balance
        stock_before = self.account("1400").current_balance
        self.post(client, "post the bill", reverse("procurement:bill_post", args=[b1.pk]), {})
        b1.refresh_from_db()
        je1 = b1.journal_entry
        dn_debit = self.line_total(je1, "2110", "debit")
        inv_debit = self.line_total(je1, "1400", "debit")
        vat_debit = self.line_total(je1, "2210", "debit")
        ap_credit = self.line_total(je1, "2100", "credit")
        self.expect("the bill releases the accrual the goods came in on",
                    dn_debit == D("5000.00") and vat_debit == D("800.00")
                    and ap_credit == D("5800.00"),
                    f"2110 {dn_debit}, 2210 {vat_debit}, 2100 {ap_credit}")
        self.expect("and does not charge the stock a second time",
                    inv_debit == Z and self.account("1400").current_balance == stock_before,
                    f"1400 debit {inv_debit}, balance {self.account('1400').current_balance} "
                    f"against {stock_before}")
        self.expect("the accrual fell by exactly what has now been billed",
                    self.account("2110").current_balance == accrual_before - D("5000.00"),
                    f"{self.account('2110').current_balance} against {accrual_before}")

        self.post(client, "receive the rest",
                  reverse("procurement:grn_record", args=[po.pk]),
                  {f"receive_qty_{po.lines.order_by('id')[1].pk}": "5",
                   "warehouse": self.warehouse.pk})
        po.refresh_from_db()
        self.expect("the last line in makes the order fully received",
                    po.status == "FULLY_RECEIVED", po.status)
        self.post(client, "bill the rest", reverse("procurement:po_to_bill", args=[po.pk]), {})
        po.refresh_from_db()
        b2 = po.bills.order_by("created_at").last()
        self.expect("the second bill covers the second line",
                    b2.total_amount == D("1000.00") and b2.tax_amount == Z,
                    f"{b2.total_amount} / {b2.tax_amount}")
        self.expect("a fully received and fully billed order is billed",
                    po.status == "BILLED", po.status)
        self.post(client, "post the second bill",
                  reverse("procurement:bill_post", args=[b2.pk]), {})
        b2.refresh_from_db()

        # ── E. reverting a bill, and the guard around it ──
        ap_before = self.account("2100").current_balance
        resp = self.post(client, "cancel one of two bills",
                         reverse("procurement:bill_cancel", args=[b2.pk]), {})
        b2.refresh_from_db()
        po.refresh_from_db()
        l1.refresh_from_db()
        l2 = po.lines.order_by("id")[1]
        self.expect("a cancelled bill's entry is offset, not erased",
                    b2.status == "CANCELLED" and b2.journal_entry.reversed_by_id is not None
                    and b2.journal_entry.status == "POSTED", b2.status)
        self.expect("the payable came back down by what that bill owed",
                    self.account("2100").current_balance == ap_before - D("1000.00"),
                    str(self.account("2100").current_balance))
        self.expect("a second bill still live on the order holds the quantities",
                    l2.billed_quantity == D("5.0000"), str(l2.billed_quantity))
        self.expect_flash(client, resp, "another bill against it",
                          "the order explains why nothing was released")
        bank_before = self.account("1200").current_balance
        resp = client.post(reverse("procurement:bill_pay", args=[b2.pk]), {"amount": "100"})
        b2.refresh_from_db()
        self.expect("a cancelled bill takes no payment",
                    b2.status == "CANCELLED" and b2.paid_amount == Z
                    and self.account("1200").current_balance == bank_before,
                    f"{b2.status}, paid {b2.paid_amount}")
        self.expect_flash(client, resp, "cancelled", "the refusal says why")
        for junk in ("NaN", "Infinity", "abc", "1e999999"):
            resp = client.post(reverse("procurement:bill_pay", args=[b1.pk]), {"amount": junk})
            b1.refresh_from_db()
            self.expect(f"a payment of '{junk}' is refused, not a server error",
                        resp.status_code == 302 and b1.paid_amount == Z,
                        f"HTTP {resp.status_code}, paid {b1.paid_amount}")

        resp = self.post(client, "cancel the last live bill",
                         reverse("procurement:bill_cancel", args=[b1.pk]), {})
        b1.refresh_from_db()
        po.refresh_from_db()
        l1.refresh_from_db()
        l2 = po.lines.order_by("id")[1]
        self.expect("with the other bill cancelled, the order opens again",
                    l1.billed_quantity == Z and l2.billed_quantity == Z
                    and po.status == "FULLY_RECEIVED",
                    f"{l1.billed_quantity} / {l2.billed_quantity} / {po.status}")
        self.expect("the cancelled bill's own entry stays posted and offset",
                    b1.status == "CANCELLED" and b1.journal_entry.reversed_by_id is not None,
                    b1.status)

        self.post(client, "re-bill the order", reverse("procurement:po_to_bill", args=[po.pk]), {})
        po.refresh_from_db()
        b3 = po.bills.order_by("created_at").last()
        self.expect("the whole receipt is billable again after the revert",
                    b3.total_amount == D("6800.00") and b3.subtotal == D("6000.00")
                    and b3.tax_amount == D("800.00"),
                    f"{b3.subtotal} / {b3.tax_amount} / {b3.total_amount}")
        self.post(client, "post the re-raised bill",
                  reverse("procurement:bill_post", args=[b3.pk]), {})
        b3.refresh_from_db()

        # ── F. a supplier debit note brings the VAT back at the rate it went in ──
        resp = self.post(client, "debit note",
                         reverse("procurement:bill_debit_note", args=[b3.pk]),
                         {"amount": "580", "reason": "Five drums went back"})
        dn = b3.debit_notes.first()
        if not self.expect("the debit note is its own document", dn is not None):
            return
        self.expect("the debit note splits tax the way the bill did",
                    dn.tax_amount == D("68.24") and dn.subtotal == D("511.76")
                    and dn.total_amount == D("580.00"),
                    f"{dn.subtotal} / {dn.tax_amount} / {dn.total_amount}")
        self.expect("it is dated, named and linked",
                    dn.status == "DEBIT_NOTE" and dn.original_bill_id == b3.pk
                    and dn.currency == b3.currency, dn.status)
        dje = dn.journal_entry
        self.expect("a debit note clears payables",
                    self.line_total(dje, "2100", "debit") == D("580.00"),
                    str(self.line_total(dje, "2100", "debit")))
        self.expect("and credits back the accounts the bill charged, at its share of each",
                    self.line_total(dje, "2110", "credit") == D("511.76")
                    and self.line_total(dje, "2210", "credit") == D("68.24"),
                    f"2110 {self.line_total(dje, '2110', 'credit')}, "
                    f"2210 {self.line_total(dje, '2210', 'credit')}")
        dr, cr, ok = self.balanced(dje)
        self.expect("the debit note entry balances", ok, f"Dr {dr} / Cr {cr}")
        b3.refresh_from_db()
        self.expect("the bill remembers what has been debited back",
                    b3.credited_amount == D("580.00")
                    and b3.outstanding_amount == D("6220.00"),
                    f"{b3.credited_amount} / {b3.outstanding_amount}")
        self.expect("a debit note owes nobody anything by itself",
                    dn.outstanding_amount == Z, str(dn.outstanding_amount))
        self.expect("the bill that was debited keeps its own entry",
                    b3.journal_entry.reversed_by_id is None
                    and self.line_total(b3.journal_entry, "2100", "credit") == D("6800.00"),
                    str(b3.journal_entry.reversed_by_id))

        count_before = Bill.objects.filter(company=company).count()
        resp = self.post(client, "over-large debit note",
                         reverse("procurement:bill_debit_note", args=[b3.pk]),
                         {"amount": "99999"})
        self.expect("a debit note cannot exceed what is owed",
                    Bill.objects.filter(company=company).count() == count_before,
                    f"{count_before} bills before and after")
        self.expect_flash(client, resp, "more than", "the refusal names the limit")
        resp = self.post(client, "cancel a debited bill",
                         reverse("procurement:bill_cancel", args=[b3.pk]), {})
        b3.refresh_from_db()
        self.expect("a bill with a debit note on it is not simply cancelled",
                    b3.status == "RECEIVED", b3.status)
        self.expect_flash(client, resp, "cannot cancel", "the refusal explains why")

        paid = bill_of("BILL-SMOKE-1")
        resp = self.post(client, "debit note on a paid bill",
                         reverse("procurement:bill_debit_note", args=[paid.pk]),
                         {"amount": "10"})
        self.expect("a settled bill has nothing to debit",
                    not Bill.objects.filter(company=company, original_bill=paid).exists(),
                    f"outstanding {paid.outstanding_amount}")
        self.expect_flash(client, resp, "refund", "the refusal points at the right document")

        ap_ledger, ap_book = self.ap_position()
        self.expect("payables and the bill book moved together through notes and cancellations",
                    ap_ledger - ap_book == ap_gap,
                    f"gap {ap_ledger - ap_book} against {ap_gap} before "
                    f"(ledger {ap_ledger}, book {ap_book})")

        # ── G. the paper and the screens ──
        for label, url in [
            ("order detail with its conversion", reverse("sales:order_detail", args=[order2.pk])),
            ("invoice detail with a credit note", reverse("sales:invoice_detail", args=[made.pk])),
            ("credit note as a document", reverse("sales:invoice_detail", args=[note.pk])),
            ("cancelled invoice detail", reverse("sales:invoice_detail", args=[inv_b.pk])),
            ("purchase order with lines", reverse("procurement:po_detail", args=[po.pk])),
            ("goods receipt sheet", reverse("procurement:grn", args=[po.pk])),
            ("bill detail with a debit note", reverse("procurement:bill_detail", args=[b3.pk])),
            ("debit note as a document", reverse("procurement:bill_detail", args=[dn.pk])),
            ("invoice list", reverse("sales:invoice_list")),
            ("bill list", reverse("procurement:bill_list")),
            ("purchase order list", reverse("procurement:po_list")),
        ]:
            self.get_ok(client, f"the {label}", url)

        # Another company's paper is not this book's to convert or cancel.
        from apps.core.services import provision_company
        from apps.crm.models import Customer as CrmCustomer
        from apps.organizations.models import Company

        elsewhere = Company.objects.create(
            name=f"Rival Traders {self.stamp}", currency="KES")
        provision_company(elsewhere, self.user)
        foreign = Invoice.objects.create(
            company=elsewhere,
            customer=CrmCustomer.objects.create(company=elsewhere, name="Rival Buyer"),
            reference="INV-FOREIGN-16", invoice_date=self.today, due_date=self.today,
            currency=elsewhere.currency,
            subtotal=D("10.00"), tax_amount=Z, total_amount=D("10.00"))
        resp = client.post(reverse("sales:invoice_credit_note", args=[foreign.pk]),
                           {"credit_qty_0": "1"})
        self.expect("another company's invoice cannot be credited from here",
                    resp.status_code == 404, f"HTTP {resp.status_code}")
        foreign.delete()

        lines = self.posted_lines()
        dr = sum((line.debit_amount for line in lines), Z)
        cr = sum((line.credit_amount for line in lines), Z)
        self.expect("the whole book still balances after all of it", dr == cr,
                    f"Dr {dr} / Cr {cr}")

        self.note("Orders are converted rather than retyped, and each document keeps the "
                  "quantities it has already claimed, so an order cannot be billed twice "
                  "and a bill is raised against goods that actually arrived. Corrections "
                  "never rewrite the book: a credit or debit note answers the original with "
                  "its own entry, in the accounts the original used, and a cancellation is "
                  "offset in the original's period. A goods receipt values the shelf and "
                  "accrues on 2110; the bill that follows releases that accrual rather than "
                  "charging the same goods to stock twice.")

    # ────────────────────────── exchange revaluation ─────────────────────────

    def verify_revaluation(self, client):
        """Open foreign balances are read on the day they are reported.

        Section 15 proved a document keeps the rate of the day it was raised.
        That is right for the document and wrong for a balance sheet drawn
        afterwards: a dollar receivable still open at a period end is worth the
        period end's dollars, and the gap has not been paid to anybody yet, so
        it is unrealised. It therefore goes on its own pair of accounts, the
        receivable and payable control accounts stay exactly where the
        subledgers have them, and the whole figure comes back off the book when
        the next revaluation is drawn.
        """
        from apps.accounting import gl_service, revaluation
        from apps.accounting.models import Currency, ExchangeRate, JournalEntry, period_for
        from apps.procurement.models import Bill
        from apps.sales.models import Invoice, InvoiceLine
        from apps.sales.views import CustomerStatementView

        company = self.company
        book = (company.currency or "KES").upper()
        usd = Currency.objects.filter(code="USD").first()
        eur = Currency.objects.filter(code="EUR").first()
        if not self.expect("both foreign currencies are on the register", usd and eur,
                           f"USD {bool(usd)}, EUR {bool(eur)}"):
            return
        rate_url = reverse("accounting:exchange_rate_create")
        filing = lambda currency, on_date, value: {
            "currency": currency.pk, "effective_date": str(on_date), "rate": value,
            "source": "MANUAL", "note": "Filed for the period end",
        }

        far = self.today + datetime.timedelta(days=30)
        later = self.today + datetime.timedelta(days=50)
        if not self.expect("the calendar covers both revaluation days",
                           period_for(company, far) and period_for(company, later),
                           f"{far}, {later}"):
            return

        reval = ["1350", "2150", "4320", "7310"]
        for code in reval:
            self.expect(f"{code} starts the run empty",
                        self.account(code).current_balance == Z,
                        str(self.account(code).current_balance))

        # ── three open foreign documents: a dollar owed, a dollar owed out, and
        #    a euro the book has never had a rate for ──
        self.post(client, "dollar invoice to revalue", reverse("sales:invoice_create"), merge({
            "reference": "INV-FX-17", "customer": self.customer.pk,
            "invoice_date": str(self.today), "due_date": str(far),
            "currency": "USD", "notes": "Deliberately left open",
        }, formset("lines", [
            {"product": self.product.pk, "description": "Widgets", "quantity": "1",
             "unit_price": "100", "discount_percent": "0", "tax_rate": ""},
        ], blank=BLANK_INVOICE_LINE)))
        dollar = Invoice.objects.filter(company=company, reference="INV-FX-17").first()
        if not self.expect("the dollar receivable is open",
                           dollar is not None and dollar.outstanding_amount == D("100.00")
                           and dollar.total_amount_base == D("13500.00"),
                           f"{dollar and dollar.outstanding_amount} USD / "
                           f"{dollar and dollar.total_amount_base} {book}"):
            return
        self.post(client, "dollar bill to revalue", reverse("procurement:bill_create"), {
            "reference": "BILL-FX-17", "supplier": self.supplier.pk, "purchase_order": "",
            "supplier_reference": "FX-17", "bill_date": str(self.today),
            "due_date": str(far), "currency": "USD",
            "subtotal": "50", "tax_amount": "0", "total_amount": "50",
            "notes": "Bought in dollars and still owed",
        })
        owed_bill = Bill.objects.filter(company=company, reference="BILL-FX-17").first()
        self.expect("the dollar payable is open",
                    owed_bill is not None
                    and owed_bill.outstanding_base == D("6750.00"),
                    f"{owed_bill and owed_bill.outstanding_base} {book}")

        euro = Invoice.objects.create(
            company=company, customer=self.customer, reference="INV-FX-EUR17",
            invoice_date=self.today, due_date=far, currency="EUR",
            exchange_rate=D("150"), subtotal=D("100.00"), tax_amount=Z,
            total_amount=D("100.00"))
        InvoiceLine.objects.create(
            invoice=euro, description="Euro paper", quantity=D("1"),
            unit_price=D("100.00"))
        gl_service.post_invoice(euro, self.user)
        euro.refresh_from_db()
        self.expect("a euro document booked at a rate written on the paper holds",
                    euro.journal_entry_id is not None
                    and euro.total_amount_base == D("15000.00"),
                    f"{euro.total_amount_base} {book}")

        # Measured once the paperwork is in: a revaluation must move the
        # valuation accounts and nothing else.
        lines_before = self.posted_lines().count()
        ar_control = self.account("1300").current_balance
        ap_control = self.account("2100").current_balance
        ap_ledger0, ap_book0 = self.ap_position()
        ap_gap = ap_ledger0 - ap_book0

        # ── a money with no rate cannot be valued, and saying so beats guessing ──
        position = revaluation.positions(company, self.today)
        self.expect("an unpriced money is reported, not quietly dropped",
                    position["missing"] == ["EUR"], str(position["missing"]))
        rows = {row["code"]: row for row in position["rows"]}
        # What was owed is read as at the day: the receipt and the supplier
        # payment section 15 dated ahead have not happened yet, so INV-USD-2
        # and BILL-USD-1 still count as owed today.
        self.expect("a balance raised at the reported day's rate needs no move",
                    rows["USD"]["ar_foreign"] == D("300.00")
                    and rows["USD"]["ar_carried"] == D("40500.00")
                    and rows["USD"]["ar_revalued"] == D("40500.00")
                    and rows["USD"]["ap_foreign"] == D("150.00")
                    and rows["USD"]["ar_difference"] == Z
                    and rows["USD"]["ap_difference"] == Z,
                    f"USD row {rows['USD']}")
        self.get_ok(client, "revaluation screen", reverse("accounting:revaluation"))
        self.expect("a preview touches nothing in the ledger",
                    self.posted_lines().count() == lines_before
                    and revaluation.live_revaluation(company) is None,
                    f"{self.posted_lines().count()} posted lines, was {lines_before}")
        resp = client.post(reverse("accounting:revaluation_post"), {"date": str(self.today)})
        self.expect_flash(client, resp, "no exchange rate has been filed",
                          "revaluing an unpriced money is refused")
        self.expect("the refusal posted no revaluation",
                    not JournalEntry.objects.filter(
                        company=company, entry_type="REVALUATION").exists(),
                    f"{JournalEntry.objects.filter(company=company, entry_type='REVALUATION').count()} entries")

        # ── the period end: file the day's rates and read the paper at them ──
        self.post(client, "euro rate for the period end", rate_url, filing(eur, far, "155"))
        self.post(client, "dollar rate for the period end", rate_url, filing(usd, far, "128"))
        self.expect("the period end's own rate is the one used",
                    ExchangeRate.rate_for(company, "USD", far) == D("128")
                    and ExchangeRate.rate_for(company, "EUR", far) == D("155"),
                    f"USD {ExchangeRate.rate_for(company, 'USD', far)}, "
                    f"EUR {ExchangeRate.rate_for(company, 'EUR', far)}")
        position = revaluation.positions(company, far)
        rows = {row["code"]: row for row in position["rows"]}
        self.expect("every money is priced now", not position["missing"],
                    str(position["missing"]))
        self.expect("a weaker dollar cuts both the receivable and the payable",
                    rows["USD"]["ar_difference"] == D("-700.00")
                    and rows["USD"]["ap_difference"] == D("-350.00"),
                    f"AR {rows['USD']['ar_difference']}, AP {rows['USD']['ap_difference']}")
        self.expect("a stronger euro lifts only the receivable",
                    rows["EUR"]["ar_difference"] == D("500.00")
                    and rows["EUR"]["ap_difference"] == Z,
                    f"AR {rows['EUR']['ar_difference']}")
        receivable, payable = revaluation.summary(position)
        self.expect("the differences are totalled in the book's money, never on the paper",
                    receivable == D("-200.00") and payable == D("-350.00"),
                    f"receivables {receivable}, payables {payable}")
        preview = client.get(reverse("accounting:revaluation")
                             + f"?date={far:%Y-%m-%d}").context
        self.expect("the screen reports the same figures the service does",
                    preview["total_receivable"] == D("-200.00")
                    and preview["total_payable"] == D("-350.00")
                    and preview["period"] == period_for(company, far)
                    and not preview["period_warn"] and preview["book"] == book,
                    f"{preview['total_receivable']} / {preview['total_payable']}")
        self.expect("reading the screen still posts nothing",
                    self.posted_lines().count() == lines_before,
                    f"{self.posted_lines().count()} posted lines")

        resp = self.post(client, "period end revaluation",
                         reverse("accounting:revaluation_post"), {"date": str(far)})
        first = revaluation.live_revaluation(company)
        if not self.expect("the revaluation is on the book", first is not None):
            return
        self.expect_flash(client, resp, "difference booked", "the posting was announced")
        self.expect("the entry is exchange machinery of its own kind",
                    first.entry_type == "REVALUATION"
                    and first.source_document_type == revaluation.REVALUATION
                    and first.reference.startswith("JE-FX-")
                    and first.posting_date == far
                    and first.period_id == period_for(company, far).pk,
                    f"{first.reference} {first.entry_type} on {first.posting_date}")
        dr, cr, ok = self.balanced(first)
        self.expect("the revaluation balances and carries only the difference",
                    ok and dr == D("550.00"), f"Dr {dr} / Cr {cr}")
        self.expect("a receivable worth less is credited off the valuation account, "
                    "and the loss is unrealised",
                    self.line_total(first, "1350", "credit") == D("200.00")
                    and self.line_total(first, "1350", "debit") == Z
                    and self.line_total(first, "7310", "debit") == D("200.00"),
                    f"1350 {self.line_total(first, '1350', 'credit')}, "
                    f"7310 {self.line_total(first, '7310', 'debit')}")
        self.expect("a payable worth less is taken off the valuation account "
                    "against an unrealised gain",
                    self.line_total(first, "2150", "debit") == D("350.00")
                    and self.line_total(first, "4320", "credit") == D("350.00"),
                    f"2150 {self.line_total(first, '2150', 'debit')}, "
                    f"4320 {self.line_total(first, '4320', 'credit')}")
        self.expect("the difference sits on the valuation accounts, in the book",
                    self.account("1350").current_balance == D("-200.00")
                    and self.account("2150").current_balance == D("-350.00")
                    and self.account("4320").current_balance == D("350.00")
                    and self.account("7310").current_balance == D("200.00"),
                    " ".join(f"{code} {self.account(code).current_balance}" for code in reval))
        self.expect("revaluation accounts are on the balance sheet, not in trading",
                    self.account("1350").account_type == "ASSET"
                    and self.account("2150").account_type == "LIABILITY"
                    and self.account("4320").account_type == "REVENUE"
                    and self.account("7310").account_type == "EXPENSE")
        self.expect("the control accounts were left alone",
                    self.account("1300").current_balance == ar_control
                    and self.account("2100").current_balance == ap_control,
                    f"1300 {self.account('1300').current_balance}, "
                    f"2100 {self.account('2100').current_balance}")
        ar_ledger, ar_book = self.ar_position()
        ap_ledger, ap_book = self.ap_position()
        self.expect("receivables still equal the invoice book through a revaluation",
                    ar_ledger == ar_book, f"{ar_ledger} against {ar_book}")
        self.expect("payables and the bill book kept their distance unchanged",
                    ap_ledger - ap_book == ap_gap,
                    f"gap {ap_ledger - ap_book} against {ap_gap}")
        book_dr, book_cr = self.book_totals()
        self.expect("the whole book still balances with unrealised exchange on it",
                    book_dr == book_cr, f"Dr {book_dr} / Cr {book_cr}")
        sheet = client.get(reverse("reporting:balance_sheet")).context or {}
        self.expect("the balance sheet agrees with the revaluation on it",
                    sheet.get("is_balanced"), f"error {sheet.get('balancing_error')}")

        # ── the next period end redraws the picture: the old one must come off ──
        self.post(client, "dollar rate at the next period end", rate_url,
                  filing(usd, later, "139"))
        self.post(client, "euro rate at the next period end", rate_url,
                  filing(eur, later, "158"))
        self.post(client, "next period end revaluation",
                  reverse("accounting:revaluation_post"), {"date": str(later)})
        second = revaluation.live_revaluation(company)
        if not self.expect("the new revaluation replaced the old",
                           second is not None and second.pk != first.pk):
            return
        first.refresh_from_db()
        self.expect("the superseded revaluation was reversed, never edited",
                    first.reversed_by_id is not None and first.status == "POSTED",
                    f"{first.reference} still {first.status}")
        self.expect("a redraw dated later takes the old figure out on that later day",
                    first.reversed_by.posting_date == later
                    and first.reversed_by.period_id == period_for(company, later).pk,
                    f"reversal {first.reversed_by.posting_date} in "
                    f"{first.reversed_by.period}")
        self.expect("the reversal is still revaluation machinery, not fresh trading",
                    first.reversed_by.entry_type == "REVALUATION")
        dr, cr, ok = self.balanced(second)
        self.expect("the redraw balances", ok and dr == D("1400.00"), f"Dr {dr} / Cr {cr}")
        self.expect("at the newer rates the receivable is worth more and so is the payable",
                    self.line_total(second, "1350", "debit") == D("1200.00")
                    and self.line_total(second, "4320", "credit") == D("1200.00")
                    and self.line_total(second, "7310", "debit") == D("200.00")
                    and self.line_total(second, "2150", "credit") == D("200.00"),
                    f"1350 {self.line_total(second, '1350', 'debit')}")
        self.expect("the valuation accounts show only the newest reading",
                    self.account("1350").current_balance == D("1200.00")
                    and self.account("2150").current_balance == D("200.00")
                    and self.account("4320").current_balance == D("1200.00")
                    and self.account("7310").current_balance == D("200.00"),
                    " ".join(f"{code} {self.account(code).current_balance}" for code in reval))
        self.expect("the book carries one revaluation at a time",
                    JournalEntry.objects.filter(
                        company=company, entry_type="REVALUATION", status="POSTED",
                        reversed_by__isnull=True, reversal_of__isnull=True).count() == 1,
                    f"{JournalEntry.objects.filter(company=company, entry_type='REVALUATION').count()} revaluation entries in all")

        # ── drawing the same period end twice changes nothing ──
        balances = {code: self.account(code).current_balance for code in reval}
        self.post(client, "redraw the same period end",
                  reverse("accounting:revaluation_post"), {"date": str(later)})
        self.expect("a redraw of the same day nets back to the same figures",
                    all(self.account(code).current_balance == balances[code]
                        for code in reval),
                    " ".join(f"{code} {self.account(code).current_balance}" for code in reval))
        self.expect("and still leaves exactly one live revaluation",
                    revaluation.live_revaluation(company).posting_date == later
                    and JournalEntry.objects.filter(
                        company=company, entry_type="REVALUATION", status="POSTED",
                        reversed_by__isnull=True, reversal_of__isnull=True).count() == 1)

        resp = client.post(reverse("accounting:revaluation_post"), {"date": str(far)})
        self.expect_flash(client, resp, "already revalued to",
                          "revaluing backwards over a later figure is refused")
        self.expect("the refused backdating left the newest reading in place",
                    revaluation.live_revaluation(company).posting_date == later)

        # ── take it off the book ──
        resp = self.post(client, "reverse the revaluation",
                         reverse("accounting:revaluation_reverse"), {"date": str(later)})
        self.expect_flash(client, resp, "reversed by", "the reversal was announced")
        self.expect("with no revaluation on the book the difference is gone",
                    all(self.account(code).current_balance == Z for code in reval),
                    " ".join(f"{code} {self.account(code).current_balance}" for code in reval))
        self.expect("nothing is left live",
                    revaluation.live_revaluation(company) is None)
        self.expect("the foreign documents are carried at their booked rates again",
                    self.account("1300").current_balance == ar_control
                    and self.account("2100").current_balance == ap_control,
                    f"1300 {self.account('1300').current_balance}")
        resp = client.post(reverse("accounting:revaluation_reverse"), {"date": str(later)})
        self.expect_flash(client, resp, "no revaluation to reverse",
                          "reversing what is already gone says so")
        for label, url in [
            ("revaluation screen", reverse("accounting:revaluation")),
            ("valuation account ledger",
             reverse("accounting:account_ledger", args=[self.account("1350").pk])),
            ("currency register", reverse("accounting:currency_list")),
            ("accounting home", reverse("accounting:index")),
            ("balance sheet", reverse("reporting:balance_sheet")),
        ]:
            self.get_ok(client, f"the {label}", url)

        # ── a statement and a report must still be one money ──
        statement = client.get(
            reverse("sales:customer_statement", args=[self.customer.pk])).context
        self.expect("the customer statement runs to its closing balance",
                    statement["closing_balance"]
                    == statement["opening_balance"] + statement["total_debit"]
                    - statement["total_credit"],
                    f"{statement['opening_balance']} + {statement['total_debit']} "
                    f"- {statement['total_credit']} against {statement['closing_balance']}")
        by_ref = {row["reference"]: row for row in statement["rows"]}
        self.expect("a dollar row reads in dollars and counts in the book",
                    by_ref["INV-FX-17"]["paper"] == D("100.00")
                    and by_ref["INV-FX-17"]["currency"] == "USD"
                    and by_ref["INV-FX-17"]["debit"] == D("13500.00"),
                    f"{by_ref['INV-FX-17']['paper']} {by_ref['INV-FX-17']['currency']} "
                    f"= {by_ref['INV-FX-17']['debit']} {book}")
        self.expect("the statement knows it is mixing papers", statement["mixed"],
                    str(statement["currencies"]))
        not_on_ledger = set(
            Invoice.objects.filter(customer=self.customer)
            .exclude(status__in=CustomerStatementView.ON_ACCOUNT)
            .values_list("reference", flat=True))
        self.expect("paper the ledger has not seen is not on the statement",
                    not (not_on_ledger & set(by_ref)), str(sorted(not_on_ledger)))
        self.expect("a credit note answers on its own line",
                    any(row["type"] == "Credit note" for row in statement["rows"]),
                    str({row["type"] for row in statement["rows"]}))
        dollars = client.get(reverse("sales:customer_statement", args=[self.customer.pk])
                             + "?currency=USD").context
        self.expect("asked for one money, the statement holds to it",
                    dollars["rows"] and all(row["currency"] == "USD"
                                            for row in dollars["rows"])
                    and not dollars["mixed"]
                    and dollars["closing_balance"] == dollars["opening_balance"]
                    + dollars["total_debit"] - dollars["total_credit"],
                    f"{ {row['currency'] for row in dollars['rows']} }")
        open_invoices = Invoice.objects.filter(
            company=company, status__in=["SENT", "PARTIAL", "OVERDUE"])
        owed = sum((i.outstanding_base for i in open_invoices
                    if i.outstanding_amount > Z), Z)
        paper = sum((i.outstanding_amount for i in open_invoices
                     if i.outstanding_amount > Z), Z)
        debtors = client.get(reverse("reporting:aged_debtors")).context
        self.expect("aged debtors total the book value, never a heap of papers",
                    debtors["grand_total"] == owed and owed != paper,
                    f"report {debtors['grand_total']}, book {owed}, paper {paper}")

        supplier = client.get(
            reverse("procurement:supplier_statement", args=[self.supplier.pk])).context
        self.expect("the supplier statement runs to its closing balance",
                    supplier["closing_balance"]
                    == supplier["opening_balance"] + supplier["total_billed"]
                    - supplier["total_credited"] - supplier["total_paid"],
                    f"{supplier['opening_balance']} + {supplier['total_billed']} "
                    f"- {supplier['total_credited']} - {supplier['total_paid']} "
                    f"against {supplier['closing_balance']}")
        bills_by_ref = {row["reference"]: row for row in supplier["rows"]}
        self.expect("a dollar bill shows its paper figure and its book value",
                    bills_by_ref["BILL-FX-17"]["paper"] == D("50.00")
                    and bills_by_ref["BILL-FX-17"]["owed"] == D("6750.00"),
                    f"{bills_by_ref['BILL-FX-17']['owed']} {book}")
        self.expect("a debit note is its own line and the bill it answers "
                    "stays at its own full value",
                    any(row["type"] == "Debit note" for row in supplier["rows"]),
                    str({row["type"] for row in supplier["rows"]}))
        open_bills = Bill.objects.filter(
            company=company, status__in=["RECEIVED", "PARTIAL", "OVERDUE"])
        payable = sum((b.outstanding_base for b in open_bills
                       if b.outstanding_amount > Z), Z)
        creditors = client.get(reverse("reporting:aged_creditors")).context
        self.expect("aged creditors are aged in the book's money too",
                    creditors["grand_total"] == payable,
                    f"report {creditors['grand_total']}, book {payable}")
        for label, url, figure in [
            ("the dashboard", reverse("dashboard:index"), "accounts_receivable"),
            ("the receivables home", reverse("sales:index"), "total_outstanding"),
        ]:
            page = client.get(url).context
            self.expect(f"{label} counts receivables in the book's money",
                        page["book"] == book and page[figure] == owed,
                        f"{page[figure]} {book}")
        home = client.get(reverse("procurement:index")).context
        self.expect("the payables home counts payables in the book's money",
                    home["book"] == book and home["total_outstanding"] == payable,
                    f"{home['total_outstanding']} {book}")

        book_dr, book_cr = self.book_totals()
        self.expect("the whole book balances after revaluation and reversal alike",
                    book_dr == book_cr, f"Dr {book_dr} / Cr {book_cr}")

        self.note("Period-end revaluation is here: open foreign receivables and payables "
                  "are read at the rate of the day reported on, the difference goes to "
                  "1350/2150 against the unrealised gain and loss accounts, one reading is "
                  "live at a time, and the control accounts the subledgers are proved "
                  "against are never touched. What is NOT here: a bank balance held in a "
                  "foreign currency cannot be revalued, because a payment records its "
                  "value in the book but not the foreign amount still sitting in the "
                  "account, and the realised gain is only recognised when money actually "
                  "moves.")

    # ──────────────────────────── stock costing ──────────────────────────────

    def verify_stock_costing(self, client):
        """What the shelf is worth, what a sale cost, and what a refusal leaves behind.

        The earlier sections put goods on a shelf and billed for them; this one
        follows a second product from the purchase order to the customer's
        invoice and asks what the book says at every step. Goods arrive at the
        price the order agreed — not the price typed on the product card — and
        stand against the received-not-invoiced accrual until the supplier's bill
        releases it. Goods leave at the average the shelf actually holds, so cost
        of sales is the same money the stock is credited with, and the distance
        between that and the invoice is the margin the sale made. A shortage is
        written off through the adjustment account rather than sold, and an issue
        the shelf cannot cover must leave neither a movement row nor a journal
        entry — a shelf that moved while its entry failed is a book nobody trusts.
        """
        from apps.inventory.models import Product, StockLevel, StockMovement
        from apps.organizations.models import Warehouse
        from apps.procurement.models import PurchaseOrder
        from apps.sales.models import SalesOrder

        company = self.company

        def shelf(warehouse=None):
            return StockLevel.objects.filter(
                product=second, warehouse=warehouse or self.warehouse).first()

        # ── a second product, whose order costs more than its card says ──
        self.post(client, "second product", reverse("inventory:product_create"), {
            "sku": "SMK-002", "name": "Smoke Test Drum", "product_type": "GOODS",
            "category": self.category.pk, "unit_of_measure": self.uom.pk,
            "cost_price": "140.00", "selling_price": "200.00", "tax_rate": "",
            "reorder_level": "0", "reorder_quantity": "0",
            "track_inventory": "on", "valuation_method": "WAVG",
            "income_account": self.account("4200").pk,
            "cogs_account": self.account("5000").pk,
            "inventory_account": self.account("1400").pk,
            "barcode": "", "description": "Costing run item",
        })
        second = Product.objects.filter(company=company, sku="SMK-002").first()
        if not self.expect("a second product on file to cost", second is not None):
            return
        self.expect("nothing is on the shelf for it yet", shelf() is None,
                    str(shelf().quantity) if shelf() else "empty")

        posted0 = self.posted_lines().count()
        stock0 = self.account("1400").current_balance
        accrual0 = self.account("2110").current_balance
        ap0 = self.account("2100").current_balance

        # ── A. goods in: the order's price, against the accrual ──
        self.post(client, "drum purchase order", reverse("procurement:po_create"), {
            "reference": "PO-SMOKE-18", "supplier": self.supplier.pk,
            "order_date": str(self.today),
            "expected_delivery_date": str(self.today + datetime.timedelta(days=7)),
            "warehouse": self.warehouse.pk, "notes": "Twenty drums at the quoted price",
        })
        po = PurchaseOrder.objects.filter(company=company, reference="PO-SMOKE-18").first()
        if not self.expect("the drum order is on file", po is not None):
            return
        self.post(client, "drum order line", reverse("procurement:po_line_add", args=[po.pk]), {
            "product": second.pk, "quantity": "20", "unit_cost": "150",
            "tax_rate": "", "description": "Drums"})
        po.refresh_from_db()
        self.expect("the order is the quoted price times the quantity",
                    po.subtotal == D("3000.00") and po.tax_amount == Z
                    and po.total_amount == D("3000.00"),
                    f"{po.subtotal} / {po.tax_amount} / {po.total_amount}")
        self.post(client, "send the drum order",
                  reverse("procurement:po_send", args=[po.pk]), {})

        po_line = po.lines.order_by("id").first()
        self.post(client, "receive the drums",
                  reverse("procurement:grn_record", args=[po.pk]),
                  {f"receive_qty_{po_line.pk}": "20", "warehouse": self.warehouse.pk})
        receipt = StockMovement.objects.filter(purchase_order=po).first()
        if not self.expect("the receipt wrote a movement of its own", receipt is not None):
            return
        self.expect("goods arrive at the price the order agreed, not the card's guess",
                    receipt.unit_cost == D("150.0000")
                    and receipt.total_cost == D("3000.00"),
                    f"{receipt.unit_cost} × {receipt.quantity} = {receipt.total_cost} "
                    f"against a card cost of {second.cost_price}")
        level = shelf()
        self.expect("the shelf holds all twenty at what they were paid",
                    level is not None and level.quantity == D("20.0000")
                    and level.avg_cost == D("150.0000"),
                    f"{level.quantity if level else 'none'} @ "
                    f"{level.avg_cost if level else 'none'}")
        self.expect("stock went up by the delivery's own value",
                    self.account("1400").current_balance == stock0 + D("3000.00"),
                    f"1400 {self.account('1400').current_balance} against {stock0}")
        self.expect("and the credit is the accrual, not a payable",
                    self.account("2110").current_balance == accrual0 + D("3000.00")
                    and self.account("2100").current_balance == ap0
                    and self.posted_lines().count() == posted0 + 2,
                    f"2110 +{self.account('2110').current_balance - accrual0}, "
                    f"2100 +{self.account('2100').current_balance - ap0}")

        self.post(client, "bill the drums",
                  reverse("procurement:po_to_bill", args=[po.pk]), {})
        po.refresh_from_db()
        bill = po.bills.filter(status="DRAFT").first()
        if not self.expect("the supplier's bill was raised from the order", bill is not None):
            return
        self.expect("the bill asks for exactly what arrived",
                    bill.subtotal == D("3000.00") and bill.tax_amount == Z
                    and bill.total_amount == D("3000.00"),
                    f"{bill.subtotal} / {bill.tax_amount} / {bill.total_amount}")
        self.post(client, "post the drum bill",
                  reverse("procurement:bill_post", args=[bill.pk]), {})
        bill.refresh_from_db()
        entry = bill.journal_entry
        self.expect("the bill releases the accrual instead of costing the stock again",
                    self.line_total(entry, "2110", "debit") == D("3000.00")
                    and self.line_total(entry, "1400", "debit") == Z
                    and self.line_total(entry, "2100", "credit") == D("3000.00"),
                    f"2110 {self.line_total(entry, '2110', 'debit')}, "
                    f"1400 {self.line_total(entry, '1400', 'debit')}, "
                    f"2100 {self.line_total(entry, '2100', 'credit')}")
        self.expect("the bill remembers the accrual it cleared",
                    bill.accrued_cleared == D("3000.00"), str(bill.accrued_cleared))
        self.expect("the accrual is closed and payables carry the bill",
                    self.account("2110").current_balance == accrual0
                    and self.account("2100").current_balance == ap0 + D("3000.00"),
                    f"2110 {self.account('2110').current_balance} against {accrual0}")

        # ── B. goods out: cost of sales at the average the shelf holds ──
        cogs0 = self.account("5000").current_balance
        stock1 = self.account("1400").current_balance
        revenue0 = self.account("4200").current_balance
        self.post(client, "drum sales order", reverse("sales:order_create"), merge(
            {"reference": "SO-SMOKE-18", "customer": self.customer.pk,
             "order_date": str(self.today),
             "delivery_date": str(self.today + datetime.timedelta(days=3)),
             "warehouse": self.warehouse.pk, "sales_rep": self.user.pk,
             "currency": "KES", "discount_percent": "0",
             "branch": self.company.branches.first().pk,
             "notes": "Eight drums out to the trade counter"},
            formset("lines", [
                {"product": second.pk, "description": "Drums", "quantity": "8",
                 "unit_price": "200", "discount_percent": "0", "tax_rate": ""},
            ], blank=BLANK_INVOICE_LINE)))
        order = SalesOrder.objects.filter(company=company, reference="SO-SMOKE-18").first()
        if not self.expect("the customer order is on file", order is not None):
            return
        self.post(client, "issue nothing",
                  reverse("sales:order_deliver_record", args=[order.pk]),
                  {"warehouse": self.warehouse.pk})
        self.expect("a draft order cannot be issued against",
                    order.status == "DRAFT"
                    and not StockMovement.objects.filter(sales_order=order).exists(),
                    order.status)

        self.post(client, "confirm the drum order",
                  reverse("sales:order_confirm", args=[order.pk]), {})
        order.refresh_from_db()
        so_line = order.lines.order_by("id").first()

        self.post(client, "deliver five drums",
                  reverse("sales:order_deliver_record", args=[order.pk]),
                  {f"deliver_qty_{so_line.pk}": "5", "warehouse": self.warehouse.pk})
        issue = StockMovement.objects.filter(sales_order=order).first()
        self.expect("the issue is priced by the shelf, not by the person standing at it",
                    issue is not None and issue.unit_cost == D("150.0000")
                    and issue.total_cost == D("750.00"),
                    f"{issue.unit_cost} × 5 = {issue.total_cost}" if issue else "no movement")
        self.expect("cost of sales takes the average the stock is credited at",
                    self.account("5000").current_balance == cogs0 + D("750.00")
                    and self.account("1400").current_balance == stock1 - D("750.00"),
                    f"5000 {self.account('5000').current_balance}, "
                    f"1400 {self.account('1400').current_balance}")
        order.refresh_from_db()
        so_line.refresh_from_db()
        level = shelf()
        self.expect("a part issue leaves the order part delivered",
                    order.status == "PARTIALLY_DELIVERED"
                    and so_line.delivered_quantity == D("5.0000")
                    and level.quantity == D("15.0000"),
                    f"{order.status} / {level.quantity} on the shelf")
        self.expect("issuing goods books two lines and nothing else",
                    self.posted_lines().count() == posted0 + 6,
                    str(self.posted_lines().count()))

        resp = client.post(reverse("sales:order_deliver_record", args=[order.pk]),
                           {f"deliver_qty_{so_line.pk}": "6", "warehouse": self.warehouse.pk})
        self.expect_flash(client, resp, "still outstanding",
                          "asking for more than the order has left is refused")
        so_line.refresh_from_db()
        self.expect("the refused over-issue moved nothing",
                    so_line.delivered_quantity == D("5.0000")
                    and shelf().quantity == D("15.0000")
                    and StockMovement.objects.filter(sales_order=order).count() == 1,
                    f"{so_line.delivered_quantity} delivered, {shelf().quantity} on the shelf, "
                    f"{StockMovement.objects.filter(sales_order=order).count()} movement(s)")

        self.post(client, "deliver the last three drums",
                  reverse("sales:order_deliver_record", args=[order.pk]),
                  {f"deliver_qty_{so_line.pk}": "3", "warehouse": self.warehouse.pk})
        order.refresh_from_db()
        so_line.refresh_from_db()
        self.expect("the order is delivered once every drum has gone",
                    order.status == "DELIVERED" and so_line.delivered_quantity == D("8.0000")
                    and shelf().quantity == D("12.0000"),
                    f"{order.status} / {shelf().quantity}")

        self.post(client, "convert the delivered order",
                  reverse("sales:order_invoice", args=[order.pk]), {})
        order.refresh_from_db()
        invoice = order.invoices.first()
        if not self.expect("the order converted into one invoice", invoice is not None):
            return
        self.post(client, "post the drum invoice",
                  reverse("sales:invoice_post", args=[invoice.pk]), {})
        self.expect("the sale is revenue and the shelf pays for it — a margin, not a guess",
                    self.account("4200").current_balance == revenue0 + D("1600.00")
                    and self.account("5000").current_balance == cogs0 + D("1200.00"),
                    f"revenue {self.account('4200').current_balance - revenue0} "
                    f"against cost {self.account('5000').current_balance - cogs0}, "
                    f"margin {self.account('4200').current_balance - revenue0
                           - (self.account('5000').current_balance - cogs0)}")
        self.expect("the delivered quantities survived being billed",
                    order.lines.order_by("id").first().delivered_quantity == D("8.0000"),
                    str(order.lines.order_by("id").first().delivered_quantity))

        # ── C. a shortage is written off, not sold ──
        shrink0 = self.account("5300").current_balance
        stock2 = self.account("1400").current_balance
        self.post(client, "write two drums off a count",
                  reverse("inventory:adjustment_create"), {
                      "product": second.pk, "warehouse": self.warehouse.pk,
                      "movement_type": "ADJUSTMENT_MINUS", "quantity": "2",
                      "unit_cost": "0", "movement_date": str(self.today),
                      "reference": "COUNT-18", "notes": "Two drums missing from the rack",
                  })
        shortage = StockMovement.objects.filter(
            product=second, movement_type="ADJUSTMENT_MINUS").first()
        self.expect("an unpriced write-off takes the shelf's own value",
                    shortage is not None and shortage.unit_cost == D("150.0000")
                    and shortage.total_cost == D("300.00"),
                    f"{shortage.unit_cost if shortage else 'none'} / "
                    f"{shortage.total_cost if shortage else 'none'}")
        self.expect("the shortage goes to the adjustment account, not to cost of sales",
                    self.account("5300").current_balance == shrink0 + D("300.00")
                    and self.account("1400").current_balance == stock2 - D("300.00")
                    and self.account("5000").current_balance == cogs0 + D("1200.00"),
                    f"5300 +{self.account('5300').current_balance - shrink0}")

        # ── D. a movement the shelf cannot cover leaves nothing behind ──
        self.post(client, "order more drums than there are",
                  reverse("sales:order_create"), merge(
                      {"reference": "SO-SMOKE-18B", "customer": self.customer.pk,
                       "order_date": str(self.today),
                       "delivery_date": str(self.today + datetime.timedelta(days=3)),
                       "warehouse": self.warehouse.pk, "sales_rep": self.user.pk,
                       "currency": "KES", "discount_percent": "0",
                       "branch": self.company.branches.first().pk,
                       "notes": "Twelve drums, ten on the shelf"},
                      formset("lines", [
                          {"product": second.pk, "description": "Drums", "quantity": "12",
                           "unit_price": "200", "discount_percent": "0", "tax_rate": ""},
                      ], blank=BLANK_INVOICE_LINE)))
        order_b = SalesOrder.objects.filter(company=company, reference="SO-SMOKE-18B").first()
        if not self.expect("the oversize order is on file", order_b is not None):
            return
        self.post(client, "confirm the oversize order",
                  reverse("sales:order_confirm", args=[order_b.pk]), {})
        lines_before = self.posted_lines().count()
        stock_before = self.account("1400").current_balance
        cogs_before = self.account("5000").current_balance
        on_shelf = shelf().quantity
        resp = client.post(reverse("sales:order_deliver_record", args=[order_b.pk]),
                           {f"deliver_qty_{order_b.lines.order_by('id').first().pk}": "12",
                            "warehouse": self.warehouse.pk})
        self.expect_flash(client, resp, "insufficient stock",
                          "the issue that the shelf cannot cover is refused")
        self.expect("a refused issue writes no movement row",
                    not StockMovement.objects.filter(sales_order=order_b).exists(),
                    str([str(m.quantity) for m in
                         StockMovement.objects.filter(sales_order=order_b)]))
        self.expect("and books no entry — the shelf and the ledger are still one event",
                    self.posted_lines().count() == lines_before
                    and self.account("1400").current_balance == stock_before
                    and self.account("5000").current_balance == cogs_before
                    and shelf().quantity == on_shelf,
                    f"1400 {self.account('1400').current_balance}, "
                    f"shelf {shelf().quantity} against {on_shelf}")
        order_b.refresh_from_db()
        self.expect("a refused issue leaves the order where it was",
                    order_b.status == "CONFIRMED"
                    and order_b.lines.first().delivered_quantity == Z,
                    f"{order_b.status} / {order_b.lines.first().delivered_quantity}")

        # ── E. moving stock between two of this company's own shelves ──
        upcountry = Warehouse.objects.create(
            company=company, name="Upcountry Cache", code="AT-18")
        lines_before = self.posted_lines().count()
        stock_before = self.account("1400").current_balance
        self.post(client, "transfer four drums upcountry", reverse("inventory:stock_transfer"), {
            "from_warehouse": self.warehouse.pk, "to_warehouse": upcountry.pk,
            "product": second.pk, "quantity": "4",
            "movement_date": str(self.today), "reference": "TRF-18",
        })
        home = shelf()
        away = shelf(upcountry)
        self.expect("a transfer moves stock and books nothing",
                    self.posted_lines().count() == lines_before
                    and self.account("1400").current_balance == stock_before,
                    f"1400 {self.account('1400').current_balance} against {stock_before}")
        self.expect("the two shelves carry the same value per drum",
                    home.quantity == D("6.0000") and away.quantity == D("4.0000")
                    and home.avg_cost == away.avg_cost == D("150.0000"),
                    f"{home.quantity} @ {home.avg_cost} and "
                    f"{away.quantity if away else 'none'} @ "
                    f"{away.avg_cost if away else 'none'}")

        book_dr, book_cr = self.book_totals()
        self.expect("the book still balances to itself after the whole costing run",
                    book_dr == book_cr, f"Dr {book_dr} / Cr {book_cr}")

        for label, url in [
            ("goods issue screen", reverse("sales:order_deliver", args=[order.pk])),
            ("stock movements", reverse("inventory:movement_list")),
            ("inventory home", reverse("inventory:index")),
            ("warehouse detail", reverse("inventory:warehouse_detail", args=[self.warehouse.pk])),
            ("the upcountry shelf", reverse("inventory:warehouse_detail", args=[upcountry.pk])),
            ("stock by warehouse", reverse("inventory:stock_levels")),
            ("the received-not-invoiced account's ledger",
             reverse("accounting:account_ledger", args=[self.account("2110").pk])),
        ]:
            self.get_ok(client, label, url)

        self.note("Stock is costed where it is bought and released where it goes: a "
                  "receipt values the shelf at the order's own price and credits 2110, "
                  "the bill clears that accrual instead of charging stock twice, an "
                  "issue is priced by the moving average the shelf holds and lands in "
                  "cost of sales, a write-off goes through 5300, production runs "
                  "through 1450 work in progress, a transfer books nothing, and a "
                  "movement the ledger refuses is rolled back with its own row. What "
                  "is NOT here: there is no supplier-return screen yet, so RETURN_OUT "
                  "routing exists in the ledger but nothing raises it; a movement "
                  "carries only book money, so goods bought in a foreign currency are "
                  "held at the order's rate on the day it was written rather than "
                  "revalued lot by lot; and valuation is weighted average — the FIFO "
                  "choice on the product form is not yet honoured by the shelf.")

    def verify_counter_sale(self, client):
        """The counter: notes in, goods out, change back — and the way home for
        goods that come back.

        A till is the one sale in this book that is paid for as it is made, so it
        must never reach receivables: the drawer or the bank is debited and revenue
        and VAT credited in the same entry that the goods leave the shelf on. The
        ticket is priced here by the product card rather than by the browser, so a
        posted price cannot be talked down at the counter, and the two halves — the
        movement and the journal — are one event: whatever the ledger refuses, the
        shelf gives back. A refund is a second ticket rather than an edit of the
        first, because the money that came in and the money that went out are two
        facts the cash-up has to be able to show separately.
        """
        from apps.accounting.models import JournalEntry
        from apps.inventory.models import StockLevel, StockMovement
        from apps.pos.models import POSSale, POSSession

        # The terminal and refund views now refuse a post without the form's
        # one-time token (it is what stops a double click taking the money
        # twice). Every till post below therefore goes the way a browser's
        # does: open the form, then send it back with the token it rendered.
        # The checks themselves are unchanged.
        client = _FormTokenClient(client)

        company = self.company
        branch = company.branches.first()
        product = self.product

        def shelf():
            return StockLevel.objects.filter(
                product=product, warehouse=self.warehouse).first()

        def tickets():
            return POSSale.objects.filter(session__company=company)

        def landed(response):
            """The ticket a successful POST's redirect landed on."""
            location = (response.get("Location")
                        if response.status_code in (301, 302, 303) else "")
            match = re.search(r"/sales/([0-9a-fA-F-]{36})/?$", location or "")
            return tickets().filter(pk=match.group(1)).first() if match else None

        cash0 = self.account("1100").current_balance
        bank0 = self.account("1200").current_balance
        ar0 = self.account("1300").current_balance
        stock0 = self.account("1400").current_balance
        revenue0 = self.account("4200").current_balance
        vat0 = self.account("2200").current_balance
        cogs0 = self.account("5000").current_balance
        lines0 = self.posted_lines().count()

        # ── A. nothing rings before somebody opens a drawer ──
        resp = client.post(reverse("pos:terminal"), {f"qty_{product.pk}": "1"})
        self.expect_flash(client, resp, "Open the till",
                          "a ticket cannot be rung on a shut drawer")
        self.expect("the ticket rung before the session wrote nothing",
                    not tickets().exists() and self.posted_lines().count() == lines0,
                    f"{tickets().count()} ticket(s)")
        self.get_ok(client, "point of sale home", reverse("pos:home"))
        self.get_ok(client, "till terminal", reverse("pos:terminal"))

        self.post(client, "open the till", reverse("pos:session_open"), {
            "branch": branch.pk, "opening_cash": "2000.00"})
        session = POSSession.objects.filter(
            company=company, cashier=self.user, status="OPEN").first()
        if not self.expect("the drawer is open", session is not None):
            return
        self.expect("the till holds the float that was counted into it",
                    session.opening_cash == D("2000.00") and session.branch_id == branch.pk
                    and session.expected_cash == D("2000.00"),
                    f"float {session.opening_cash}, should hold "
                    f"{session.expected_cash} at {session.branch.name}")
        self.post(client, "opening a till that is already open",
                  reverse("pos:session_open"), {"branch": branch.pk,
                                               "opening_cash": "5000.00"})
        self.expect("a second open hands back the running till rather than a parallel one",
                    POSSession.objects.filter(company=company, cashier=self.user).count() == 1
                    and POSSession.objects.get(pk=session.pk).opening_cash == D("2000.00"),
                    f"{POSSession.objects.filter(company=company, cashier=self.user).count()} "
                    "session(s) for this cashier")
        self.get_ok(client, "terminal with a till open", reverse("pos:terminal"))
        self.get_ok(client, "point of sale home with a till open", reverse("pos:home"))

        # ── B. a cash ticket: what the card says, not what the browser typed ──
        level0 = shelf()
        if not self.expect("the shelf holds goods to sell at the counter",
                           level0 is not None and level0.quantity >= D("5"),
                           str(level0.quantity if level0 else "empty")):
            return
        avg = level0.avg_cost

        resp = self.post(client, "cash ticket", reverse("pos:terminal"), {
            f"qty_{product.pk}": "2", f"unit_price_{product.pk}": "1.00",
            "payment_method": "CASH", "amount_tendered": "300.00",
            "customer": self.customer.pk, "warehouse": self.warehouse.pk,
            "notes": "Two widgets, paid in notes"})
        sale = landed(resp)
        if not self.expect("the ticket was rung and the cashier sent to it",
                           sale is not None, f"landed on HTTP {resp.status_code}"):
            return
        self.expect("the till prices the goods off the card, not off the post",
                    sale.subtotal == D("200.00") and sale.tax_amount == D("32.00")
                    and sale.total_amount == D("232.00"),
                    f"{sale.subtotal} + {sale.tax_amount} = {sale.total_amount} "
                    f"though the browser posted 1.00 each")
        self.expect("the money handed over is counted, not assumed",
                    sale.amount_tendered == D("300.00") and sale.change_given == D("68.00")
                    and sale.status == "COMPLETED" and sale.reference.startswith("POS-"),
                    f"{sale.reference}: {sale.amount_tendered} in, {sale.change_given} back")
        self.expect("the ticket remembers whose drawer rang it and which shelf emptied",
                    sale.session_id == session.pk and sale.warehouse_id == self.warehouse.pk
                    and sale.customer_id == self.customer.pk,
                    f"{sale.session.cashier.username} @ {sale.warehouse.name}")
        line = sale.lines.first()
        self.expect("the line carries the tax rate it was charged at",
                    line.quantity == D("2.0000") and line.unit_price == D("100.0000")
                    and line.tax_amount == D("32.00") and line.line_total == D("232.00")
                    and line.tax_rate_id == self.vat.pk,
                    f"{line.quantity} @ {line.unit_price} = {line.line_total} "
                    f"of which {line.tax_amount} VAT")

        je = sale.journal_entry
        self.expect("a till sale books a receipt of its own, posted and dated in its period",
                    je is not None and je.status == "POSTED" and je.entry_type == "RECEIPT"
                    and je.reference.startswith("JE-POS-") and je.period_id is not None,
                    f"{je.reference if je else 'none'} "
                    f"{je.entry_type if je else ''}/{je.status if je else ''}")
        if not je:
            return
        dr, cr, ok = self.balanced(je)
        self.expect("the receipt balances on its own", ok, f"Dr {dr} / Cr {cr}")
        self.expect("the drawer is debited with the bill and not with the tender",
                    self.line_total(je, "1100", "debit") == D("232.00")
                    and self.line_total(je, "1100", "credit") == Z,
                    f"1100 +{self.line_total(je, '1100', 'debit')}, though 300 was handed over")
        self.expect("revenue takes the net and the taxman's share goes to VAT",
                    self.line_total(je, "4200", "credit") == D("200.00")
                    and self.line_total(je, "2200", "credit") == D("32.00"),
                    f"4200 +200.00, 2200 +32.00")
        self.expect("the book moves by exactly that much",
                    self.account("1100").current_balance == cash0 + D("232.00")
                    and self.account("4200").current_balance == revenue0 + D("200.00")
                    and self.account("2200").current_balance == vat0 + D("32.00")
                    # five lines, not three: the receipt's three plus the goods
                    # issue's cost of sales against stock
                    and self.posted_lines().count() == lines0 + 5,
                    f"1100 {self.account('1100').current_balance - cash0}, "
                    f"{self.posted_lines().count() - lines0} line(s) posted")
        self.expect("a counter sale is paid on the spot, so receivables never hear about it",
                    self.account("1300").current_balance == ar0,
                    f"1300 {self.account('1300').current_balance}")

        movement = StockMovement.objects.filter(
            product=product, movement_type="DELIVERY", reference=sale.reference).first()
        self.expect("the goods leave the shelf at what the shelf holds them at",
                    movement is not None and movement.unit_cost == avg
                    and movement.total_cost == (avg * D("2")).quantize(D("0.01")),
                    f"{movement.unit_cost if movement else 'none'} × 2 = "
                    f"{movement.total_cost if movement else 'none'} (card cost "
                    f"{product.cost_price})")
        cost_of_two = movement.total_cost
        self.expect("cost of sales is the money stock was credited with",
                    shelf().quantity == level0.quantity - D("2.0000")
                    and self.account("1400").current_balance == stock0 - cost_of_two
                    and self.account("5000").current_balance == cogs0 + cost_of_two,
                    f"shelf {shelf().quantity}, 1400 -{cost_of_two}, 5000 +{cost_of_two}")
        session.refresh_from_db()
        self.expect("the till's own running figures agree with its tickets",
                    session.total_sales == D("232.00") and session.total_refunds == Z
                    and session.expected_cash == D("2232.00"),
                    f"sales {session.total_sales}, drawer should hold "
                    f"{session.expected_cash}")

        # ── C. a card ticket: the bank's money, not the drawer's ──
        bank_lines = self.posted_lines().count()
        resp = self.post(client, "card ticket", reverse("pos:terminal"), {
            f"qty_{product.pk}": "1", "payment_method": "CARD"})
        card = landed(resp)
        if not self.expect("the card ticket is on file", card is not None):
            return
        self.expect("a slip pays the bill it was quoted for, with no change to count",
                    card.total_amount == D("116.00")
                    and card.amount_tendered == D("116.00") and card.change_given == Z
                    and card.payment_method == "CARD",
                    f"{card.total_amount} tendered, {card.change_given} back")
        cje = card.journal_entry
        self.expect("the bank is debited where the drawer was",
                    cje is not None and self.line_total(cje, "1200", "debit") == D("116.00")
                    and self.line_total(cje, "1100", "debit") == Z,
                    f"1200 +{self.line_total(cje, '1200', 'debit') if cje else 'none'}")
        self.expect("a card ticket leaves the drawer alone",
                    self.account("1100").current_balance == cash0 + D("232.00")
                    and self.account("1200").current_balance == bank0 + D("116.00")
                    and POSSession.objects.get(pk=session.pk).expected_cash == D("2232.00"),
                    f"drawer still {POSSession.objects.get(pk=session.pk).expected_cash}")
        self.expect("a ticket with no warehouse named still comes off the till's own shelf",
                    card.warehouse_id == self.warehouse.pk,
                    card.warehouse.name if card.warehouse else "none")
        self.expect("and a card ticket is the same five lines through the bank",
                    self.posted_lines().count() == bank_lines + 5,
                    f"{self.posted_lines().count() - bank_lines} line(s)")

        # ── D. one widget goes back ──
        stock1 = self.account("1400").current_balance
        revenue1 = self.account("4200").current_balance
        vat1 = self.account("2200").current_balance
        cogs1 = self.account("5000").current_balance
        cash1 = self.account("1100").current_balance
        shelf1 = shelf()
        resp = self.post(client, "refund one of the two", reverse("pos:sale_refund",
                                                                 args=[sale.pk]), {
            f"refund_qty_{line.pk}": "1", "reason": "Wrong colour"})
        back = landed(resp)
        if not self.expect("the refund is a ticket of its own", back is not None,
                           f"landed on HTTP {resp.status_code}"):
            return
        self.expect("it is marked as a refund and priced at what the customer paid",
                    back.refund_of_id == sale.pk and back.status == "REFUNDED"
                    and back.reference.startswith("POS-R")
                    and back.total_amount == D("116.00")
                    and back.tax_amount == D("16.00") and back.notes == "Wrong colour",
                    f"{back.reference} {back.total_amount} ({back.notes})")
        sale.refresh_from_db()
        self.expect("the original is part refunded, never rewritten",
                    sale.status == "PARTIAL_REFUND"
                    and sale.refunded_amount == D("116.00")
                    and sale.total_amount == D("232.00"),
                    f"{sale.status}, {sale.refunded_amount} of {sale.total_amount} back")
        rje = back.journal_entry
        self.expect("the refund runs the receipt backwards through a payment entry",
                    rje is not None and rje.entry_type == "PAYMENT"
                    and rje.reference.startswith("JE-RFND-")
                    and self.line_total(rje, "1100", "credit") == D("116.00")
                    and self.line_total(rje, "4200", "debit") == D("100.00")
                    and self.line_total(rje, "2200", "debit") == D("16.00"),
                    f"{rje.reference if rje else 'none'}")
        self.expect("revenue and VAT come back down by exactly what they went up",
                    self.account("4200").current_balance == revenue1 - D("100.00")
                    and self.account("2200").current_balance == vat1 - D("16.00")
                    and self.account("1100").current_balance == cash1 - D("116.00"),
                    f"4200 {self.account('4200').current_balance - revenue1}, "
                    f"drawer {self.account('1100').current_balance - cash1}")
        returned = StockMovement.objects.filter(
            movement_type="RETURN_IN", reference=back.reference).first()
        if not self.expect("the goods go back on the shelf they came off",
                           returned is not None
                           and returned.warehouse_id == self.warehouse.pk,
                           f"{returned.warehouse.name if returned else 'no movement'}"):
            return
        self.expect("the return lands at the same average the issue left at",
                    returned.unit_cost == avg and returned.total_cost
                    == (avg * D("1")).quantize(D("0.01"))
                    and shelf().quantity == shelf1.quantity + D("1.0000"),
                    f"{returned.unit_cost} × 1 = {returned.total_cost}, "
                    f"shelf back to {shelf().quantity}")
        self.expect("and stock is revalued by the same average that released it",
                    self.account("1400").current_balance == stock1 + returned.total_cost
                    and self.account("5000").current_balance == cogs1 - returned.total_cost,
                    f"1400 +{returned.total_cost}, 5000 -{returned.total_cost}")
        session.refresh_from_db()
        self.expect("the till shows notes out as well as notes in",
                    session.total_refunds == D("116.00")
                    and session.expected_cash == D("2116.00"),
                    f"refunds {session.total_refunds}, drawer "
                    f"{session.expected_cash}")

        # ── E. what the counter refuses ──
        resp = client.post(reverse("pos:sale_refund", args=[sale.pk]),
                           {f"refund_qty_{line.pk}": "2", "reason": "And then both"})
        self.expect_flash(client, resp, "still returnable",
                          "one widget was already back, so two more cannot be")
        sale.refresh_from_db()
        self.expect("the over-refund gave nothing back",
                    tickets().filter(refund_of=sale).count() == 1
                    and sale.refunded_amount == D("116.00")
                    and shelf().quantity == shelf1.quantity + D("1.0000"),
                    f"{sale.refunded_amount} refunded, shelf {shelf().quantity}")
        resp = client.post(reverse("pos:sale_refund", args=[back.pk]),
                           {f"refund_qty_{back.lines.first().pk}": "1"})
        self.expect_flash(client, resp, "itself a refund",
                          "a refund ticket cannot be refunded again")
        resp = client.post(reverse("pos:terminal"), {
            f"qty_{product.pk}": "1", "payment_method": "CASH",
            "amount_tendered": "10.00"})
        self.expect_flash(client, resp, "tendered for a bill",
                          "notes that do not cover the bill are not a sale")
        resp = client.post(reverse("pos:terminal"), {"payment_method": "CASH"})
        self.expect_flash(client, resp, "Nothing to sell",
                          "an empty ticket books nothing")
        resp = client.post(reverse("pos:terminal"), {
            f"qty_{product.pk}": "1", "payment_method": "SPLIT"})
        self.expect_flash(client, resp, "split tender",
                          "two tenders on one ticket would be two untold drawers")
        resp = client.post(reverse("pos:terminal"), {f"qty_999999": "1"})
        self.expect_flash(client, resp, "not on this company",
                          "another company's goods cannot be rung here")
        resp = client.post(reverse("pos:terminal"), {
            f"qty_{product.pk}": "99999", "payment_method": "CASH"})
        self.expect_flash(client, resp, "Insufficient stock",
                          "goods the shelf does not hold cannot be sold")
        self.expect("a refusal at the counter leaves neither ticket nor entry nor a moved shelf",
                    tickets().count() == 3 and self.posted_lines().count() == bank_lines + 10
                    and shelf().quantity == shelf1.quantity + D("1.0000")
                    and self.account("1400").current_balance == stock1 + returned.total_cost,
                    f"{tickets().count()} ticket(s), "
                    f"{self.posted_lines().count() - bank_lines} line(s) since the card ticket, "
                    f"shelf {shelf().quantity}")

        # ── F. counting the drawer and closing the shift ──
        self.get_ok(client, "cash-up sheet", reverse("pos:cash_up", args=[session.pk]))
        self.get_ok(client, "till session page", reverse("pos:session_detail",
                                                         args=[session.pk]))
        self.get_ok(client, "ticket page", reverse("pos:sale_detail", args=[sale.pk]))
        self.get_ok(client, "refund page", reverse("pos:sale_detail", args=[back.pk]))
        self.get_ok(client, "all tickets", reverse("pos:sale_list"))
        self.get_ok(client, "all sessions", reverse("pos:session_list"))
        self.get_ok(client, "the till receipt's ledger entry",
                    reverse("accounting:journal_entry_detail", args=[rje.pk]))

        self.post(client, "close the till with the drawer proved",
                  reverse("pos:session_close", args=[session.pk]),
                  {"closing_cash": "2116.00"})
        session.refresh_from_db()
        self.expect("a count that agrees is a variance of nothing",
                    session.status == "CLOSED" and session.closing_cash == D("2116.00")
                    and session.variance == Z and session.closed_at is not None,
                    f"counted {session.closing_cash} against "
                    f"{session.expected_cash}, variance {session.variance}")
        resp = client.post(reverse("pos:session_close", args=[session.pk]),
                           {"closing_cash": "2116.00"})
        self.expect_flash(client, resp, "already closed",
                          "a proved drawer is not counted twice")
        resp = client.post(reverse("pos:terminal"), {f"qty_{product.pk}": "1"})
        self.expect_flash(client, resp, "Open the till",
                          "a proved shift cannot be sold on again")
        self.expect("and the closed shift took no ticket after its count",
                    tickets().filter(session=session,
                                     created_at__gt=session.closed_at).count() == 0,
                    f"{tickets().filter(session=session).count()} ticket(s) on it")

        self.post(client, "open the next shift", reverse("pos:session_open"), {
            "branch": branch.pk, "opening_cash": "0"})
        next_shift = POSSession.objects.filter(
            company=company, cashier=self.user, status="OPEN").first()
        if not self.expect("the next cashier gets a till of their own",
                           next_shift is not None and next_shift.pk != session.pk):
            return
        self.expect("a closed shift is not counted into the new one",
                    next_shift.total_sales == Z and next_shift.expected_cash == Z,
                    f"{next_shift.total_sales} carried over")
        resp = self.post(client, "ticket on the new till", reverse("pos:terminal"), {
            f"qty_{product.pk}": "1", "payment_method": "CASH",
            "amount_tendered": "200.00"})
        evening = landed(resp)
        next_shift.refresh_from_db()
        self.expect("the new till rings its own tickets",
                    evening is not None and evening.session_id == next_shift.pk
                    and evening.change_given == D("84.00")
                    and next_shift.total_sales == D("116.00")
                    and next_shift.expected_cash == D("116.00"),
                    f"{evening.reference if evening else 'none'} on a till holding "
                    f"{next_shift.total_sales}")
        self.post(client, "close the second shift on a short drawer",
                  reverse("pos:session_close", args=[next_shift.pk]),
                  {"closing_cash": "100.00"})
        next_shift.refresh_from_db()
        self.expect("a drawer that is short is shown as short, not smoothed over",
                    next_shift.variance == D("-16.00"),
                    f"counted {next_shift.closing_cash} against "
                    f"{next_shift.expected_cash}")

        from apps.pos import services
        third = services.open_session(company, self.user, branch=branch,
                                      opening_cash=D("100.00"))
        self.post(client, "close a till nobody counted", reverse("pos:session_close",
                                                                 args=[third.pk]), {})
        third.refresh_from_db()
        self.expect("a till can be shut without a count, but it is not proved",
                    third.status == "CLOSED" and third.closing_cash is None
                    and third.variance is None and third.opening_cash == D("100.00"),
                    "closed uncounted")

        book_dr, book_cr = self.book_totals()
        self.expect("the book still balances to itself after a day at the counter",
                    book_dr == book_cr, f"Dr {book_dr} / Cr {book_cr}")
        self.expect("every ticket this till rang is posted, and every posting is a ticket",
                    tickets().exclude(journal_entry=None).count() == tickets().count()
                    and JournalEntry.objects.filter(
                        company=company, reference__startswith="JE-POS-").count()
                    + JournalEntry.objects.filter(
                        company=company, reference__startswith="JE-RFND-").count()
                    == tickets().count(),
                    f"{tickets().count()} ticket(s)")

        self.note("The counter keeps its own counsel: a ticket is priced off the product card "
                  "rather than off the browser, is paid into the drawer or the bank in the same "
                  "entry that credits revenue and VAT, and never touches receivables because "
                  "nothing is owed after it. Goods leave and return at the shelf's own average, "
                  "so the movement and the journal are the same money, and a refusal — short "
                  "notes, short stock, an over-refund, a split tender — leaves neither ticket, "
                  "nor entry, nor a moved shelf behind. A refund is a second ticket, so the "
                  "cash-up can show notes in and notes out separately, and a drawer closed "
                  "without a count is marked unproved rather than assumed right. What is NOT "
                  "here: the till takes no split tender and gives no till discount, so a "
                  "customer paying part in cash and part on a slip is rung as two tickets and "
                  "an agreed counter discount would have to be a sales order instead.")

    # ────────────────── receipts, allocations and reversals ──────────────────

    def verify_allocations(self, client):
        """A receipt has to say which paper it settles, and a bounced receipt has
        to give the whole of it back through the same door it came in.

        Three papers are raised on a fresh account so every movement in
        receivables belongs to this section alone: an over-allocation, a paper
        that is not this customer's, a paper in another money, a box of words and
        a receipt that does not add up are each refused before anything is
        written; a good receipt settles one paper in full and part of another; a
        receipt bigger than everything owed leaves the rest resting on the
        account without calling that a gain on the rate; and reversing the two
        receipts puts the cash, the receivable and both invoices' names back, so
        the account ends exactly where it began.
        """
        from apps.crm.models import Customer
        from apps.sales.models import CustomerPayment, Invoice, PaymentAllocation

        company = self.company
        payer = Customer.objects.create(company=company, name="Githurai Builders",
                                        is_active=True)

        def paper(reference, quantity, currency="KES"):
            self.post(client, f"invoice {reference}", reverse("sales:invoice_create"), merge(
                {"reference": reference, "customer": payer.pk, "currency": currency,
                 "invoice_date": str(self.today),
                 "due_date": str(self.today + datetime.timedelta(days=30)),
                 "notes": "Allocation test"},
                formset("lines", [
                    {"product": self.product.pk, "description": "Widget",
                     "quantity": str(quantity), "unit_price": "100",
                     "discount_percent": "0", "tax_rate": self.vat.pk}],
                    blank=BLANK_INVOICE_LINE)))
            return Invoice.objects.filter(company=company, reference=reference).first()

        first, second = paper("INV-ALLOC-1", 2), paper("INV-ALLOC-2", 5)
        foreign = paper("INV-ALLOC-3", 1, currency="USD")
        if not self.expect("three papers raised on one account",
                           first is not None and second is not None
                           and foreign is not None
                           and first.total_amount == D("232.00")
                           and second.total_amount == D("580.00")
                           and foreign.total_amount == D("116.00"),
                           f"{first and first.total_amount} / "
                           f"{second and second.total_amount} / "
                           f"{foreign and foreign.total_amount}"):
            return

        ar0 = self.account("1300").current_balance
        cash0 = self.account("1100").current_balance
        lines0 = self.posted_lines().count()

        def pay(reference, amount, **extra):
            return merge({
                "reference": reference, "customer": payer.pk,
                "payment_date": str(self.today), "amount": str(amount),
                "currency": "KES", "payment_method": "CASH", "bank_account": "",
                "transaction_reference": "", "notes": "",
            }, extra)

        def why(response):
            form = (getattr(response, "context", None) or {}).get("form")
            if form is None:
                return ""
            return " | ".join(sum(form.errors.values(), []))

        # ── A. an allocation that cannot be true is refused, and nothing is
        #      written while it is being refused ──
        other = Invoice.objects.filter(company=company,
                                        reference="INV-SMOKE-1").first()
        refusals = [
            ("settling more than a paper is owed",
             pay("PAY-BAD-1", "300", **{f"allocate_{first.pk}": "300.00"}),
             "only owed"),
            ("settling a paper of somebody else",
             pay("PAY-BAD-2", "300", **{f"allocate_{other.pk}": "300.00"}),
             "does not owe"),
            ("settling a dollar paper with shilling money",
             pay("PAY-BAD-3", "116", **{f"allocate_{foreign.pk}": "116.00"}),
             "billed in USD"),
            ("settling papers that add up beyond the money received",
             pay("PAY-BAD-4", "300", **{f"allocate_{first.pk}": "232.00",
                                        f"allocate_{second.pk}": "400.00"}),
             "add up to"),
            ("typing words in a settled column",
             pay("PAY-BAD-5", "100", **{f"allocate_{first.pk}": "ten"}),
             "is not an"),
            ("typing a negative figure against a paper",
             pay("PAY-BAD-6", "100", **{f"allocate_{first.pk}": "-50"}),
             "money going"),
            ("a receipt for nothing at all",
             pay("PAY-BAD-7", "0"), "more than nothing"),
        ]
        for name, payload, needle in refusals:
            resp = self.reject(client, name, reverse("sales:payment_create"), payload)
            self.expect(f"{name} explains itself", needle in why(resp), why(resp))
        self.expect("every refusal left neither a receipt nor an entry behind",
                    CustomerPayment.objects.filter(company=company, customer=payer)
                    .count() == 0 and self.posted_lines().count() == lines0,
                    f"{CustomerPayment.objects.filter(company=company, customer=payer).count()} "
                    f"receipt(s), {self.posted_lines().count() - lines0} new line(s)")

        resp = self.get_ok(client, "receipt form with the account chosen",
                           reverse("sales:payment_create") + f"?customer={payer.pk}")
        self.expect("the form lists the papers this account owes",
                    len(resp.context["allocation_rows"]) == 3
                    and resp.context["chosen"].pk == payer.pk
                    and payer.name in [c.name for c in resp.context["payers"]],
                    f"{len(resp.context['allocation_rows'])} paper(s)")

        # ── B. a receipt that names its papers ──
        resp = self.post(client, "receipt against two papers",
                         reverse("sales:payment_create"),
                         pay("PAY-ALLOC-1", "400", **{f"allocate_{first.pk}": "232.00",
                                                      f"allocate_{second.pk}": "168.00"}))
        self.expect_flash(client, resp, "settled against 2 paper(s)",
                          "the receipt says what it settled")
        first.refresh_from_db()
        second.refresh_from_db()
        one = CustomerPayment.objects.filter(company=company,
                                             reference="PAY-ALLOC-1").first()
        if not self.expect("the receipt was taken", one is not None):
            return
        self.expect("the paper paid in full is called paid",
                    first.status == "PAID" and first.outstanding_amount == Z
                    and first.paid_amount == D("232.00"),
                    f"{first.status} at {first.outstanding_amount}")
        self.expect("the part-paid paper keeps the balance",
                    second.status == "PARTIAL"
                    and second.outstanding_amount == D("412.00"),
                    f"{second.status} at {second.outstanding_amount}")
        entry = one.journal_entry
        if not self.expect("notes handed in are debited to the drawer, not the bank",
                           entry is not None
                           and entry.reference.startswith("JE-PAY-")
                           and self.line_total(entry, "1100", "debit") == D("400.00"),
                           f"{entry and entry.reference}"):
            return
        self.expect("the receivable is cleared by exactly the money received",
                    self.line_total(entry, "1300", "credit") == D("400.00")
                    and self.balanced(entry) and entry.lines.count() == 2,
                    f"cr {self.line_total(entry, '1300', 'credit')} on "
                    f"{entry.lines.count()} line(s)")
        self.expect("shillings in against shillings owed invents no exchange gain",
                    self.line_total(entry, "4310", "credit") == Z
                    and self.line_total(entry, "7300", "debit") == Z,
                    f"gain {self.line_total(entry, '4310', 'credit')}, "
                    f"loss {self.line_total(entry, '7300', 'debit')}")
        self.expect("the allocation rows tie the receipt to the two papers",
                    PaymentAllocation.objects.filter(
                        payment=one).aggregate(t=Sum("allocated_amount"))["t"]
                    == D("400.00")
                    and PaymentAllocation.objects.filter(
                        payment=one, invoice=first,
                        allocated_amount=D("232.00")).exists(),
                    str(list(PaymentAllocation.objects.filter(payment=one)
                             .values_list("invoice__reference", "allocated_amount"))))
        self.expect("the account is 400 lighter in receivables and 400 heavier in cash",
                    self.account("1300").current_balance == ar0 - D("400.00")
                    and self.account("1100").current_balance == cash0 + D("400.00"),
                    f"AR {self.account('1300').current_balance}, "
                    f"cash {self.account('1100').current_balance}")

        # ── C. a receipt bigger than everything the account owes ──
        self.post(client, "receipt larger than the paper it can settle",
                  reverse("sales:payment_create"), pay("PAY-ALLOC-2", "500"))
        two = CustomerPayment.objects.filter(company=company,
                                             reference="PAY-ALLOC-2").first()
        if not self.expect("the over-wide receipt was taken", two is not None):
            return
        second.refresh_from_db()
        self.expect("what could be settled was, oldest paper first, and the paper is paid",
                    second.status == "PAID" and second.outstanding_amount == Z
                    and PaymentAllocation.objects.filter(
                        payment=two, invoice=second,
                        allocated_amount=D("412.00")).exists(),
                    f"{second.status} at {second.outstanding_amount}")
        self.expect("the 88 that settled nothing rests on the account instead of "
                    "being called a gain",
                    self.line_total(two.journal_entry, "1300", "credit") == D("500.00")
                    and self.line_total(two.journal_entry, "4310", "credit") == Z
                    and self.balanced(two.journal_entry),
                    f"cr 1300 {self.line_total(two.journal_entry, '1300', 'credit')}, "
                    f"gain {self.line_total(two.journal_entry, '4310', 'credit')}")

        # ── D. giving a receipt back ──
        self.post(client, "reverse the receipt that overpaid",
                  reverse("sales:payment_reverse", args=[two.pk]),
                  {"reason": "Cheque bounced at the bank"})
        two.refresh_from_db()
        second.refresh_from_db()
        back = two.journal_entry.reversed_by
        if not self.expect("the receipt is marked given back and mirrored by an entry",
                           two.status == "REVERSED" and back is not None
                           and back.status == "POSTED",
                           f"{two.status}, {back and back.reference}"):
            return
        self.expect("the original is not erased — the book keeps both sides",
                    two.journal_entry.status == "POSTED"
                    and back.entry_type == "REVERSAL"
                    and back.reference.startswith("JE-REV-"),
                    f"{two.journal_entry.status} answered by {back.reference}")
        self.expect("the mirror runs the other way, to the last cent",
                    self.line_total(back, "1300", "debit") == D("500.00")
                    and self.line_total(back, "1100", "credit") == D("500.00")
                    and self.balanced(back),
                    f"dr 1300 {self.line_total(back, '1300', 'debit')}")
        self.expect("it is booked in the period the receipt was in, not today's",
                    back.posting_date == two.journal_entry.posting_date
                    and back.period_id == two.journal_entry.period_id,
                    f"{back.posting_date} period {back.period_id}")
        self.expect("the reason the money went back is written on the entry",
                    "Cheque bounced" in (back.notes or ""), back.notes)
        self.expect("the paper that was called paid is owed again",
                    second.status == "PARTIAL" and second.paid_amount == D("168.00")
                    and second.outstanding_amount == D("412.00"),
                    f"{second.status} at {second.outstanding_amount}")
        self.expect("the allocation rows stay, as the record of what was settled "
                    "and then undone",
                    PaymentAllocation.objects.filter(payment=two).count() == 1,
                    f"{PaymentAllocation.objects.filter(payment=two).count()} row(s)")

        self.post(client, "reverse the receipt that named its papers",
                  reverse("sales:payment_reverse", args=[one.pk]), {"reason": ""})
        one.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.expect("both papers are wholly owed again",
                    first.status == "SENT" and first.outstanding_amount == D("232.00")
                    and second.status == "SENT"
                    and second.outstanding_amount == D("580.00"),
                    f"{first.reference} {first.status} {first.outstanding_amount} / "
                    f"{second.reference} {second.status} {second.outstanding_amount}")
        self.expect("the account is exactly where it stood before either receipt",
                    self.account("1300").current_balance == ar0
                    and self.account("1100").current_balance == cash0,
                    f"AR {self.account('1300').current_balance} against {ar0}, "
                    f"cash {self.account('1100').current_balance} against {cash0}")
        resp = client.post(reverse("sales:payment_reverse", args=[one.pk]),
                           {"reason": "again"})
        self.expect_flash(client, resp, "already been given back",
                          "a receipt is given back once")

        stray = CustomerPayment.objects.create(
            company=company, customer=payer, reference="PAY-UNPOSTED",
            payment_date=self.today, amount=D("10.00"), currency="KES",
            payment_method="CASH")
        resp = client.post(reverse("sales:payment_reverse", args=[stray.pk]), {})
        self.expect_flash(client, resp, "was never posted",
                          "there is no entry to answer")
        stray.refresh_from_db()
        self.expect("and a receipt with no entry stays a receipt",
                    stray.status == "RECEIVED", stray.status)
        # The probe row never belonged to the book — it only ever probed the guard.
        stray.delete()

        # ── E. what the screens tell about all of it ──
        resp = self.get_ok(client, "the receipts list", reverse("sales:payment_list"))
        rows = {p.reference: p for p in resp.context["payments"]}
        if not self.expect("both receipts are on the list",
                           "PAY-ALLOC-1" in rows and "PAY-ALLOC-2" in rows,
                           str(sorted(rows))):
            return
        self.expect("the list shows what each receipt settled and what it left unapplied",
                    rows["PAY-ALLOC-1"].settled_amount == D("400.00")
                    and rows["PAY-ALLOC-1"].unapplied == Z
                    and rows["PAY-ALLOC-2"].settled_amount == D("412.00")
                    and rows["PAY-ALLOC-2"].unapplied == D("88.00"),
                    f"{rows['PAY-ALLOC-1'].settled_amount} settled / "
                    f"{rows['PAY-ALLOC-2'].settled_amount} settled, "
                    f"{rows['PAY-ALLOC-2'].unapplied} unapplied")
        self.expect("and marks both as given back",
                    rows["PAY-ALLOC-1"].status == "REVERSED"
                    and rows["PAY-ALLOC-2"].status == "REVERSED",
                    f"{rows['PAY-ALLOC-1'].status}, {rows['PAY-ALLOC-2'].status}")

        resp = self.get_ok(client, "the customer's statement",
                           reverse("sales:customer_statement", args=[payer.pk]))
        owed = sum((inv.outstanding_base for inv in Invoice.objects.filter(
            company=company, customer=payer,
            status__in=["SENT", "PARTIAL", "OVERDUE"])
            if inv.outstanding_amount > 0), Z)
        self.expect("the statement ends where the papers say it should",
                    resp.context["closing_balance"] == owed,
                    f"statement {resp.context['closing_balance']} against paper {owed}")
        self.expect("the receipts that were given back are listed but not counted",
                    len(resp.context["reversals"]) == 2
                    and not any(r["reference"] in ("PAY-ALLOC-1", "PAY-ALLOC-2")
                                for r in resp.context["rows"]),
                    f"{len(resp.context['reversals'])} reversal(s) shown")
        self.get_ok(client, "a reversed receipt's own ledger entry",
                    reverse("accounting:journal_entry_detail", args=[two.journal_entry.pk]))
        self.get_ok(client, "the reversal that answers it",
                    reverse("accounting:journal_entry_detail", args=[back.pk]))
        self.get_ok(client, "the paper with its reversed receipts on it",
                    reverse("sales:invoice_detail", args=[first.pk]))

        book_dr, book_cr = self.book_totals()
        self.expect("the book still balances to itself after receipts in and back out",
                    book_dr == book_cr, f"Dr {book_dr} / Cr {book_cr}")

        self.note("A receipt is only trusted once it says which paper it settles: the "
                  "boxes on the form are checked against the customer's own open papers, "
                  "in their own money, before anything is saved, and money that settles "
                  "nothing rests on the account as a credit rather than being invented as "
                  "an exchange gain. A receipt given back is answered by an offsetting "
                  "entry in the receipt's own period — the original stays posted, the "
                  "allocation rows stay, and the invoices take their balance back, right "
                  "down to a paper that was called paid becoming owed again. What is NOT "
                  "here: a customer's overpayment stays in receivables rather than on a "
                  "separate advances account, and there is no screen to send that money "
                  "back out again — a refund of an overpayment would have to be booked "
                  "as a manual journal today.")

    def verify_payroll(self, client):
        """The month's pay is worked out of a table, never typed into a form.

        Basic pay comes from a contract, the rates come from the company's own
        statutory rows, and every figure on a slip has to say which rate it came
        out of and what pay that rate looked at. So a run is proved three ways:
        the arithmetic reproduces from the rows, the payslip contradicts none of
        its own detail, and the whole month leaves the book as one entry that
        charges the pay, owes both halves of every contribution, holds the money
        kept for other people, and pays out only what is left.
        """
        from apps.hr.models import Contract, Employee
        from apps.payroll import services
        from apps.payroll.models import PayrollRun, SalaryComponent, StatutoryRule

        company = self.company
        period_start = self.today.replace(day=1)
        next_month = (period_start.replace(year=period_start.year + 1, month=1)
                      if period_start.month == 12
                      else period_start.replace(month=period_start.month + 1))
        period_end = next_month - datetime.timedelta(days=1)
        following_end = (next_month.replace(year=next_month.year + 1, month=1)
                         if next_month.month == 12
                         else next_month.replace(month=next_month.month + 1))
        following_end -= datetime.timedelta(days=1)
        iso = "%Y-%m-%d"

        # ── A. the book already has the table it needs to pay anybody ──
        home = self.get_ok(client, "payroll home", reverse("payroll:index"))
        codes = [r.code for r in services.rules_as_at(company, self.today)]
        self.expect("the new company arrived with a statutory table",
                    sorted(codes) == ["HOUSING", "NSSF", "PAYE", "SHIF"], str(codes))
        self.expect("the tiered table is read last, after the allowable shares",
                    codes[-1] == "PAYE", str(codes))
        self.expect("and payroll says it has nobody's pay to work from",
                    home.context["staff_count"] == 1 and home.context["unpaid_staff"] == 1,
                    f"{home.context['staff_count']} on the staff list, "
                    f"{home.context['unpaid_staff']} with no contract")
        paye = StatutoryRule.objects.get(company=company, code="PAYE")
        nssf = StatutoryRule.objects.get(company=company, code="NSSF")
        self.expect("the income tax table is five steps and a monthly relief",
                    paye.tax_bands.count() == 5 and paye.monthly_relief == D("2400.00"),
                    f"{paye.tax_bands.count()} steps, relief {paye.monthly_relief}")
        gaps = [code for code in ("2300", "2400", "2500", "2550", "6100", "6110",
                                  "6120", "6130")
                if not company.accounts.filter(code=code, is_active=True).exists()]
        self.expect("every share has an account to be owed from and one to cost",
                    not gaps, str(gaps))

        # ── B. pay is written down before it can be worked out ──
        contract_url = reverse("hr:contract_create", args=[self.employee.pk])
        contract_form = {
            "employee": str(self.employee.pk), "contract_type": "PERMANENT",
            "job_title": "Field Technician", "department": "",
            "start_date": f"{self.today:{iso}}", "end_date": "",
            "basic_salary": "100000.00", "is_current": "on",
        }
        self.post(client, "contract", contract_url, contract_form)
        grace = Contract.objects.filter(employee=self.employee).first()
        if not self.expect("the figure payroll reads is on the employee's contract",
                           grace is not None and grace.is_current):
            return
        self.expect("and it is saved exactly as written",
                    grace.basic_salary == D("100000.00"), str(grace.basic_salary))

        self.reject(client, "a contract that ends before it starts", contract_url,
                    merge(contract_form, {
                        "start_date": f"{period_end + datetime.timedelta(days=30):{iso}}",
                        "end_date": f"{period_end + datetime.timedelta(days=5):{iso}}"}))
        self.reject(client, "a contract that pays nothing", contract_url,
                    merge(contract_form, {"basic_salary": "0"}))
        self.post(client, "a contract starting next month", contract_url,
                  merge(contract_form, {"basic_salary": "110000.00",
                                         "start_date": f"{next_month:{iso}}"}))
        grace.refresh_from_db()
        self.expect("the newer contract took the current flag off the older one",
                    not grace.is_current, str(grace.is_current))
        self.post(client, "put this month's contract back",
                  reverse("hr:contract_update", args=[grace.pk]), contract_form)
        grace.refresh_from_db()
        self.expect("editing a contract moves the flag back with it",
                    grace.is_current, str(grace.is_current))
        self.expect("the contract that superseded it stays as history, unused",
                    Contract.objects.filter(employee=self.employee).count() == 2
                    and Contract.objects.filter(employee=self.employee,
                                                is_current=True).count() == 1)

        hired = datetime.date(self.today.year, 1, 5)
        samuel = Employee.objects.create(
            company=company, employee_number="EMP-002", first_name="Samuel",
            last_name="Okello", status="ACTIVE", date_hired=hired,
            job_title="Warehouse Keeper")
        sam_url = reverse("hr:contract_create", args=[samuel.pk])
        sam_form = {
            "employee": str(samuel.pk), "contract_type": "CONTRACT",
            "job_title": "Warehouse Keeper", "department": "",
            "start_date": f"{hired:{iso}}",
            "end_date": f"{period_start - datetime.timedelta(days=1):{iso}}",
            "basic_salary": "20000.00", "is_current": "on",
        }
        resp = self.reject(client, "a lapsed contract marked current", sam_url, sam_form)
        errs = " ".join(str(e) for row in resp.context["form"].errors.values() for e in row)
        self.expect("and it names the rule the row broke",
                    "current one" in errs, errs)
        self.post(client, "the lapsed contract, honestly ended", sam_url,
                  merge(sam_form, {"is_current": ""}))
        home = client.get(reverse("payroll:index"))
        self.expect("a person whose contract ended is still reported, not skipped quietly",
                    home.context["staff_count"] == 2 and home.context["unpaid_staff"] == 1,
                    f"{home.context['staff_count']} staff, "
                    f"{home.context['unpaid_staff']} unpayable")

        # ── C. the tables police themselves ──
        rule_url = reverse("payroll:rule_create")
        rule_form = {
            "code": "LEVY", "name": "A table", "method": "BANDS", "basis": "GROSS",
            "employee_percent": "0", "employer_percent": "0", "employee_flat": "0",
            "employer_flat": "0", "basis_cap": "", "monthly_relief": "0",
            "reduces_taxable_pay": "", "payable_account": str(self.account("2300").pk),
            "expense_account": "", "effective_from": f"{self.today:{iso}}",
            "is_active": "on",
        }
        resp = self.reject(client, "a second tiered table in the same book", rule_url,
                           merge(rule_form, {"code": "PAYE2", "name": "Second tiers"}))
        errs = " ".join(str(e) for row in resp.context["form"].errors.values() for e in row)
        self.expect("two tiered tables would tax the same pay, so one is refused",
                    "tiered table" in errs, errs)
        self.reject(client, "a table that takes nothing off anybody", rule_url,
                    merge(rule_form, {"code": "IDLE", "name": "Idle levy",
                                      "method": "PERCENT"}))
        resp = self.reject(client, "relief on a plain percentage", rule_url,
                           merge(rule_form, {"code": "IDLE", "name": "Idle levy",
                                              "method": "PERCENT",
                                              "employee_percent": "1",
                                              "monthly_relief": "100"}))
        errs = " ".join(str(e) for row in resp.context["form"].errors.values() for e in row)
        self.expect("a relief belongs to a table of steps, not a rate",
                    "Only a tiered table" in errs, errs)
        self.reject(client, "an employer share with no expense account", rule_url,
                    merge(rule_form, {"code": "SHARE", "name": "Employer scheme",
                                      "method": "PERCENT", "employee_percent": "1",
                                      "employer_percent": "1"}))
        self.reject(client, "a tax step that overlaps the one below it",
                    reverse("payroll:band_create", args=[paye.pk]),
                    {"lower": "20000", "upper": "30000", "percent": "30"})
        self.post(client, "a fixed monthly levy", rule_url, merge(rule_form, {
            "code": "TECH", "name": "Technology levy", "method": "FLAT",
            "employee_flat": "200", "payable_account": str(self.account("2600").pk)}))
        tech = StatutoryRule.objects.filter(company=company, code="TECH").first()
        if not self.expect("a rate can be a flat monthly figure",
                           tech is not None and tech.method == "FLAT"):
            return
        self.post(client, "retire the levy again",
                  reverse("payroll:rule_toggle", args=[tech.pk]), {})
        self.expect("a retired table is not in force",
                    "TECH" not in [r.code for r in
                                   services.rules_as_at(company, self.today)],
                    str([r.code for r in services.rules_as_at(company, self.today)]))

        # ── D. pay elements: the earnings and deductions that are not the law ──
        comp_url = reverse("payroll:component_create")
        house_form = {
            "employee": "", "name": "House allowance", "component_type": "ALLOWANCE",
            "calculation": "FIXED", "amount": "15000.00", "percent": "0",
            "is_taxable": "on", "gl_account": str(self.account("6100").pk),
            "is_active": "on",
        }
        overtime_form = {
            "employee": str(self.employee.pk), "name": "Field overtime",
            "component_type": "OVERTIME", "calculation": "PERCENT_OF_BASIC",
            "amount": "0", "percent": "5", "is_taxable": "on", "gl_account": "",
            "is_active": "on",
        }
        union_form = {
            "employee": str(self.employee.pk), "name": "Union subscription",
            "component_type": "DEDUCTION", "calculation": "FIXED",
            "amount": "500.00", "percent": "0", "is_taxable": "",
            "gl_account": str(self.account("2600").pk), "is_active": "on",
        }
        bond_form = {
            "employee": str(self.employee.pk), "name": "Bond repayment",
            "component_type": "DEDUCTION", "calculation": "FIXED",
            "amount": "500000.00", "percent": "0", "is_taxable": "",
            "gl_account": str(self.account("2700").pk), "is_active": "on",
        }
        self.reject(client, "a fixed element with no figure in it", comp_url,
                    merge(house_form, {"name": "Stand-by", "amount": "0"}))
        self.reject(client, "a percentage element with no percentage", comp_url,
                    merge(overtime_form, {"name": "Site allowance", "percent": "0"}))
        resp = self.reject(client, "withheld money that owes nobody", comp_url,
                           merge(union_form, {"gl_account": ""}))
        errs = " ".join(str(e) for row in resp.context["form"].errors.values() for e in row)
        self.expect("money held for a union needs the account it is held in",
                    "held for somebody else" in errs, errs)
        for name, payload in (("house allowance", house_form),
                              ("overtime off basic pay", overtime_form),
                              ("union subscription", union_form),
                              ("a bond repayment bigger than the pay", bond_form)):
            self.post(client, name, comp_url, payload)

        # ── E. the run: refuse what cannot be paid, then work it out ──
        run_form = {
            "reference": "PR-OCT", "period_start": f"{period_start:{iso}}",
            "period_end": f"{period_end:{iso}}", "payment_date": f"{self.today:{iso}}",
            "salary_account": "", "paid_into": "", "notes": "October pay",
        }
        resp = self.post(client, "payroll run", reverse("payroll:run_create"), run_form)
        run = PayrollRun.objects.filter(company=company, reference="PR-OCT").first()
        if not self.expect("a run is raised for the month", run is not None):
            return
        said = " | ".join(str(m) for m in self.flashes(client, resp))
        self.expect("a slip that would hand out less than nothing is refused by name",
                    "more than their pay" in said and "Grace Rider" in said, said)
        run.refresh_from_db()
        self.expect("and the run is left as an honest empty draft rather than a guess",
                    run.status == "DRAFT" and run.total_net == Z
                    and not run.payslips.exists(),
                    f"{run.status}, net {run.total_net}, {run.payslips.count()} slip(s)")

        bond = SalaryComponent.objects.get(company=company, name="Bond repayment")
        self.post(client, "retire the bond repayment",
                  reverse("payroll:component_update", args=[bond.pk]),
                  merge(bond_form, {"is_active": ""}))
        resp = self.post(client, "recalculate",
                         reverse("payroll:run_calculate", args=[run.pk]), {})
        said = " | ".join(str(m) for m in self.flashes(client, resp))
        self.expect("the month's figures come out of the tables",
                    "83010.65 to pay" in said, said)
        self.expect("and the person with no live contract is named as left off",
                    "Samuel Okello" in said, said)
        run.refresh_from_db()
        self.expect("the run's totals are the payslips' own",
                    run.total_gross == D("120000.00")
                    and run.total_deductions == D("36989.35")
                    and run.total_net == D("83010.65")
                    and run.total_employer == D("8280.00"),
                    f"gross {run.total_gross}, withheld {run.total_deductions}, "
                    f"net {run.total_net}, employer {run.total_employer}")
        self.expect("what the month cost is the pay plus the employer's own shares",
                    run.total_cost == D("128280.00"), str(run.total_cost))

        slip = run.payslips.get(employee=self.employee)
        self.expect("the contract pay, the fixed allowance and the share of basic "
                    "make one gross",
                    slip.gross_salary == D("120000.00")
                    and slip.total_allowances == D("20000.00"),
                    f"gross {slip.gross_salary}, the rest of it allowances "
                    f"{slip.total_allowances}")
        self.expect("taxable pay is the gross less the pension, health and housing shares the law allows off",
                    slip.taxable_pay == D("108420.00"), str(slip.taxable_pay))
        self.expect("and the take-home is the gross less everything withheld",
                    slip.net_salary == D("83010.65")
                    and slip.net_salary == slip.gross_salary - slip.total_deductions,
                    str(slip.net_salary))
        pension = slip.lines.get(code="NSSF", kind="DEDUCTION")
        self.expect("the pension line shows the ceiling it was charged under",
                    pension.basis == D("108000.00")
                    and pension.percent == D("6.000000")
                    and pension.amount == D("6480.00") and pension.reduces_tax,
                    f"{pension.percent}% of {pension.basis} = {pension.amount}")
        health = slip.lines.get(code="SHIF", kind="DEDUCTION")
        self.expect("a health share is charged on the whole of the pay, and comes off it before tax",
                    health.basis == D("120000.00") and health.reduces_tax,
                    f"{health.percent}% of {health.basis}")
        tiers = slip.lines.get(code="PAYE")
        self.expect("income tax names the pay the table was read off and the "
                    "figure after relief",
                    tiers.basis == D("108420.00") and tiers.amount == D("24909.35"),
                    f"{tiers.basis} → {tiers.amount}")
        self.expect("the employer's half of each shared contribution is on the slip too, "
                    "at its own rate",
                    slip.lines.filter(kind="EMPLOYER").count() == 2
                    and slip.employer_cost == D("8280.00"), str(slip.employer_cost))
        overtime = slip.lines.get(description="Field overtime")
        self.expect("a percentage pay element shows the pay it was measured on",
                    overtime.percent == D("5.000000") and overtime.basis == D("100000.00"),
                    f"{overtime.percent}% of {overtime.basis}")
        self.expect("a retired table takes nothing from anybody",
                    not slip.lines.filter(code="TECH").exists())
        self.expect("and a retired pay element is off the slip as well",
                    not slip.lines.filter(description="Bond repayment").exists())

        steps = [{"lower": f"{band.lower}",
                  "upper": "" if band.upper is None else f"{band.upper}",
                  "percent": f"{band.percent}"} for band in paye.bands()]
        tax_before = paye.tax_on(D("1000000"))
        for band in list(paye.tax_bands.all()):
            self.post(client, f"take out the step from {band.lower}",
                      reverse("payroll:band_delete", args=[band.pk]), {})
        self.expect("the table's steps are the only thing the tax came from",
                    not paye.tax_bands.exists() and paye.tax_on(D("1000000")) == Z)
        resp = client.post(reverse("payroll:run_calculate", args=[run.pk]), {})
        said = " | ".join(str(m) for m in self.flashes(client, resp))
        self.expect("a tiered table with no steps refuses to tax anybody",
                    "no bands" in said, said)
        run.refresh_from_db()
        self.expect("and the draft keeps the figures it had rather than losing them",
                    run.total_net == D("83010.65"), str(run.total_net))
        for step in steps:
            self.post(client, "put a step back",
                      reverse("payroll:band_create", args=[paye.pk]), step)
        self.expect("the same five steps went back in and read the same tax",
                    paye.tax_bands.count() == 5
                    and paye.tax_on(D("1000000")) == tax_before,
                    f"{paye.tax_bands.count()} steps, {paye.tax_on(D('1000000'))} "
                    f"against {tax_before}")

        # ── F. approval comes before the book ──
        resp = client.post(reverse("payroll:run_post", args=[run.pk]), {})
        self.expect_flash(client, resp, "approved",
                          "pay does not reach the ledger before somebody has approved it")
        self.post(client, "approve the run",
                  reverse("payroll:run_approve", args=[run.pk]), {})
        run.refresh_from_db()
        self.expect("approval says whose decision it was and when",
                    run.status == "APPROVED" and run.approved_by == self.user
                    and run.approved_at is not None,
                    f"{run.status} by {run.approved_by}")
        resp = client.post(reverse("payroll:run_calculate", args=[run.pk]), {})
        self.expect_flash(client, resp, "settled",
                          "an approved run is not a draft any more")
        self.expect("the table that paid an approved run cannot be edited",
                    client.get(reverse("payroll:rule_update", args=[nssf.pk])).status_code
                    == 404)
        self.reject(client, "a second run for the same month",
                    reverse("payroll:run_create"),
                    merge(run_form, {"reference": "PR-OCT-AGAIN"}))
        self.reject(client, "a second run overlapping part of the month",
                    reverse("payroll:run_create"),
                    merge(run_form, {"reference": "PR-OCT-HALF",
                                     "period_start": f"{run.period_start.replace(day=15):%Y-%m-%d}"}))

        # ── G. one journal for the whole month ──
        before = {code: self.account(code).current_balance for code in
                  ("1200", "2300", "2400", "2500", "2550", "2600", "6100", "6110",
                   "6120", "6130")}
        self.post(client, "post the run", reverse("payroll:run_post", args=[run.pk]), {})
        run.refresh_from_db()
        entry = run.journal_entry
        if not self.expect("the month is in the ledger",
                           entry is not None and run.status == "POSTED", run.status):
            return
        self.expect("one entry for the whole run, typed as payroll",
                    entry.entry_type == "PAYROLL" and entry.status == "POSTED"
                    and entry.reference.startswith("JE-PR-"), entry.reference)
        self.expect("dated the day the money leaves, not the day it was booked",
                    entry.posting_date == run.payment_date
                    and entry.period_id is not None,
                    f"{entry.posting_date}, period {entry.period_id}")
        dr, cr, ok = self.balanced(entry)
        self.expect("the entry balances, and what it charges is what it owes",
                    ok and dr == D("128280.00"), f"Dr {dr} / Cr {cr}")
        self.expect("the three earnings are one charge to salaries, not three lines",
                    entry.lines.filter(account__code="6100", debit_amount__gt=0).count()
                    == 1 and self.line_total(entry, "6100", "debit") == D("120000.00"),
                    f"{entry.lines.filter(account__code='6100').count()} line(s) on 6100")
        for code, want in (("6110", "6480.00"), ("6120", "0"), ("6130", "1800.00")):
            self.expect(f"{code} took the employer's own share as a cost",
                        self.line_total(entry, code, "debit") == D(want),
                        str(self.line_total(entry, code, "debit")))
        for code, want in (("2300", "24909.35"), ("2400", "12960.00"),
                           ("2500", "3300.00"), ("2550", "3600.00"), ("2600", "500.00")):
            self.expect(f"{code} is owed both halves, or the whole of the withholding",
                        self.line_total(entry, code, "credit") == D(want),
                        str(self.line_total(entry, code, "credit")))
        self.expect("only the net pay leaves the bank",
                    self.line_total(entry, "1200", "credit") == D("83010.65")
                    and not entry.lines.filter(account__code="1100").exists())
        owed = {code: self.account(code).current_balance - before[code] for code in
                ("2300", "2400", "2500", "2550", "2600")}
        self.expect("the authorities are owed exactly the figures on the slips",
                    owed == {"2300": D("24909.35"), "2400": D("12960.00"),
                             "2500": D("3300.00"), "2550": D("3600.00"),
                             "2600": D("500.00")}, str(owed))
        self.expect("the bank gave out the take-home and the salary expense is the pay",
                    self.account("1200").current_balance - before["1200"] == D("-83010.65")
                    and self.account("6100").current_balance - before["6100"]
                    == D("120000.00"),
                    f"bank {self.account('1200').current_balance - before['1200']}, "
                    f"6100 {self.account('6100').current_balance - before['6100']}")
        resp = client.post(reverse("payroll:run_post", args=[run.pk]), {})
        self.expect_flash(client, resp, "already in the ledger", "a run is booked once")

        # ── H. a month put right by reversal, not by editing ──
        self.post(client, "cancel the run", reverse("payroll:run_cancel", args=[run.pk]),
                  {"reason": "paid again through the bank's own payroll file"})
        run.refresh_from_db()
        entry.refresh_from_db()  # the reversal was written by the view, on its own copy
        back = entry.reversed_by
        if not self.expect("the posted month is answered by an offsetting entry",
                           run.status == "CANCELLED" and back is not None,
                           f"{run.status}, reversal {back}"):
            return
        self.expect("the original stays in the book as what was decided",
                    entry.status == "POSTED" and entry.reversed_by_id == back.pk)
        dr, cr, ok = self.balanced(back)
        self.expect("the reversal runs the whole month backwards to the last cent",
                    back.entry_type == "REVERSAL"
                    and back.reference.startswith("JE-REV-")
                    and ok and dr == D("128280.00"), f"{back.reference}, Dr {dr}")
        self.expect("and it puts salaries, the bank and each authority back",
                    self.line_total(back, "6100", "credit") == D("120000.00")
                    and self.line_total(back, "1200", "debit") == D("83010.65")
                    and self.line_total(back, "2300", "debit") == D("24909.35"))
        self.expect("booked in the pay month it is unwinding",
                    back.posting_date == entry.posting_date
                    and back.period_id == entry.period_id,
                    f"{back.posting_date}, period {back.period_id}")
        self.expect("the reason the month was stood down is written on the entry",
                    "payroll file" in (back.notes or ""), (back.notes or "")[:80])
        after = {code: self.account(code).current_balance for code in before}
        self.expect("every account is back where the month left it",
                    after == before,
                    str({code: (before[code], after[code]) for code in before
                         if before[code] != after[code]}))
        self.expect("the cancelled run keeps its journal as the record of what happened",
                    run.journal_entry_id == entry.pk)
        resp = client.post(reverse("payroll:run_cancel", args=[run.pk]), {})
        self.expect_flash(client, resp, "already cancelled", "a run is stood down once")
        book_dr, book_cr = self.book_totals()
        self.expect("the book balances to itself after pay in and pay back out",
                    book_dr == book_cr, f"Dr {book_dr} / Cr {book_cr}")

        # ── I. the next month, with a contract come back and an element retired ──
        sam_contract = Contract.objects.get(employee=samuel)
        self.post(client, "bring Samuel's contract back into force",
                  reverse("hr:contract_update", args=[sam_contract.pk]),
                  merge(sam_form, {"end_date": "", "is_current": "on"}))
        union = SalaryComponent.objects.get(company=company, name="Union subscription")
        self.post(client, "retire the union subscription",
                  reverse("payroll:component_update", args=[union.pk]),
                  merge(union_form, {"is_active": ""}))
        resp = self.post(client, "the following month", reverse("payroll:run_create"), {
            "reference": "PR-NEXT", "period_start": f"{next_month:{iso}}",
            "period_end": f"{following_end:{iso}}",
            "payment_date": f"{following_end:{iso}}",
            "salary_account": "", "paid_into": "", "notes": ""})
        said = " | ".join(str(m) for m in self.flashes(client, resp))
        run2 = PayrollRun.objects.filter(company=company, reference="PR-NEXT").first()
        if not self.expect("the next month is worked out from the same tables",
                           run2 is not None):
            return
        self.expect("the person whose contract came back is on it",
                    run2.payslips.count() == 2, f"{run2.payslips.count()} slip(s)")
        self.expect("and nobody is named as left off this time",
                    "no contract covering" not in said, said)
        grace2 = run2.payslips.get(employee=self.employee)
        sam2 = run2.payslips.get(employee=samuel)
        self.expect("retiring a pay element gives the money back to the slip",
                    grace2.net_salary == D("83510.65")
                    and not grace2.lines.filter(description="Union subscription").exists(),
                    str(grace2.net_salary))
        self.expect("the pension ceiling only bites above the pay it caps",
                    sam2.lines.get(code="NSSF", kind="DEDUCTION").basis
                    == D("35000.00") and sam2.gross_salary == D("35000.00"),
                    f"basis {sam2.lines.get(code='NSSF', kind='DEDUCTION').basis} "
                    f"on gross {sam2.gross_salary}")
        self.expect("and a small pay still walks through the same three steps, "
                    "then loses the relief",
                    sam2.taxable_pay == D("31412.50")
                    and sam2.lines.get(code="PAYE").amount == D("1853.13"),
                    f"taxable {sam2.taxable_pay}, PAYE "
                    f"{sam2.lines.get(code='PAYE').amount}")
        self.expect("the run's totals are nothing but its slips added up",
                    run2.total_gross == grace2.gross_salary + sam2.gross_salary
                    and run2.total_deductions
                    == grace2.total_deductions + sam2.total_deductions
                    and run2.total_net == grace2.net_salary + sam2.net_salary
                    and run2.total_employer
                    == grace2.employer_cost + sam2.employer_cost,
                    f"net {run2.total_net} against "
                    f"{grace2.net_salary + sam2.net_salary}")
        self.expect("and the draft of next month has not touched the ledger",
                    run2.journal_entry_id is None and run2.status == "DRAFT")

        # ── J. what the screens say about all of it ──
        for name, url in (
            ("payroll home", reverse("payroll:index")),
            ("the run list", reverse("payroll:run_list")),
            ("the cancelled month", reverse("payroll:run_detail", args=[run.pk])),
            ("the drafted month", reverse("payroll:run_detail", args=[run2.pk])),
            ("the payslip list", reverse("payroll:payslip_list")),
            ("a payslip", reverse("payroll:payslip_detail", args=[slip.pk])),
            ("a second month's payslip",
             reverse("payroll:payslip_detail", args=[sam2.pk])),
            ("the statutory tables", reverse("payroll:rule_list")),
            ("the tiered table", reverse("payroll:rule_detail", args=[paye.pk])),
            ("a flat levy", reverse("payroll:rule_detail", args=[tech.pk])),
            ("the pay elements", reverse("payroll:component_list")),
            ("a new pay element", comp_url),
            ("a new table", rule_url),
            ("a new contract", contract_url),
            ("an existing contract", reverse("hr:contract_update", args=[grace.pk])),
            ("the employee's own record",
             reverse("hr:employee_detail", args=[self.employee.pk])),
            ("the journal the month left",
             reverse("accounting:journal_entry_detail", args=[entry.pk])),
            ("and the entry that answers it",
             reverse("accounting:journal_entry_detail", args=[back.pk])),
            ("the trial balance", reverse("accounting:trial_balance")),
        ):
            self.get_ok(client, name, url)
        page = client.get(reverse("payroll:run_detail", args=[run.pk]))
        self.expect("a month that is settled offers no buttons to change it",
                    page.context["can_edit"] is False)
        page = client.get(reverse("payroll:rule_detail", args=[nssf.pk]))
        self.expect("a table that has paid an approved run says it is frozen",
                    page.context["can_edit"] is False, str(page.context["can_edit"]))
        page = client.get(reverse("payroll:rule_list"))
        self.expect("and the table list marks one row per code as the one in force",
                    sum(1 for r in page.context["rules"] if r.is_in_force) == 4,
                    str([(r.code, r.effective_from, r.is_active, r.is_in_force)
                         for r in page.context["rules"]]))

        self.note("Payroll is the one part of the book where no figure is typed: the "
                  "contract gives the pay, the company's own rows give the rates, and "
                  "each line on a slip carries the rate and the pay it was taken from, "
                  "so a January slip can still be proved in June. A run is approved "
                  "before it is posted, posted as one balanced entry, and put right by "
                  "reversal — never by recalculating a month somebody already signed "
                  "off. What is NOT here: pay is only ever in the book currency, so a "
                  "company with foreign staff has no rate applied to their salary; "
                  "there is no per-department cost split and no payslip correction "
                  "short of cancelling the run; the statutory liabilities built here "
                  "are remitted by manual journal, because no remittance screen exists; "
                  "and a contract row cannot be deleted from the screens once written.")

    def verify_fixed_assets(self, client):
        """The register and the ledger are one asset, written down twice.

        An asset is only a spreadsheet row until the book carries it, so every
        figure here is proved from both sides at once: what the cost accounts, the
        accumulated depreciation account and the year's charge hold has to equal
        what the register says, month after month and disposal after disposal. The
        charge is worked out of the price, the life and the residual rather than
        typed, so the only thing a bookkeeper enters is which month is being caught
        up. And nothing already posted is edited: a wrong month is answered by a
        reversal in that same month, and the register moves back with it.
        """
        from apps.accounts.models import User
        from apps.accounting.models import JournalEntry
        from apps.fixed_assets import services
        from apps.fixed_assets.models import (
            AssetCategory, AssetDepreciation, AssetDisposal, FixedAsset,
        )

        company = self.company
        iso = "%Y-%m-%d"
        this_month = self.today.replace(day=1)

        def back(months):
            """The first day of a month `months` before this one."""
            count = this_month.year * 12 + this_month.month - 1 - months
            return datetime.date(count // 12, count % 12 + 1, 1)

        bought = back(6) + datetime.timedelta(days=4)
        # Six months of wear, all of them lived through: this month itself is
        # deliberately left uncharged so the register still has a month waiting.
        months = [back(6 - i) for i in range(6)]

        def errors_of(response):
            return " ".join(str(e)
                            for row in response.context["form"].errors.values()
                            for e in row)

        # ── A. a category is the bookkeeper's own routing ──
        home = self.get_ok(client, "fixed asset home", reverse("fixed_assets:index"))
        self.expect("the register starts empty",
                    home.context["asset_count"] == 0
                    and home.context["totals"]["gross_value"] is None,
                    f"{home.context['asset_count']} assets")

        category_url = reverse("fixed_assets:category_create")
        category_form = {
            "name": "Computers & Technology", "default_useful_life": "5",
            "default_depreciation_method": "SLM",
            "gl_asset_account": str(self.account("1630").pk),
            "gl_depreciation_account": str(self.account("6900").pk),
            "gl_accumulated_account": str(self.account("1690").pk),
        }
        resp = self.reject(client, "a category that would last no time at all",
                           category_url, merge(category_form, {"default_useful_life": "0"}))
        self.expect("a life of no years has no monthly charge to post",
                    "no monthly charge" in errors_of(resp), errors_of(resp))

        fx = self.fx_company
        fx_user = User.objects.filter(
            memberships__company=fx).first() or self.user
        borrowed = fx.accounts.filter(code="1630").first()
        resp = self.reject(client, "a category borrowing another book's accounts",
                           category_url, merge(category_form, {
                               "name": "Borrowed Ledger",
                               "gl_asset_account": str(borrowed.pk)}))
        self.expect("another company's account is not even a choice on this form",
                    "Select a valid choice" in errors_of(resp), errors_of(resp)[:200])

        self.post(client, "asset category", category_url, category_form)
        computers = AssetCategory.objects.filter(
            company=company, name="Computers & Technology").first()
        if not self.expect("the category is saved with the three accounts it names",
                           computers is not None and computers.gl_asset_account_id
                           and computers.gl_accumulated_account_id):
            return
        resp = self.reject(client, "a second category under the same name",
                           category_url, category_form)
        self.expect("two categories of one name would split the same assets",
                    "already exists" in errors_of(resp), errors_of(resp))

        self.post(client, "vehicle category", category_url, merge(
            category_form, {"name": "Motor Vehicles",
                            "gl_asset_account": str(self.account("1620").pk),
                            "default_useful_life": "4",
                            "default_depreciation_method": "WDV"}))
        vehicles = AssetCategory.objects.filter(
            company=company, name="Motor Vehicles").first()
        self.expect("a category can price its own kind of asset a different way",
                    vehicles is not None and vehicles.default_depreciation_method == "WDV",
                    str(vehicles and vehicles.default_depreciation_method))

        # ── B. saving an asset puts it into the ledger ──
        asset_url = reverse("fixed_assets:asset_create")
        laptop_form = {
            "code": "FA-LAP-01", "name": "Bookkeeper's laptop",
            "category": str(computers.pk), "serial_number": "SN-8842",
            "location": "Nairobi office", "supplier": "",
            "paid_into": "", "warranty_expiry": "", "notes": "",
            "purchase_date": f"{bought:{iso}}", "purchase_cost": "240000.00",
            "residual_value": "40000.00", "useful_life_years": "5",
            "depreciation_method": "SLM", "status": "ACTIVE",
        }
        self.post(client, "laptop", asset_url, laptop_form)
        laptop = FixedAsset.objects.filter(company=company, code="FA-LAP-01").first()
        if not self.expect("the asset is on the register at the figures typed",
                           laptop is not None
                           and laptop.purchase_cost == D("240000.00")
                           and laptop.book_value == D("240000.00")):
            return
        acq = laptop.journal_entry
        if not self.expect("and it is in the ledger already, without a second screen",
                           acq is not None and acq.status == "POSTED"):
            return
        self.expect("the acquisition entry is typed ASSET and dated the day it was bought",
                    acq.entry_type == "ASSET" and acq.posting_date == bought,
                    f"{acq.entry_type} on {acq.posting_date}")
        self.expect("it points back at the register row it came from",
                    acq.source_document_type == "FixedAsset"
                    and acq.source_document_id == str(laptop.pk),
                    f"{acq.source_document_type} {acq.source_document_id}")
        self.expect("the cost account took the asset and the book's own bank paid for it",
                    self.line_total(acq, "1630", "debit") == D("240000.00")
                    and self.line_total(acq, "1200", "credit") == D("240000.00")
                    and acq.lines.count() == 2,
                    str([(l.account.code, str(l.debit_amount), str(l.credit_amount))
                         for l in acq.lines.all()]))
        self.expect("the entry balances and landed in an open period",
                    acq.is_balanced() and acq.period_id is not None
                    and not acq.calendar_warning, acq.calendar_warning)
        self.expect("the accounts now hold the cost the register does",
                    self.account("1630").current_balance == D("240000.00"),
                    str(self.account("1630").current_balance))
        again = services.post_acquisition(laptop, user=self.user)
        self.expect("saving the same asset twice does not buy it twice",
                    again.pk == acq.pk, f"{again.reference} vs {acq.reference}")

        # ── C. the charge is worked out, month by month ──
        detail = self.get_ok(client, "asset detail",
                             reverse("fixed_assets:asset_detail", args=[laptop.pk]))
        self.expect("the screen offers the first month the asset wore out in",
                    detail.context["next_period"] == months[0],
                    str(detail.context["next_period"]))
        self.expect("and shows the two lines that month would move before it is posted",
                    [(side, account.code, amount)
                     for side, account, amount in detail.context["preview"]]
                    == [("Dr", "6900", D("3333.33")), ("Cr", "1690", D("3333.33"))],
                    str(detail.context["preview"]))
        self.expect("the monthly figure comes out of the price, the residual and the life",
                    services.next_charge_amount(laptop) == D("3333.33"),
                    str(laptop.monthly_charge))

        run_url = reverse("fixed_assets:asset_depreciate", args=[laptop.pk])
        charge = {f"period_date": f"{months[0]:{iso}}"}
        resp = self.post(client, "first month", run_url, charge)
        self.expect_flash(client, resp, "charged for", "the month's wear is announced")
        jan = AssetDepreciation.objects.get(asset=laptop, period_date=months[0])
        self.expect("the charge is what the figures give, to the cent",
                    jan.depreciation_amount == D("3333.33"), str(jan.depreciation_amount))
        self.expect("the register's memory of the month matches the entry beside it",
                    jan.journal_entry.status == "POSTED"
                    and jan.journal_entry.entry_type == "DEPRECIATION"
                    and self.line_total(jan.journal_entry, "6900", "debit") == D("3333.33")
                    and self.line_total(jan.journal_entry, "1690", "credit") == D("3333.33"),
                    str([(l.account.code, str(l.debit_amount), str(l.credit_amount))
                         for l in jan.journal_entry.lines.all()]))
        laptop.refresh_from_db()
        self.expect("the book value came down by exactly that charge",
                    laptop.accumulated_depreciation == D("3333.33")
                    and laptop.book_value == D("236666.67"),
                    f"{laptop.accumulated_depreciation} / {laptop.book_value}")
        self.expect("accumulated depreciation reads negative — a contra asset",
                    self.account("1690").current_balance == D("-3333.33")
                    and self.account("6900").current_balance == D("3333.33"),
                    f"1690 {self.account('1690').current_balance}, "
                    f"6900 {self.account('6900').current_balance}")

        resp = self.reject(client, "the same month twice", run_url, charge)
        self.expect("one month is never charged twice",
                    "already been charged" in errors_of(resp), errors_of(resp))
        resp = self.reject(client, "a month before the asset arrived", run_url,
                           {"period_date": f"{back(7):{iso}}"})
        self.expect("a thing not yet bought cannot have worn down",
                    "cannot have worn down" in errors_of(resp), errors_of(resp))
        resp = self.reject(client, "a month that has not come round", run_url,
                           {"period_date": f"{back(-1):{iso}}"})
        self.expect("wear is charged once the month has been lived through",
                    "has not come round" in errors_of(resp), errors_of(resp))

        self.post(client, "second month", run_url,
                  {"period_date": f"{months[1]:{iso}}"})
        self.post(client, "third month", run_url,
                  {"period_date": f"{months[2]:{iso}}"})
        laptop.refresh_from_db()
        self.expect("three months of wear stand in the book",
                    laptop.accumulated_depreciation == D("9999.99"),
                    str(laptop.accumulated_depreciation))

        # ── D. a wrong month is answered by a reversal, then charged again ──
        march = AssetDepreciation.objects.get(asset=laptop, period_date=months[2])
        resp = self.post(client, "stand the month down",
                         reverse("fixed_assets:depreciation_reverse", args=[march.pk]),
                         {"reason": "typed the wrong month"})
        self.expect_flash(client, resp, "stood down", "the reversal is announced")
        laptop.refresh_from_db()
        self.expect("the register went back by exactly the charge",
                    laptop.accumulated_depreciation == D("6666.66")
                    and laptop.last_depreciated == months[1],
                    f"{laptop.accumulated_depreciation} through {laptop.last_depreciated}")
        self.expect("and the wear account gave the month back too",
                    self.account("1690").current_balance == D("-6666.66")
                    and self.account("6900").current_balance == D("6666.66"),
                    f"1690 {self.account('1690').current_balance}")
        reversal = march.journal_entry.reversed_by
        self.expect("the answer is a second entry, not an edited one",
                    reversal.entry_type == "REVERSAL" and reversal.status == "POSTED"
                    and reversal.posting_date == months[2]
                    and march.journal_entry.status == "POSTED",
                    f"{reversal.reference} {reversal.entry_type} on {reversal.posting_date}")
        self.expect("and it carries the reason on its face",
                    "wrong month" in reversal.notes, reversal.notes)
        resp = self.post(client, "stand the same month down again",
                         reverse("fixed_assets:depreciation_reverse", args=[march.pk]),
                         {"reason": ""})
        self.expect_flash(client, resp, "Could not", "one month is only stood down once")

        # A month two on from the last charge is a gap, and gaps are allowed:
        # catching up is ordinary work, going backwards is not.
        self.post(client, "a later month, skipping one", run_url,
                  {"period_date": f"{months[4]:{iso}}"})
        resp = self.reject(client, "a month behind the last charge", run_url,
                           {"period_date": f"{months[3]:{iso}}"})
        self.expect("depreciation runs forward through the calendar",
                    "runs forward" in errors_of(resp), errors_of(resp))
        resp = self.post(client, "stand the skipped-ahead month down",
                         reverse("fixed_assets:depreciation_reverse",
                                 args=[AssetDepreciation.objects.get(
                                     asset=laptop, period_date=months[4]).pk]),
                         {"reason": "make way for the month before it"})
        self.expect_flash(client, resp, "stood down", "and the way back is open")
        resp = self.post(client, "charge the month again", run_url,
                         {"period_date": f"{months[2]:{iso}}"})
        self.expect_flash(client, resp, "charged for", "a stood-down month re-charges")
        self.expect("the register keeps one row per month and points it at the new entry",
                    AssetDepreciation.objects.filter(
                        asset=laptop, period_date=months[2]).count() == 1
                    and AssetDepreciation.objects.get(
                        asset=laptop, period_date=months[2]).journal_entry.pk
                    != march.journal_entry.pk,
                    str(AssetDepreciation.objects.filter(
                        asset=laptop, period_date=months[2]).values_list(
                        "journal_entry_id", flat=True)))

        # ── E. what the register refuses to carry ──
        bad = merge(laptop_form, {
            "code": "FA-BAD-01", "name": "Guard probe", "category": "",
            "purchase_cost": "50000.00", "residual_value": "0",
            "useful_life_years": "3", "depreciation_method": "SLM"})
        for label, data, needle in (
            ("is expected to be worth more than it cost",
             merge(bad, {"residual_value": "60000.00"}), "nothing here to write off"),
            ("has no years of life",
             merge(bad, {"useful_life_years": "0"}), "no monthly charge"),
            ("has no price at all",
             merge(bad, {"purchase_cost": "0"}), "no purchase cost"),
            ("whose warranty ended before it was bought",
             merge(bad, {"warranty_expiry": f"{bought - datetime.timedelta(days=30):{iso}}"}),
             "ends before the asset was bought"),
            ("is already on the register under another name",
             merge(bad, {"code": "fa-lap-01"}), "already on the register"),
        ):
            resp = self.reject(client, f"an asset that {label}", asset_url, data)
            self.expect(f"an asset that {label} is refused, and told why",
                        needle in errors_of(resp), errors_of(resp)[:200])
        self.expect("nothing was written for the assets that were refused",
                    not FixedAsset.objects.filter(
                        company=company, code__startswith="FA-BAD").exists()
                    and not AssetDepreciation.objects.filter(asset__in=[laptop],
                                                              depreciation_amount=Z).exists())

        # ── F. the rest of the register: reducing balances, a floor, a desk ──
        vehicle_form = merge(laptop_form, {
            "code": "FA-VEH-01", "name": "Pickup", "category": str(vehicles.pk),
            "purchase_cost": "1200000.00", "residual_value": "200000.00",
            "useful_life_years": "4", "depreciation_method": "WDV",
            "paid_into": str(self.account("1100").pk)})
        self.post(client, "vehicle", asset_url, vehicle_form)
        vehicle = FixedAsset.objects.get(company=company, code="FA-VEH-01")
        self.expect("a paid-into account is honoured, so the drawer that gave "
                    "the money can be named",
                    self.line_total(vehicle.journal_entry, "1100", "credit")
                    == D("1200000.00"),
                    str([(l.account.code, str(l.credit_amount))
                         for l in vehicle.journal_entry.lines.all()]))
        self.expect("a written-down charge falls on the value the asset holds now",
                    services.next_charge_amount(vehicle) == D("25000.00"),
                    str(vehicle.monthly_charge))

        # A cost so small and a residual so high that the floor arrives inside
        # two charges — the only way to watch the cap bite.
        cap_form = merge(laptop_form, {
            "code": "FA-CAP-01", "name": "Hand drill", "category": "",
            "purchase_cost": "1000.00", "residual_value": "900.00",
            "useful_life_years": "1", "depreciation_method": "WDV"})
        self.post(client, "hand drill", asset_url, cap_form)
        cap = FixedAsset.objects.get(company=company, code="FA-CAP-01")
        self.expect("with no category the asset lands on the chart's own cost account",
                    self.line_total(cap.journal_entry, "1610", "debit") == D("1000.00"),
                    str([(l.account.code, str(l.debit_amount))
                         for l in cap.journal_entry.lines.all()]))

        desk_form = merge(laptop_form, {
            "code": "FA-DSK-01", "name": "Office desk", "category": "",
            "purchase_cost": "60000.00", "residual_value": "0",
            "useful_life_years": "5", "depreciation_method": "SLM"})
        self.post(client, "desk", asset_url, desk_form)
        desk = FixedAsset.objects.get(company=company, code="FA-DSK-01")

        # ── G. an unbuy ──
        resp = self.post(client, "stand a depreciated asset's purchase down",
                         reverse("fixed_assets:acquisition_reverse", args=[laptop.pk]),
                         {"reason": ""})
        self.expect("a purchase that charges were worked out of cannot be unbuyed "
                    "in front of them",
                    any("charged depreciation through" in str(m)
                        for m in self.flashes(client, resp)),
                    str(self.flashes(client, resp)))
        self.expect("and the refusal left the cost where it was",
                    self.account("1630").current_balance == D("240000.00"),
                    str(self.account("1630").current_balance))

        cost_before = self.account("1610").current_balance
        desk_entry = desk.journal_entry
        resp = self.post(client, "stand the desk's purchase down",
                         reverse("fixed_assets:acquisition_reverse", args=[desk.pk]),
                         {"reason": "the supplier took it back"})
        self.expect_flash(client, resp, "stood down through", "the unbuy is announced")
        self.expect("the cost account gave the desk back up again",
                    self.account("1610").current_balance == cost_before - D("60000.00"),
                    f"1610 {self.account('1610').current_balance} was {cost_before}")
        desk.refresh_from_db()
        self.expect("the asset stays on the register, marked as unposted in the book",
                    desk.journal_entry_id == desk_entry.pk
                    and desk.journal_entry.reversed_by_id is not None)
        resp = self.post(client, "save the desk again",
                         reverse("fixed_assets:asset_edit", args=[desk.pk]),
                         merge(desk_form, {"code": "FA-DSK-01"}))
        self.expect_flash(client, resp, "posted to the ledger again",
                          "the save says a fresh entry was made, not nothing")
        desk.refresh_from_db()
        self.expect("and the new entry is a different one from the stood-down original",
                    desk.journal_entry.pk != desk_entry.pk
                    and self.account("1610").current_balance == cost_before,
                    f"{desk.journal_entry.reference}, 1610 "
                    f"{self.account('1610').current_balance}")

        # ── H. the whole register, one month at a time ──
        run_all_url = reverse("fixed_assets:depreciation_run")
        screen = self.get_ok(client, "depreciation run", run_all_url)
        self.expect("the run names every asset that has a month waiting",
                    screen.context["due_count"] == 4,
                    f"{screen.context['due_count']} due, {screen.context['asset_count']} active")
        self.expect("and totals the charge it would make before being asked",
                    screen.context["due_total"] == D("3333.33") + D("25000.00")
                    + D("83.33") + D("1000.00"), str(screen.context["due_total"]))

        for index, want_charged, want_skipped in ((3, 4, 0), (4, 4, 0), (5, 3, 1)):
            resp = self.post(client, f"the month {months[index]:%b %Y}", run_all_url,
                             {"period_date": f"{months[index]:{iso}}"})
            messages_ = self.flashes(client, resp)
            text = " ".join(str(m) for m in messages_)
            self.expect(f"{want_charged} asset(s) charged for {months[index]:%b %Y}",
                        f"{want_charged} asset(s) charged" in text, text)
            if want_skipped:
                self.expect("the one that could not be charged is reported, "
                            "not allowed to abort the run",
                            f"{want_skipped} asset(s) were not charged" in text, text)

        laptop.refresh_from_db()
        vehicle.refresh_from_db()
        cap.refresh_from_db()
        desk.refresh_from_db()
        self.expect("straight line charges the same figure every month",
                    laptop.accumulated_depreciation == D("19999.98")
                    and laptop.book_value == D("220000.02"),
                    f"laptop {laptop.accumulated_depreciation}")
        self.expect("a written-down balance falls each month on what is left",
                    [str(d.depreciation_amount) for d in
                     vehicle.depreciations.order_by("period_date")]
                    == ["25000.00", "24479.17", "23969.18"],
                    str(vehicle.accumulated_depreciation))
        self.expect("and the charge stops on the residual value, however the method works out",
                    cap.accumulated_depreciation == D("100.00")
                    and cap.book_value == D("900.00") and cap.is_fully_depreciated,
                    f"drill {cap.accumulated_depreciation} left {cap.remaining_depreciable}")
        self.expect("a fully worn asset has no month waiting, but the register says so kindly",
                    services.next_charge_date(cap) is None
                    and services.next_charge_date(laptop) == this_month,
                    f"drill {services.next_charge_date(cap)}, laptop "
                    f"{services.next_charge_date(laptop)}")
        page = client.get(reverse("fixed_assets:asset_detail", args=[cap.pk]))
        self.expect("and the asset's own screen tells the difference between the two",
                    "residual value" in str(page.content.decode()),
                    str(page.context["next_period"]))

        # ── I. figures the ledger has already used are locked ──
        page = client.get(reverse("fixed_assets:asset_edit", args=[laptop.pk]))
        self.expect("the cost of an asset the book carries cannot be edited away",
                    page.context["form"].fields["purchase_cost"].disabled
                    and page.context["form"].fields["purchase_date"].disabled,
                    str(page.context["form"].fields["purchase_cost"].disabled))
        resp = self.post(client, "edit the laptop",
                         reverse("fixed_assets:asset_edit", args=[laptop.pk]),
                         merge(laptop_form, {"location": "Mombasa office",
                                             "purchase_cost": "1.00",
                                             "useful_life_years": "1"}))
        self.expect_flash(client, resp, "saved.", "an already-posted asset is only saved")
        laptop.refresh_from_db()
        self.expect("the location moved and the cost and the life did not",
                    laptop.location == "Mombasa office"
                    and laptop.purchase_cost == D("240000.00")
                    and laptop.useful_life_years == 5,
                    f"{laptop.purchase_cost} over {laptop.useful_life_years} years")
        self.expect("and no second purchase was booked for the edit",
                    JournalEntry.objects.filter(
                        company=company, entry_type="ASSET",
                        source_document_id=str(laptop.pk)).count() == 1,
                    str(JournalEntry.objects.filter(
                        company=company, entry_type="ASSET",
                        source_document_id=str(laptop.pk)).count()))

        # ── J. disposals: what came back, and what the book lost ──
        dispose_url = reverse("fixed_assets:asset_dispose", args=[cap.pk])
        self.post(client, "sell the drill", dispose_url, {
            "disposal_date": f"{self.today:{iso}}", "disposal_type": "SALE",
            "proceeds": "1500.00", "received_into": "", "notes": "sold on"})
        cap.refresh_from_db()
        sale = cap.disposal
        self.expect("a disposal is its own journal entry, typed DISPOSAL",
                    sale.journal_entry_id is not None
                    and sale.journal_entry.entry_type == "DISPOSAL"
                    and cap.status == "DISPOSED", cap.status)
        self.expect("it clears the cost and the wear both, and pays the proceeds in",
                    self.line_total(sale.journal_entry, "1610", "credit") == D("1000.00")
                    and self.line_total(sale.journal_entry, "1690", "debit") == D("100.00")
                    and self.line_total(sale.journal_entry, "1200", "debit") == D("1500.00"),
                    str([(l.account.code, str(l.debit_amount), str(l.credit_amount))
                         for l in sale.journal_entry.lines.all()]))
        self.expect("and the excess over the book value is income, not a smaller cost",
                    sale.gain_loss == D("600.00")
                    and self.account("4350").current_balance == D("600.00"),
                    f"gain {sale.gain_loss}, 4350 {self.account('4350').current_balance}")

        laptop_dispose_url = reverse("fixed_assets:asset_dispose", args=[laptop.pk])
        resp = self.reject(client, "a disposal dated before the asset arrived",
                           laptop_dispose_url, {
                               "disposal_date": f"{back(7):{iso}}", "disposal_type": "SALE",
                               "proceeds": "1.00", "received_into": "", "notes": ""})
        self.expect("a thing cannot go before it came",
                    "could not have gone" in errors_of(resp), errors_of(resp))
        resp = self.reject(client, "a disposal dated behind the last charge",
                           laptop_dispose_url, {
                               "disposal_date": f"{months[1]:{iso}}",
                               "disposal_type": "SALE", "proceeds": "1.00",
                               "received_into": "", "notes": ""})
        self.expect("it cannot clear a book value the asset had not reached yet",
                    "charged through" in errors_of(resp), errors_of(resp)[:200])

        self.post(client, "sell the laptop at a loss", laptop_dispose_url, {
            "disposal_date": f"{self.today:{iso}}", "disposal_type": "SALE",
            "proceeds": "50000.00", "received_into": "", "notes": ""})
        laptop.refresh_from_db()
        gone = laptop.disposal
        self.expect("the loss is the book the asset was on, less what came back",
                    gone.gain_loss == D("-170000.02")
                    and self.line_total(gone.journal_entry, "6950", "debit")
                    == D("170000.02"), f"loss {gone.gain_loss}")
        self.expect("the entry balances with the cost taken out whole",
                    self.line_total(gone.journal_entry, "1630", "credit")
                    == D("240000.00") and gone.journal_entry.is_balanced(),
                    f"Dr {gone.journal_entry.total_debit} / Cr "
                    f"{gone.journal_entry.total_credit}")
        resp = client.get(laptop_dispose_url)
        self.expect("an asset already gone offers no second disposal",
                    resp.status_code == 302, f"HTTP {resp.status_code}")
        resp = self.post(client, "bring the laptop back",
                         reverse("fixed_assets:disposal_reverse", args=[laptop.pk]),
                         {"reason": "the buyer returned it"})
        self.expect_flash(client, resp, "active again", "the asset comes back")
        laptop.refresh_from_db()
        self.expect("the reversal puts the wear and the cost back where they stood",
                    laptop.status == "ACTIVE"
                    and laptop.accumulated_depreciation == D("19999.98")
                    and self.account("1630").current_balance == D("240000.00")
                    and self.account("6950").current_balance == Z,
                    f"{laptop.status}, 6950 {self.account('6950').current_balance}")
        self.expect("and the register is ready to charge it again",
                    services.next_charge_date(laptop) == this_month,
                    str(services.next_charge_date(laptop)))

        self.post(client, "dispose of it a second time", laptop_dispose_url, {
            "disposal_date": f"{self.today:{iso}}", "disposal_type": "SALE",
            "proceeds": "60000.00", "received_into": "", "notes": "sold again"})
        laptop.refresh_from_db()
        self.expect("one asset keeps one disposal row, pointed at the newer entry",
                    AssetDisposal.objects.filter(asset=laptop).count() == 1
                    and laptop.disposal.journal_entry_id != gone.journal_entry_id
                    and laptop.disposal.gain_loss == D("-160000.02"),
                    str(AssetDisposal.objects.filter(asset=laptop).count()))
        first_entry = JournalEntry.objects.get(pk=gone.journal_entry_id)
        self.expect("and the first disposal still stands as the record it was",
                    laptop.disposal.journal_entry.reversed_by_id is None
                    and first_entry.reversed_by_id is not None,
                    f"first {first_entry.reference} reversed by "
                    f"{first_entry.reversed_by_id}")

        self.post(client, "scrap the desk",
                  reverse("fixed_assets:asset_dispose", args=[desk.pk]), {
                      "disposal_date": f"{self.today:{iso}}", "disposal_type": "SCRAP",
                      "proceeds": "0", "received_into": "", "notes": "beyond repair"})
        desk.refresh_from_db()
        self.expect("something scrapped with nothing received moves no cash at all",
                    desk.disposal.journal_entry.lines.count() == 3
                    and not desk.disposal.journal_entry.lines.filter(
                        account__code="1200").exists(),
                    str([(l.account.code, str(l.debit_amount), str(l.credit_amount))
                         for l in desk.disposal.journal_entry.lines.all()]))
        self.expect("and the whole book value of it is a loss",
                    desk.disposal.gain_loss == D("-57000.00"), str(desk.disposal.gain_loss))

        resp = self.post(client, "scrap it again",
                         reverse("fixed_assets:asset_dispose", args=[desk.pk]), {
                             "disposal_date": f"{self.today:{iso}}",
                             "disposal_type": "SCRAP", "proceeds": "0",
                             "received_into": "", "notes": ""})
        self.expect("one asset is only disposed of once",
                    any("nothing left to dispose of" in str(m)
                        for m in self.flashes(client, resp)), str(self.flashes(client, resp)))

        # ── K. the other bookkeeper cannot see any of this ──
        fx_client = Client()
        fx_client.force_login(fx_user, backend="django.contrib.auth.backends.ModelBackend")
        session = fx_client.session
        session["active_company_id"] = str(fx.id)
        session.save()
        for name, url in (
            ("an asset", reverse("fixed_assets:asset_detail", args=[laptop.pk])),
            ("its edit screen", reverse("fixed_assets:asset_edit", args=[vehicle.pk])),
            ("its month", reverse("fixed_assets:asset_depreciate", args=[vehicle.pk])),
            ("its disposal", reverse("fixed_assets:asset_dispose", args=[vehicle.pk])),
        ):
            foreign = fx_client.get(url)
            self.expect(f"another company's fixed asset screen ({name}) is not theirs",
                        foreign.status_code == 404, f"HTTP {foreign.status_code}")
        blocked = fx_client.post(
            reverse("fixed_assets:depreciation_reverse", args=[march.pk]),
            {"reason": ""})
        self.expect("a stood-down charge in another book cannot be stood down again",
                    blocked.status_code == 404, f"HTTP {blocked.status_code}")
        blocked = fx_client.post(
            reverse("fixed_assets:acquisition_reverse", args=[vehicle.pk]),
            {"reason": ""})
        self.expect("nor its purchase unbuyed",
                    blocked.status_code == 404, f"HTTP {blocked.status_code}")
        listed = fx_client.get(reverse("fixed_assets:asset_list"))
        self.expect("their register lists only their own assets",
                    not (listed.context["assets"]
                         & FixedAsset.objects.filter(company=company)),
                    f"{listed.context['assets'].count()} rows")
        self.expect("and their own categories are not offered to us",
                    client.get(reverse("fixed_assets:asset_create")).context["form"]
                    .fields["category"].queryset.filter(company=fx).count() == 0)

        # ── L. the two sides of the register are the same figures ──
        on_register = [a for a in FixedAsset.objects.filter(company=company)
                       if a.status != "DISPOSED"]
        charged = sum((d.depreciation_amount for d in AssetDepreciation.objects.filter(
            asset__company=company).exclude(
            journal_entry__reversed_by__isnull=False)), Z)
        carried = sum((a.accumulated_depreciation for a in on_register), Z)
        cost_on_book = sum((self.account(code).current_balance
                            for code in ("1610", "1620", "1630")), Z)
        self.expect("the depreciation expense is every charge still standing",
                    self.account("6900").current_balance == charged,
                    f"6900 {self.account('6900').current_balance} vs {charged}")
        self.expect("the contra asset holds the wear the register still carries",
                    -self.account("1690").current_balance == carried,
                    f"1690 {self.account('1690').current_balance} vs {carried}")
        self.expect("the cost accounts hold the cost of the assets still on the register",
                    cost_on_book == sum((a.purchase_cost for a in on_register), Z),
                    f"cost accounts {cost_on_book}, register "
                    f"{sum((a.purchase_cost for a in on_register), Z)}")
        self.expect("which at book value is that cost less that wear",
                    sum((a.book_value for a in on_register), Z)
                    == cost_on_book + self.account("1690").current_balance,
                    f"register {sum((a.book_value for a in on_register), Z)}, ledger "
                    f"{cost_on_book + self.account('1690').current_balance}")
        self.expect("nothing is left in the disposal accounts but the answer to a mistake",
                    self.account("6950").current_balance == D("217000.02")
                    and self.account("4350").current_balance == D("600.00"),
                    f"6950 {self.account('6950').current_balance}, "
                    f"4350 {self.account('4350').current_balance}")
        dr, cr = self.book_totals()
        self.expect("and the whole book still balances to itself", dr == cr,
                    f"Dr {dr} / Cr {cr}")

        for name, url in (
            ("the register home", reverse("fixed_assets:index")),
            ("the asset list", reverse("fixed_assets:asset_list")),
            ("the disposed assets", reverse("fixed_assets:asset_list") + "?status=DISPOSED"),
            ("the categories", reverse("fixed_assets:category_list")),
            ("the month's run", run_all_url),
            ("the laptop's card", reverse("fixed_assets:asset_detail", args=[laptop.pk])),
            ("the pickup's card", reverse("fixed_assets:asset_detail", args=[vehicle.pk])),
            ("the drill's card", reverse("fixed_assets:asset_detail", args=[cap.pk])),
            ("the acquisition", reverse("accounting:journal_entry_detail", args=[acq.pk])),
            ("a month's charge", reverse("accounting:journal_entry_detail",
                                         args=[jan.journal_entry.pk])),
            ("the entry that answered it", reverse("accounting:journal_entry_detail",
                                                   args=[reversal.pk])),
            ("a disposal", reverse("accounting:journal_entry_detail",
                                   args=[laptop.disposal.journal_entry.pk])),
        ):
            self.get_ok(client, name, url)

        self.note("The register and the book are now one asset kept twice: the cost accounts "
                  "hold exactly the cost of what is still on the register, the contra asset "
                  "holds exactly the wear charged, and every charge and every disposal is "
                  "worked out of the price, the life and the residual rather than typed. A "
                  "month is charged once, stood down by an offsetting entry in its own "
                  "period, and re-charged against the same row. What is NOT here: an asset "
                  "is paid for out of the bank rather than owed to the supplier who sold "
                  "it, so a purchase on credit has to be booked by hand; proceeds land "
                  "straight on the bank rather than on a receivable, so an asset sold on "
                  "terms has no paper for the money coming in; there is no revaluation, "
                  "transfer between branches or maintenance history behind the tiles that "
                  "ask for them; and depreciation runs at the month the bookkeeper picks, "
                  "with no schedule that catches up several months on its own.")

    def verify_transaction_accuracy(self, client):
        """The finished book, read the way an auditor reads it.

        Sections 1-22 build the book transaction by transaction; this section
        forgets how it was built and reads only what is on the disk. Every
        entry balanced, every posted entry in its own company's accounts and
        stamped with its period, every source-document pointer resolving to a
        real row, and the control accounts agreeing with the subledgers they
        consolidate. What an accountant must be able to say at the end of any
        day: the book is whole, and the papers underneath it say the same.
        """
        from apps.accounting.models import JournalEntry
        from apps.fixed_assets.models import AssetDisposal, FixedAsset
        from apps.organizations.models import AccountingPeriod, FiscalYear
        from apps.procurement.models import Bill
        from apps.sales.models import Invoice

        company = self.company
        zero = D("0")

        entries = list(JournalEntry.objects.filter(
            company=company).prefetch_related("lines"))

        posted = [e for e in entries if e.status == "POSTED"]
        unbalanced = [e.reference for e in posted if not e.is_balanced()]
        self.expect("every posted entry is balanced — a lopsided entry can only exist as a draft",
                    not unbalanced, str(unbalanced[:5]))
        drafts = [e.reference for e in entries
                  if e.status == "DRAFT" and not e.is_balanced()]
        self.note(f"{len(drafts)} draft entr{'y is' if len(drafts) == 1 else 'ies are'} "
                  "deliberately lopsided: a draft is a working paper, and the "
                  "balance rule meets it at Post, not at save.")

        unstamped = [e.reference for e in posted if e.period_id is None]
        self.expect("every posted entry is stamped with the period it belongs to",
                    not unstamped, str(unstamped[:5]))

        unwarned = [e.reference for e in posted
                    if e.calendar_warning and "closed" not in e.calendar_warning]
        self.expect("no posted entry hides the state of the period it is dated in",
                    not unwarned, str(unwarned[:5]))
        late = sum(1 for e in posted if e.calendar_warning)
        self.note(f"{late} posted entr{'y sits' if late == 1 else 'ies sit'} in a "
                  "closed period on purpose: closing is reported, never "
                  "enforced, so a late adjustment can still be filed.")

        foreign_lines = sum(
            1 for e in entries for line in e.lines.all()
            if line.account.company_id != company.id)
        self.expect("every line in the book was posted into its own company's accounts",
                    foreign_lines == 0, f"{foreign_lines} foreign lines")

        targets = {"FixedAsset": FixedAsset, "AssetDisposal": AssetDisposal,
                   "ACCOUNTING_PERIOD": AccountingPeriod,
                   "FISCAL_YEAR": FiscalYear,
                   "EXCHANGE_REVALUATION": AccountingPeriod}
        dangling, unknown = [], []
        for e in entries:
            if not e.source_document_type:
                if e.source_document_id:
                    dangling.append((e.reference, "an id with no type"))
                continue
            model = targets.get(e.source_document_type)
            if model is None:
                unknown.append((e.reference, e.source_document_type))
                continue
            if e.source_document_type == "EXCHANGE_REVALUATION" \
                    and not e.source_document_id:
                continue
            if not model.objects.filter(pk=e.source_document_id).exists():
                dangling.append((e.reference, e.source_document_type))
        self.expect("every entry that names its source document points at a real row",
                    not dangling, str(dangling[:5]))
        self.expect("and no entry carries a source type this book does not know",
                    not unknown, str(unknown[:5]))

        receivable = self.account("1300").current_balance
        book = sum((i.outstanding_base for i in Invoice.objects.filter(
            company=company).exclude(status__in=["DRAFT", "CANCELLED"])), zero)
        self.expect("receivables is exactly what the invoice book says is still owed",
                    receivable == book, f"1300 {receivable} vs {book}")

        payable = self.account("2100").current_balance
        owed = sum((b.outstanding_base for b in Bill.objects.filter(
            company=company).exclude(status__in=["DRAFT", "CANCELLED"])), zero)
        self.expect("payables is exactly the paper still owed to suppliers",
                    payable == owed, f"2100 {payable} vs {owed}")

        dr, cr = self.book_totals()
        self.expect("and the whole book still balances to itself", dr == cr,
                    f"Dr {dr} / Cr {cr}")

        sheet = client.get(reverse("reporting:balance_sheet")).context or {}
        self.expect("the balance sheet agrees when everything above is on it",
                    sheet.get("is_balanced") and sheet.get("shows_profit"),
                    f"error {sheet.get('balancing_error')}, carried "
                    f"{sheet.get('current_year_profit')}")
        tb = client.get(reverse("accounting:trial_balance")).context
        self.expect("and the trial balance reports itself balanced",
                    tb is not None and tb["is_balanced"])


