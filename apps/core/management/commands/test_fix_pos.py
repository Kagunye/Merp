"""Regression checks for the till and the bank screens: the holes testers found
in who may touch a drawer, what a refund may pay back, which day a ticket
belongs to, and which ledger account a bank account may sit on.

Usage:
    python manage.py test_fix_pos          # rolls the whole run back
    python manage.py test_fix_pos --keep   # leave the test company behind

Everything goes through the screens a cashier or a bookkeeper would use, as
two cashiers at two branches of one company:

    1. one till, one cashier      -> a colleague cannot close another cashier's
                                     till or put a count on it; an administrator
                                     can, and is recorded as having done so
    2. refunds come out of the    -> a refund of a colleague's ticket is paid
       refunder's own drawer         from the refunder's till; with no open till
                                     there is nothing to pay from; a closed till
                                     is never changed after its cash-up
    3. the money a ticket took    -> three partial refunds of a fractional line
                                     pay back exactly what the line took, never
                                     a cent more; the refund screen counts every
                                     refund, not just the last
    4. the way it came in         -> a refund method that does not exist is
                                     refused, and a card sale is not paid back
                                     in notes unless an administrator says so
    5. the day in Nairobi         -> a ticket rung at 01:30 belongs to today's
                                     takings, the refund too
    6. the counted drawer         -> a short or over count is written off to cash
                                     over and short once; a till closed without a
                                     count can be counted afterwards, once
    7. bad input, not a crash     -> a sliver worth nothing, a customer or branch
                                     id that is not an id
    8. the till's own shelves     -> no selling off another branch's warehouse;
                                     a till opens at the cashier's own branch or
                                     asks which
    9. one form, one ticket       -> the same terminal or refund form submitted
                                     twice takes the money once
   10. bank accounts              -> only on a bank or cash ledger account, one
                                     per ledger account; foreign accounts show
                                     their own money; no negative or zero
                                     amounts; a cleared transaction is not
                                     edited under its statement
   12. no token, no ticket        -> a terminal or refund post without the
                                     form's one-time token is refused
   13. manual lines post          -> a deposit or withdrawal typed on the bank
                                     screen books Dr/Cr against its counter
                                     account; petty cash too; an edit re-books,
                                     a delete reverses; no future dates
   14. the ledger feeds the bank  -> till card takings, refunds, an expense
       book                          claim and a journal each put one line on
                                     the bank book; a reversal unwinds it, or
                                     answers it once the bank has seen it
   15. transfers                  -> one entry, a line on each account; KES to
                                     USD books the gap as realised exchange
   16. the statement chain        -> an earlier statement is not reversed
                                     while a later one stands
   17. one token, one action      -> a token spent on a sale, a refund or
                                     another sale's refund is refused with a
                                     message, never a 500
   18. a bank account's history   -> the opening balance is posted against
                                     3100 and locked; ledger account and
                                     currency are locked once there is history;
                                     a ledger account's earlier movements are
                                     brought onto a new bank book
   19. draft statements           -> a draft re-opens on the latest posted
                                     statement; it can be edited, deleted and
                                     posted with notes; statements run forward
   20. dated after today          -> not in the balance, not offered on a
                                     statement, listed apart
   21. cash cannot go below zero  -> petty cash spends and transfers past its
                                     balance are refused; a bank may overdraw
   22. where till takings land    -> card and M-Pesa go to the flagged bank
                                     account, at most one of each
   23. a refund at another branch -> comes back at the cost it was sold at
   24. the cash floor, everywhere -> every entry that empties a cash box past
                                     nothing is refused as it posts: a journal,
                                     a deleted deposit or transfer (no 500), a
                                     transfer reversed on the journal screen, a
                                     deposit moved or re-dated past its spend,
                                     a Cash account with no bank account; a
                                     line dated before an opening takes the
                                     opening back with it; a card refund goes
                                     back through the account the sale was
                                     banked in; the till list is ordered
   25. the full-screen till       -> the old ticket post still rings, with a
                                     salesperson and a reference
   26. line discounts             -> supervisors only; revenue and VAT at the
                                     discounted figure, cost of sales at cost
   27. payouts and pay-ins        -> Dr expense / Cr till and back; the drawer
                                     and the cash-up count them; once per form
   28. the item lookup            -> this company's active goods, 25 a page,
                                     a category and everything under it
   29. lock station               -> unlock with the cashier's password, with
                                     the sign-in lockout
"""
import datetime
import re
from decimal import Decimal
from unittest import mock

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum
from django.test import Client
from django.test.utils import setup_test_environment, teardown_test_environment
from django.urls import reverse
from django.utils import timezone

D = Decimal
Z = D("0")


class Command(BaseCommand):
    help = "Regression checks for the till (POS) and banking fixes."

    def add_arguments(self, parser):
        parser.add_argument("--keep", action="store_true",
                            help="Commit the test data instead of rolling it back.")

    def handle(self, *args, **opts):
        self.passed = 0
        self.failures = []
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

    def flash(self, response):
        ctx = getattr(response, "context", None)
        if not ctx or "messages" not in ctx:
            return []
        return [str(m.message) for m in ctx["messages"]]

    def flashes(self, client, response):
        """Messages only appear once the redirect target renders them."""
        location = response.get("Location")
        if not location:
            return self.flash(response)
        return self.flash(client.get(location))

    def expect_flash(self, client, response, needle, label):
        found = self.flashes(client, response)
        return self.expect(label, any(needle.lower() in m.lower() for m in found), str(found))

    def form_errors(self, response):
        ctx = getattr(response, "context", None)
        form = ctx.get("form") if ctx else None
        return dict(form.errors) if form is not None else {}

    # ────────────────────────────── ledger reads ─────────────────────────────

    def account(self, code):
        from apps.accounting.models import Account
        return Account.objects.get(company=self.company, code=code)

    def balance(self, code):
        """Debits less credits on the posted lines of one account."""
        from apps.accounting.models import Account, JournalEntryLine
        account = Account.objects.filter(company=self.company, code=code).first()
        if account is None:
            return Z
        lines = JournalEntryLine.objects.filter(account=account, journal_entry__status="POSTED")
        return ((lines.aggregate(t=Sum("debit_amount"))["t"] or Z)
                - (lines.aggregate(t=Sum("credit_amount"))["t"] or Z))

    def book_balances(self):
        from apps.accounting.models import JournalEntryLine
        lines = JournalEntryLine.objects.filter(
            account__company=self.company, journal_entry__status="POSTED")
        return (lines.aggregate(t=Sum("debit_amount"))["t"] or Z,
                lines.aggregate(t=Sum("credit_amount"))["t"] or Z)

    # ────────────────────────────── fixtures ─────────────────────────────────

    def login(self, user):
        client = Client(raise_request_exception=False)
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        return client

    def make_user(self, name, role=None, branch=None):
        from apps.accounts.models import User
        from apps.organizations.models import CompanyMembership
        user = User.objects.create(username=f"{name}.{self.stamp}",
                                   email=f"{name}.{self.stamp}@fixpos.local",
                                   first_name=name.split(".")[0].title(),
                                   last_name=name.split(".")[-1].upper())
        user.set_password("fix-pos-pw")
        user.save()
        if role:
            CompanyMembership.objects.create(company=self.company, user=user, role=role,
                                             branch=branch, is_default=True)
        return user

    def setup(self):
        from apps.accounting.models import TaxRate
        from apps.inventory.models import (Product, ProductCategory, StockMovement,
                                           UnitOfMeasure)
        from apps.organizations.models import Branch, Company, Warehouse

        self.stamp = datetime.datetime.now().strftime("%H%M%S%f")
        self.admin = self.make_user("pos.admin")
        self.admin_client = self.login(self.admin)
        name = f"Fix POS Traders {self.stamp}"
        self.admin_client.post(reverse("organizations:company_create"), {
            "name": name, "legal_name": f"{name} Limited",
            "registration_number": f"PVT-{self.stamp}", "tax_id": f"A{self.stamp}Z",
            "vat_number": "VAT-FIX", "currency": "KES", "timezone": "Africa/Nairobi",
            "fiscal_year_start_month": "1", "industry": "Trading",
            "phone": "+254 700 000 001", "email": f"info@{self.stamp}.fixpos.local",
            "website": "", "address_line1": "1 Moi Avenue", "address_line2": "",
            "city": "Nairobi", "state_province": "Nairobi", "postal_code": "00100",
            "country": "Kenya"})
        company = Company.objects.filter(name=name).first()
        if not self.expect("company created through the form", company is not None):
            raise SystemExit(1)
        self.company = company
        self.hq = company.branches.get(is_headquarters=True)
        self.msa = Branch.objects.create(company=company, name="Mombasa Branch", code="MSA",
                                         city="Mombasa", country="Kenya")
        self.wh_hq = Warehouse.objects.get(company=company, is_default=True)
        self.wh_msa = Warehouse.objects.create(company=company, branch=self.msa,
                                               name="Mombasa Store", code="MSAW",
                                               city="Mombasa", country="Kenya")
        self.expect("the default warehouse is Head Office's", self.wh_hq.branch_id == self.hq.pk)

        uom = (UnitOfMeasure.objects.filter(company=company, abbreviation="PCS").first()
               or UnitOfMeasure.objects.create(company=company, name="Pieces",
                                               abbreviation="PCS", is_base=True))
        category = ProductCategory.objects.create(company=company, name="General")
        vat = TaxRate.objects.filter(company=company, rate=D("16")).first()

        def product(sku, name, cost, price, track=True):
            return Product.objects.create(
                company=company, sku=sku, name=name, category=category,
                unit_of_measure=uom, cost_price=D(cost), selling_price=D(price),
                tax_rate=vat, track_inventory=track,
                product_type="GOODS" if track else "SERVICE",
                income_account=self.account("4200"), cogs_account=self.account("5000"),
                inventory_account=self.account("1400"))

        self.sugar = product("SUG-1", "Sugar 1kg", "120", "200")
        self.nail = product("NAIL-1", "Nail (loose)", "0.10", "0.335")
        self.milk = product("MLK-1", "Milk 500ml", "40", "60")
        today = timezone.localdate()
        for item, shelf, qty, cost in [(self.sugar, self.wh_hq, 50, "120"),
                                       (self.sugar, self.wh_msa, 20, "130"),
                                       (self.milk, self.wh_hq, 40, "40"),
                                       (self.milk, self.wh_msa, 10, "40"),
                                       (self.nail, self.wh_hq, 1000, "0.10")]:
            StockMovement.objects.create(
                product=item, warehouse=shelf, movement_type="RECEIPT", quantity=D(qty),
                unit_cost=D(cost), movement_date=today, reference=f"GRN-{item.sku}",
                performed_by=self.admin, notes="Opening stock for the till checks")

        self.nbo = self.make_user("cash.nbo", "POS_CASHIER", self.hq)
        self.msa_cashier = self.make_user("cash.msa", "POS_CASHIER", self.msa)
        self.floater = self.make_user("cash.float", "POS_CASHIER", None)
        self.accountant = self.make_user("acct.bank", "ACCOUNTANT", None)
        self.c_nbo = self.login(self.nbo)
        self.c_msa = self.login(self.msa_cashier)
        self.c_float = self.login(self.floater)
        self.c_acct = self.login(self.accountant)

    # ────────────────────────────── helpers ──────────────────────────────────

    def session_of(self, user):
        from apps.pos.models import POSSession
        return POSSession.objects.filter(company=self.company, cashier=user,
                                         status="OPEN").first()

    def open_till(self, client, user, branch=None, float_="5000"):
        data = {"opening_cash": float_}
        if branch is not None:
            data["branch"] = branch.pk
        client.post(reverse("pos:session_open"), data)
        return self.session_of(user)

    def form_token(self, client, url, name):
        """The one-time token a form renders, fetched the way a browser would.

        The terminal and refund views refuse a post without one, so every
        ticket and refund here opens its form first. A page the client cannot
        open (a role stopped at the door) gives no token; a fresh one is sent
        instead so the check still reaches the rule it is about.
        """
        import uuid
        page = client.get(url)
        ctx = getattr(page, "context", None)
        token = ctx.get(name) if ctx else None
        return token or uuid.uuid4().hex

    def ring(self, client, items, **extra):
        from apps.pos.models import POSSale
        data = {f"qty_{product.pk}": qty for product, qty in items.items()}
        data["ticket_token"] = self.form_token(client, reverse("pos:terminal"), "ticket_token")
        data.update(extra)
        response = client.post(reverse("pos:terminal"), data)
        match = re.search(r"/sales/([0-9a-fA-F-]{36})/?$", response.get("Location") or "")
        sale = POSSale.objects.filter(pk=match.group(1)).first() if match else None
        return response, sale

    def refund(self, client, sale, items, **extra):
        from apps.pos.models import POSSale
        data = {}
        for line in sale.lines.all():
            if line.product_id in {p.pk for p in items}:
                data[f"refund_qty_{line.pk}"] = items[next(p for p in items
                                                           if p.pk == line.product_id)]
        data["refund_token"] = self.form_token(
            client, reverse("pos:sale_detail", args=[sale.pk]), "refund_token")
        data.update(extra)
        response = client.post(reverse("pos:sale_refund", args=[sale.pk]), data)
        match = re.search(r"/sales/([0-9a-fA-F-]{36})/?$", response.get("Location") or "")
        ticket = POSSale.objects.filter(pk=match.group(1), refund_of=sale).first() \
            if match else None
        return response, ticket

    # ────────────────────────────── the run ──────────────────────────────────

    def run(self):
        self.section("0. A company with two branches and two cashiers")
        self.setup()
        self.check_opening()
        self.check_tickets_and_refunds()
        self.check_midnight()
        self.check_double_submit()
        self.check_bad_input()
        self.check_closing()
        self.check_banking()
        self.check_tokens_required()
        self.check_manual_bank_lines()
        self.check_ledger_feeds_bank_book()
        self.check_transfers()
        self.check_statement_chain()
        self.check_token_kinds()
        self.check_account_history()
        self.check_draft_statements()
        self.check_future_lines()
        self.check_cash_floor()
        self.check_takings_accounts()
        self.check_cross_branch_refund_cost()
        self.check_central_cash_floor()
        self.check_till_terminal()

        self.section("11. The book after it all")
        dr, cr = self.book_balances()
        self.expect("the book still balances to itself", dr == cr, f"Dr {dr} / Cr {cr}")

    def check_opening(self):
        from apps.pos.models import POSSession

        self.section("8. A till opens at the cashier's own branch, and sells its shelves")
        self.c_nbo.post(reverse("pos:session_open"), {"opening_cash": "5000"})
        nbo = self.session_of(self.nbo)
        self.expect("a till opened without a branch uses the cashier's own branch",
                    nbo is not None and nbo.branch_id == self.hq.pk,
                    nbo.branch.name if nbo else "no till")
        self.nbo_till = nbo
        msa = self.open_till(self.c_msa, self.msa_cashier)
        self.expect("and the Mombasa cashier's opens in Mombasa",
                    msa is not None and msa.branch_id == self.msa.pk,
                    msa.branch.name if msa else "no till")
        self.msa_till = msa

        response = self.c_float.post(reverse("pos:session_open"), {"opening_cash": "100"})
        self.expect_flash(self.c_float, response, "Choose the branch",
                          "a cashier with no branch at a two-branch company is asked which")
        self.expect("and no till was opened on a guess", self.session_of(self.floater) is None)
        response = self.c_float.post(reverse("pos:session_open"),
                                     {"branch": "abc", "opening_cash": "100"})
        self.expect("a branch id that is not an id is a message, not a server error",
                    response.status_code == 302 and self.session_of(self.floater) is None,
                    f"HTTP {response.status_code}")
        self.expect_flash(self.c_float, response, "does not belong",
                          "and the message says there is no such branch")
        response = self.c_nbo.get(reverse("pos:terminal"))
        offered = [w.pk for w in response.context["warehouses"]] if response.context else []
        self.expect("the terminal offers only the till's own branch's shelves",
                    self.wh_hq.pk in offered and self.wh_msa.pk not in offered, str(offered))
        self.expect("and renders a one-time ticket token",
                    bool(response.context and response.context.get("ticket_token")))

        before = POSSession.objects.filter(company=self.company).count()
        response, sale = self.ring(self.c_nbo, {self.milk: "1"}, payment_method="CASH",
                                   warehouse=str(self.wh_msa.pk))
        self.expect("a Head Office till cannot sell off the Mombasa store",
                    sale is None and response.status_code == 200
                    and any("Mombasa" in m for m in self.flash(response)),
                    str(self.flash(response)))
        self.expect("and opening a till never made a second one",
                    POSSession.objects.filter(company=self.company).count() == before)

    def check_tickets_and_refunds(self):
        from apps.pos.models import POSSale

        self.section("2-4. Refunds: whose drawer, how much, and by what")
        nbo, msa = self.nbo_till, self.msa_till
        response, cash_sale = self.ring(self.c_nbo, {self.sugar: "2", self.milk: "3"},
                                        payment_method="CASH", amount_tendered="1000")
        if not self.expect("a cash ticket at Head Office", cash_sale is not None,
                           str(self.flash(response))):
            return
        response, card_sale = self.ring(self.c_nbo, {self.sugar: "1"}, payment_method="CARD")
        response, nails = self.ring(self.c_nbo, {self.nail: "1.5"}, payment_method="CASH")
        if not self.expect("a fractional nail ticket worth 0.58",
                           nails is not None and nails.total_amount == D("0.58"),
                           str(nails.total_amount if nails else self.flash(response))):
            return

        # ── a colleague refunds a Head Office ticket ──
        nbo.refresh_from_db()
        msa.refresh_from_db()
        nbo_drawer, msa_drawer = nbo.expected_cash, msa.expected_cash
        response, back = self.refund(self.c_msa, cash_sale, {self.milk: "1"})
        nbo.refresh_from_db()
        msa.refresh_from_db()
        self.expect("a colleague's refund is booked on the refunder's own till",
                    back is not None and back.session_id == msa.pk,
                    f"{back.reference} on {back.session.cashier.username}'s till" if back
                    else str(self.flashes(self.c_msa, response)))
        self.expect("so the money comes out of the refunder's drawer, not the seller's",
                    nbo.expected_cash == nbo_drawer
                    and msa.expected_cash == msa_drawer - D("69.60"),
                    f"Head Office {nbo_drawer}->{nbo.expected_cash}, "
                    f"Mombasa {msa_drawer}->{msa.expected_cash}")
        self.expect("and the goods go on the refunder's branch shelf, where they now are",
                    back is not None and back.warehouse_id == self.wh_msa.pk,
                    back.warehouse.name if back and back.warehouse else "none")

        # ── no till, no refund ──
        count = POSSale.objects.filter(refund_of=cash_sale).count()
        response, back = self.refund(self.c_acct, cash_sale, {self.milk: "1"})
        # With roles enforced the accountant is stopped at the door (403); a till
        # role with no till of its own gets the "no open till" message instead.
        self.expect("someone with no open till of their own cannot pay a refund out",
                    POSSale.objects.filter(refund_of=cash_sale).count() == count
                    and (response.status_code == 403
                         or any("no open till" in str(m).lower()
                                for m in self.flashes(self.c_acct, response))),
                    f"HTTP {response.status_code}")
        self.expect("and nothing was refunded",
                    POSSale.objects.filter(refund_of=cash_sale).count() == count)

        # ── the money a line took, and no more ──
        for attempt in range(3):
            response, back = self.refund(self.c_nbo, nails, {self.nail: "0.5"})
            if not self.expect(f"nail refund 0.5 #{attempt + 1} goes through", back is not None,
                               str(self.flashes(self.c_nbo, response))):
                return
        nails.refresh_from_db()
        paid_back = POSSale.objects.filter(refund_of=nails).aggregate(
            t=Sum("total_amount"))["t"]
        self.expect("three partial refunds pay back exactly what the line took",
                    paid_back == D("0.58") and nails.refunded_amount == D("0.58")
                    and nails.status == "REFUNDED",
                    f"{paid_back} back of {nails.total_amount}, {nails.status}")
        tax_back = POSSale.objects.filter(refund_of=nails).aggregate(t=Sum("tax_amount"))["t"]
        self.expect("and the VAT given back is the VAT that was charged",
                    tax_back == nails.tax_amount, f"{tax_back} of {nails.tax_amount}")

        # ── the refund screen sums every refund ──
        response, again = self.refund(self.c_nbo, cash_sale, {self.milk: "1"})
        page = self.c_nbo.get(reverse("pos:sale_detail", args=[cash_sale.pk]))
        left = {row["line"].product_id: row["left"] for row in page.context["refundable"]} \
            if page.context else {}
        self.expect("the refund screen counts both milk refunds, not just the last",
                    left.get(self.milk.pk) == D("1") and left.get(self.sugar.pk) == D("2"),
                    str(left))

        # ── by the way it came in ──
        response, bogus = self.refund(self.c_nbo, cash_sale, {self.sugar: "1"},
                                      payment_method="BITCOIN")
        self.expect_flash(self.c_nbo, response, "not a way this till pays",
                          "a refund method that does not exist is refused")
        self.expect("and BITCOIN was never saved",
                    not POSSale.objects.filter(payment_method="BITCOIN",
                                               session__company=self.company).exists())
        response, as_cash = self.refund(self.c_nbo, card_sale, {self.sugar: "1"},
                                        payment_method="CASH")
        self.expect_flash(self.c_nbo, response, "goes back the way",
                          "a card sale is not paid back out of the drawer by a cashier")
        self.expect("and the card sale is untouched",
                    POSSale.objects.filter(refund_of=card_sale).count() == 0)
        bank0 = self.balance("1200")
        response, by_card = self.refund(self.c_nbo, card_sale, {self.sugar: "1"})
        self.expect("refunded the way it came in, the card money goes back from the bank",
                    by_card is not None and by_card.payment_method == "CARD"
                    and self.balance("1200") == bank0 - D("232.00"),
                    f"1200 moved {self.balance('1200') - bank0}")

        # An administrator may override the method, from their own till.
        response, card2 = self.ring(self.c_nbo, {self.milk: "1"}, payment_method="CARD")
        admin_till = self.open_till(self.admin_client, self.admin, self.hq, "1000")
        cash0 = self.balance("1100")
        response, override = self.refund(self.admin_client, card2, {self.milk: "1"},
                                         payment_method="CASH")
        self.expect("an administrator can refund a card sale in cash, from their own till",
                    override is not None and override.payment_method == "CASH"
                    and override.session_id == (admin_till.pk if admin_till else None)
                    and self.balance("1100") == cash0 - D("69.60"),
                    str(self.flashes(self.admin_client, response)) if override is None
                    else f"1100 moved {self.balance('1100') - cash0}")
        self.cash_sale = cash_sale

    def check_midnight(self):
        from apps.accounting.models import JournalEntry
        from apps.inventory.models import StockMovement

        self.section("5. A ticket rung after midnight belongs to today in Nairobi")
        today = timezone.localdate()
        small_hours = timezone.make_aware(datetime.datetime.combine(today, datetime.time(1, 30)))
        self.expect("01:30 in Nairobi is still yesterday in UTC",
                    small_hours.astimezone(datetime.timezone.utc).date() != today)
        with mock.patch("django.utils.timezone.now", return_value=small_hours):
            response, sale = self.ring(self.c_nbo, {self.milk: "1"}, payment_method="CASH")
            if not self.expect("the 01:30 ticket is rung", sale is not None,
                               str(self.flash(response))):
                return
            response, back = self.refund(self.c_nbo, sale, {self.milk: "1"})
        movement = StockMovement.objects.filter(reference=sale.reference).first()
        self.expect("its revenue is posted on today's date, the day its goods left",
                    sale.journal_entry.posting_date == today
                    and movement is not None and movement.movement_date == today,
                    f"entry {sale.journal_entry.posting_date}, stock "
                    f"{movement.movement_date if movement else None}")
        self.expect("and its refund too",
                    back is not None and back.journal_entry.posting_date == today,
                    str(back.journal_entry.posting_date if back else self.flash(response)))
        self.expect("nothing the till rang is dated yesterday",
                    not JournalEntry.objects.filter(
                        company=self.company, posting_date__lt=today,
                        reference__startswith="JE-POS-").exists())

    def check_double_submit(self):
        from apps.pos.models import POSSale

        self.section("9. One form, one ticket")
        page = self.c_nbo.get(reverse("pos:terminal"))
        token = page.context["ticket_token"]
        data = {f"qty_{self.milk.pk}": "1", "payment_method": "CASH",
                "amount_tendered": "100", "ticket_token": token}
        before = POSSale.objects.filter(session=self.nbo_till).count()
        cash0 = self.balance("1100")
        first = self.c_nbo.post(reverse("pos:terminal"), data)
        second = self.c_nbo.post(reverse("pos:terminal"), data)
        self.expect("the same ticket form posted twice rings one ticket",
                    POSSale.objects.filter(session=self.nbo_till).count() == before + 1
                    and self.balance("1100") == cash0 + D("69.60"),
                    f"{POSSale.objects.filter(session=self.nbo_till).count() - before} ticket(s)")
        self.expect("and the second post lands on the ticket the first made",
                    first.get("Location") and first.get("Location") == second.get("Location"),
                    f"{first.get('Location')} / {second.get('Location')}")
        self.expect_flash(self.c_nbo, second, "already rung",
                          "with a message that it was not taken twice")

        sale = self.cash_sale
        page = self.c_nbo.get(reverse("pos:sale_detail", args=[sale.pk]))
        token = page.context["refund_token"]
        line = sale.lines.get(product=self.sugar)
        data = {f"refund_qty_{line.pk}": "1", "refund_token": token}
        before = POSSale.objects.filter(refund_of=sale).count()
        self.c_nbo.post(reverse("pos:sale_refund", args=[sale.pk]), data)
        self.c_nbo.post(reverse("pos:sale_refund", args=[sale.pk]), data)
        self.expect("the same refund form posted twice pays back once",
                    POSSale.objects.filter(refund_of=sale).count() == before + 1,
                    f"{POSSale.objects.filter(refund_of=sale).count() - before} refund(s)")

    def check_bad_input(self):
        from apps.pos.models import POSSale

        self.section("7. Bad input is a message, never a server error")
        before = POSSale.objects.filter(session__company=self.company).count()
        response, sale = self.ring(self.c_nbo, {self.milk: "0.00001"}, payment_method="CASH")
        self.expect("a quantity worth less than a cent is refused with a message",
                    response.status_code == 200 and sale is None
                    and any("less than a cent" in m for m in self.flash(response)),
                    f"HTTP {response.status_code} {self.flash(response)}")
        response, sale = self.ring(self.c_nbo, {self.milk: "1"}, payment_method="CASH",
                                   customer="not-a-uuid")
        self.expect("a customer id that is not an id is a message",
                    response.status_code == 200 and sale is None
                    and any("customer" in m for m in self.flash(response)),
                    f"HTTP {response.status_code} {self.flash(response)}")
        response, sale = self.ring(self.c_nbo, {self.milk: "1"}, payment_method="CASH",
                                   warehouse="not-a-uuid")
        self.expect("so is a warehouse id that is not an id",
                    response.status_code == 200 and sale is None,
                    f"HTTP {response.status_code}")
        self.expect("and none of them left a ticket behind",
                    POSSale.objects.filter(session__company=self.company).count() == before)
        from apps.accounting import gl_service
        self.expect("the ledger's own refusal is a ValidationError it can raise",
                    hasattr(gl_service, "ValidationError"))

    def check_closing(self):
        from apps.accounting.models import JournalEntry
        from apps.pos.models import POSSale, POSSession

        self.section("1. One till, one cashier")
        nbo, msa = self.nbo_till, self.msa_till
        response = self.c_msa.post(reverse("pos:session_close", args=[nbo.pk]),
                                   {"closing_cash": "1"})
        nbo.refresh_from_db()
        self.expect_flash(self.c_msa, response, "only they can close",
                          "a colleague cannot close another cashier's till")
        self.expect("and the till is still open, uncounted",
                    nbo.status == "OPEN" and nbo.closing_cash is None,
                    f"{nbo.status}, counted {nbo.closing_cash}")
        page = self.c_msa.get(reverse("pos:session_detail", args=[nbo.pk]))
        self.expect("the colleague is not offered the close form",
                    page.context is not None and page.context["can_close"] is False)

        self.section("6. The counted drawer is written off once")
        msa.refresh_from_db()
        cash0, over_short0 = self.balance("1100"), self.balance("6750")
        short = msa.expected_cash - D("50")
        self.c_msa.post(reverse("pos:session_close", args=[msa.pk]),
                        {"closing_cash": str(short)})
        msa.refresh_from_db()
        entry = msa.variance_entry
        self.expect("a drawer counted 50 short is closed by its own cashier",
                    msa.status == "CLOSED" and msa.variance == D("-50.00")
                    and msa.closed_by_id == self.msa_cashier.pk,
                    f"{msa.status}, variance {msa.variance}")
        self.expect("and the 50 is written off: Dr cash over and short, Cr the till",
                    entry is not None and entry.status == "POSTED"
                    and self.balance("1100") == cash0 - D("50")
                    and self.balance("6750") == over_short0 + D("50"),
                    f"1100 {self.balance('1100') - cash0}, 6750 "
                    f"{self.balance('6750') - over_short0}")
        self.expect("cash over and short is an expense account",
                    self.account("6750").account_type == "EXPENSE")
        response = self.c_msa.post(reverse("pos:session_close", args=[msa.pk]),
                                   {"closing_cash": str(short)})
        self.expect_flash(self.c_msa, response, "already closed", "closing twice is refused")
        response = self.c_msa.post(reverse("pos:cash_up", args=[msa.pk]), {"closing_cash": "1"})
        self.expect_flash(self.c_msa, response, "already counted",
                          "and a counted drawer cannot be counted again")
        self.expect("so the shortage is in the book once",
                    JournalEntry.objects.filter(company=self.company,
                                                reference__startswith="JE-TILL-").count() == 1
                    and self.balance("1100") == cash0 - D("50"))

        # A refund after the cash-up never touches the closed till.
        drawer = msa.expected_cash
        response, back = self.refund(self.c_msa, self.cash_sale, {self.sugar: "1"})
        msa.refresh_from_db()
        self.expect_flash(self.c_msa, response, "no open till",
                          "a cashier whose till is closed cannot refund on it")
        self.expect("and the closed till's figures are what its cash-up said",
                    msa.expected_cash == drawer and msa.variance == D("-50.00"),
                    f"expected {msa.expected_cash}, variance {msa.variance}")

        # An administrator closes a till on the cashier's behalf, without a count.
        cash0, over_short0 = self.balance("1100"), self.balance("6750")
        response = self.admin_client.post(reverse("pos:session_close", args=[nbo.pk]), {})
        nbo.refresh_from_db()
        self.expect("an administrator can close a cashier's till for them, and is recorded",
                    nbo.status == "CLOSED" and nbo.closed_by_id == self.admin.pk
                    and nbo.closing_cash is None and nbo.variance_entry_id is None,
                    f"{nbo.status}, closed by {nbo.closed_by}")
        page = self.c_nbo.get(reverse("pos:cash_up", args=[nbo.pk]))
        self.expect("the cash-up sheet offers the late count to the till's cashier",
                    page.status_code == 200 and page.context["can_count"] is True)
        response = self.c_msa.post(reverse("pos:cash_up", args=[nbo.pk]), {"closing_cash": "1"})
        nbo.refresh_from_db()
        self.expect_flash(self.c_msa, response, "only they can count",
                          "a colleague cannot put a count on someone else's till")
        over = nbo.expected_cash + D("20")
        response = self.c_nbo.post(reverse("pos:cash_up", args=[nbo.pk]),
                                   {"closing_cash": str(over)})
        nbo.refresh_from_db()
        self.expect("a count recorded after closing is accepted, not a 405",
                    response.status_code == 302 and nbo.closing_cash == over,
                    f"HTTP {response.status_code}, counted {nbo.closing_cash}")
        self.expect("and 20 over is booked: Dr the till, Cr cash over and short",
                    nbo.variance_entry_id is not None
                    and self.balance("1100") == cash0 + D("20")
                    and self.balance("6750") == over_short0 - D("20"),
                    f"1100 {self.balance('1100') - cash0}, "
                    f"6750 {self.balance('6750') - over_short0}")

        # Exact counts book nothing.
        till = self.open_till(self.c_msa, self.msa_cashier, float_="1000")
        entries = JournalEntry.objects.filter(company=self.company).count()
        self.c_msa.post(reverse("pos:session_close", args=[till.pk]), {"closing_cash": "1000"})
        till.refresh_from_db()
        self.expect("a drawer that counts out exactly books nothing",
                    till.status == "CLOSED" and till.variance == Z
                    and JournalEntry.objects.filter(company=self.company).count() == entries)
        self.expect("every ticket still has its own posted entry",
                    not POSSale.objects.filter(session__company=self.company,
                                               journal_entry=None).exists())
        self.expect("and no session was left half-open",
                    not POSSession.objects.filter(company=self.company,
                                                  status="CLOSING").exists())

    def check_banking(self):
        from apps.accounting.models import Account
        from apps.banking.models import BankAccount, BankTransaction, Reconciliation

        self.section("10. Bank accounts and their transactions")
        client = self.admin_client
        url = reverse("banking:account_create")

        def account_form(name, gl, currency="KES", opening="0"):
            return {"name": name, "bank_name": "Equity", "account_number": name[:8],
                    "account_type": "CURRENT", "currency": currency, "branch": "",
                    "gl_account": gl.pk, "opening_balance": opening}

        for code in ("1300", "1400"):
            response = client.post(url, account_form(f"Bank on {code}", self.account(code)))
            self.expect(f"a bank account cannot sit on {code} {self.account(code).name}",
                        response.status_code == 200
                        and "gl_account" in self.form_errors(response)
                        and not BankAccount.objects.filter(company=self.company,
                                                           name=f"Bank on {code}").exists(),
                        str(self.form_errors(response).get("gl_account")))
        response = client.post(url, account_form("Equity KES", self.account("1200"),
                                                 opening="10000"))
        kes = BankAccount.objects.filter(company=self.company, name="Equity KES").first()
        self.expect("a bank account on the Bank ledger account is accepted",
                    kes is not None, str(self.form_errors(response)))
        response = client.post(url, account_form("KCB KES", self.account("1200")))
        self.expect("a second bank account on the same ledger account is refused",
                    response.status_code == 200
                    and "already holds" in " ".join(self.form_errors(response).get(
                        "gl_account", [])),
                    str(self.form_errors(response)))
        if kes:
            response = client.post(reverse("banking:account_edit", args=[kes.pk]),
                                   account_form("Equity KES", self.account("1200"),
                                                opening="10000"))
            self.expect("but the account itself can still be saved on its own ledger account",
                        response.status_code == 302, str(self.form_errors(response)))

        usd_gl = Account.objects.create(company=self.company, code="1210", name="Bank USD",
                                        account_type="BANK", parent=self.account("1000"),
                                        currency="USD")
        from apps.accounting.models import Currency
        Currency.objects.get_or_create(code="USD", defaults={"name": "US Dollar"})
        usd = BankAccount.objects.create(company=self.company, name="Equity USD",
                                         bank_name="Equity", account_number="USD1",
                                         currency="USD", gl_account=usd_gl,
                                         opening_balance=D("500"))
        BankTransaction.objects.create(bank_account=usd, transaction_date=timezone.localdate(),
                                       transaction_type="CREDIT", amount=D("100"),
                                       description="USD in")
        page = client.get(reverse("banking:account_detail", args=[usd.pk])).content.decode()
        self.expect("a USD account shows its balance in USD from its own transactions",
                    "USD 600.00" in page, "USD 600.00 not on the page")
        self.expect("and its ledger value labelled in the book's currency, not as USD",
                    "KES 0.00" in page and "USD 0.00" not in page)
        listing = client.get(reverse("banking:account_list")).content.decode()
        self.expect("the account list labels it the same way",
                    "USD 600.00" in listing and "USD 0.00" not in listing)

        if not kes:
            return
        tx_url = reverse("banking:transaction_create")
        day = timezone.localdate() - datetime.timedelta(days=3)

        # Every manual line names its counter account now, because saving it
        # books the ledger entry; these deposits are the owner's capital.
        def txn_form(amount, kind="CREDIT", description="Deposit", account=kes):
            return {"bank_account": account.pk, "transaction_date": day.isoformat(),
                    "value_date": "", "transaction_type": kind, "amount": amount,
                    "counter_account": self.account("3100").pk,
                    "description": description, "reference": ""}

        for amount in ("-500", "0"):
            response = client.post(tx_url, txn_form(amount, description=f"Odd {amount}"))
            self.expect(f"a bank transaction of {amount} is refused",
                        response.status_code == 200 and "amount" in self.form_errors(response)
                        and not BankTransaction.objects.filter(
                            bank_account=kes, description=f"Odd {amount}").exists(),
                        str(self.form_errors(response).get("amount")))
        client.post(tx_url, txn_form("5000", description="Cleared deposit"))
        deposit = BankTransaction.objects.filter(bank_account=kes,
                                                 description="Cleared deposit").first()
        if not self.expect("a proper deposit is recorded", deposit is not None):
            return
        client.post(reverse("banking:reconciliation_create"), {
            "bank_account": kes.pk, "reference": "STM-FIX-1",
            "statement_date": day.isoformat(), "closing_balance": "15000", "notes": ""})
        recon = Reconciliation.objects.filter(bank_account=kes, reference="STM-FIX-1").first()
        if not self.expect("a statement is started", recon is not None):
            return
        client.post(reverse("banking:reconciliation_clear", args=[recon.pk]),
                    {"cleared": [str(deposit.pk)]})
        client.post(reverse("banking:reconciliation_post", args=[recon.pk]))
        recon.refresh_from_db()
        self.expect("and posted with the deposit cleared on it", recon.status == "POSTED",
                    recon.status)
        edit_url = reverse("banking:transaction_edit", args=[deposit.pk])
        page = client.get(edit_url)
        self.expect("the edit screen of a cleared transaction is not offered",
                    page.status_code == 302, f"HTTP {page.status_code}")
        response = client.post(edit_url, txn_form("5", kind="DEBIT",
                                                  description="Cleared deposit"))
        deposit.refresh_from_db()
        recon.refresh_from_db()
        self.expect_flash(client, response, "reverse the statement",
                          "a cleared transaction cannot be edited under its posted statement")
        self.expect("and its amount and direction are what the statement cleared",
                    deposit.amount == D("5000") and deposit.transaction_type == "CREDIT"
                    and recon.is_balanced,
                    f"{deposit.transaction_type} {deposit.amount}, difference {recon.difference}")
        client.post(tx_url, txn_form("700", description="Open deposit"))
        open_one = BankTransaction.objects.filter(bank_account=kes,
                                                  description="Open deposit").first()
        response = client.post(reverse("banking:transaction_edit", args=[open_one.pk]),
                               txn_form("750", description="Open deposit"))
        open_one.refresh_from_db()
        self.expect("an outstanding transaction can still be corrected",
                    response.status_code == 302 and open_one.amount == D("750"),
                    f"HTTP {response.status_code}, {open_one.amount}")
        self.kes = kes
        self.usd = usd

    # ───────────────────── 12. the one-time token is required ─────────────────

    def check_tokens_required(self):
        from apps.pos.models import POSSale

        self.section("12. No one-time token, no ticket and no refund")
        till = self.open_till(self.c_nbo, self.nbo, float_="2000")
        if not self.expect("a fresh till is open for the checks that follow", till is not None):
            return
        self.nbo_till = till
        before = POSSale.objects.filter(session__company=self.company).count()
        response = self.c_nbo.post(reverse("pos:terminal"), {
            f"qty_{self.milk.pk}": "1", "payment_method": "CASH", "amount_tendered": "100"})
        self.expect("a ticket posted without the form's token is not rung",
                    response.status_code == 200
                    and POSSale.objects.filter(session__company=self.company).count() == before,
                    f"HTTP {response.status_code}")
        self.expect("and the cashier is told why, on the screen with a fresh token",
                    any("one-time token" in m for m in self.flash(response))
                    and bool(response.context and response.context.get("ticket_token")),
                    str(self.flash(response)))
        response = self.c_nbo.post(reverse("pos:terminal"), {
            f"qty_{self.milk.pk}": "1", "payment_method": "CASH", "ticket_token": "   "})
        self.expect("a blank token is no token",
                    POSSale.objects.filter(session__company=self.company).count() == before)

        response, sale = self.ring(self.c_nbo, {self.sugar: "2"}, payment_method="CASH")
        if not self.expect("with the form's token the same ticket rings", sale is not None,
                           str(self.flash(response))):
            return
        line = sale.lines.first()
        response = self.c_nbo.post(reverse("pos:sale_refund", args=[sale.pk]),
                                   {f"refund_qty_{line.pk}": "1"})
        self.expect("a refund posted without the form's token pays nothing out",
                    POSSale.objects.filter(refund_of=sale).count() == 0)
        self.expect_flash(self.c_nbo, response, "one-time token",
                          "and the message says to refund from the ticket's page")
        response, back = self.refund(self.c_nbo, sale, {self.sugar: "1"})
        self.expect("from the ticket's own page the refund goes through", back is not None,
                    str(self.flashes(self.c_nbo, response)))

    # ───────────────────── 13. manual bank lines post to the ledger ───────────

    def manual_line(self, account, kind, amount, description, counter=None, day=None):
        from apps.banking.models import BankTransaction
        data = {"bank_account": account.pk,
                "transaction_date": (day or timezone.localdate()).isoformat(),
                "value_date": "", "transaction_type": kind, "amount": amount,
                "counter_account": counter.pk if counter else "",
                "description": description, "reference": ""}
        response = self.admin_client.post(reverse("banking:transaction_create"), data)
        return response, BankTransaction.objects.filter(bank_account=account,
                                                        description=description).first()

    def check_manual_bank_lines(self):
        from apps.banking.models import BankAccount, BankTransaction

        self.section("13. A deposit or withdrawal typed on the bank screen posts to the ledger")
        kes = self.kes
        bank0, capital0, charges0 = self.balance("1200"), self.balance("3100"), self.balance("7100")
        # The ledger account already held till takings from before this bank
        # account existed, so the two are compared by how far each moves.
        book0 = kes.statement_balance

        def agree():
            return (self.balance("1200") - bank0) == (kes.statement_balance - book0)
        response, deposit = self.manual_line(kes, "CREDIT", "50000", "Owner's capital in",
                                             self.account("3100"))
        self.expect("a deposit of 50,000 is saved with a posted entry behind it",
                    deposit is not None and deposit.journal_entry is not None
                    and deposit.journal_entry.status == "POSTED" and deposit.origin == "MANUAL",
                    str(self.form_errors(response)))
        self.expect("Dr bank 50,000 / Cr share capital 50,000",
                    self.balance("1200") == bank0 + D("50000")
                    and self.balance("3100") == capital0 - D("50000"),
                    f"1200 {self.balance('1200') - bank0}, 3100 {self.balance('3100') - capital0}")
        self.expect("and it is on the bank book once, not twice",
                    deposit is not None and BankTransaction.objects.filter(
                        journal_entry=deposit.journal_entry).count() == 1)
        response, charge = self.manual_line(kes, "DEBIT", "8000", "Ledger fees",
                                            self.account("7100"))
        response, loan_out = self.manual_line(kes, "DEBIT", "13000", "Loan repayment",
                                              self.account("2810"))
        self.expect("withdrawals of 8,000 and 13,000 take 21,000 off the bank",
                    charge is not None and loan_out is not None
                    and self.balance("1200") == bank0 + D("50000") - D("21000")
                    and self.balance("7100") == charges0 + D("8000"),
                    f"1200 moved {self.balance('1200') - bank0}")
        self.expect("so the bank's ledger account moves exactly as its bank book does",
                    agree(), f"ledger {self.balance('1200') - bank0}, "
                             f"bank book {kes.statement_balance - book0}")

        response, none = self.manual_line(kes, "DEBIT", "100", "No counter account")
        self.expect("a line with no counter account is refused, and nothing saved",
                    none is None and "counter_account" in self.form_errors(response),
                    str(self.form_errors(response)))
        response, wrong = self.manual_line(kes, "CREDIT", "100", "Customer money",
                                           self.account("1300"))
        self.expect("receivables are not offered as a counter account",
                    wrong is None and "counter_account" in self.form_errors(response),
                    str(self.form_errors(response).get("counter_account")))
        tomorrow = timezone.localdate() + datetime.timedelta(days=1)
        response, future = self.manual_line(kes, "CREDIT", "100", "Tomorrow's money",
                                            self.account("4400"), day=tomorrow)
        self.expect("a date after today is refused",
                    future is None and "transaction_date" in self.form_errors(response),
                    str(self.form_errors(response).get("transaction_date")))
        response, today_line = self.manual_line(kes, "CREDIT", "120", "Interest today",
                                                self.account("4400"))
        self.expect("today itself is accepted", today_line is not None,
                    str(self.form_errors(response)))

        # Petty cash on a Cash ledger account behaves the same way.
        response = self.admin_client.post(reverse("banking:account_create"), {
            "name": "Petty cash", "bank_name": "Office", "account_number": "PC1",
            "account_type": "PETTY_CASH", "currency": "KES", "branch": "",
            "gl_account": self.account("1110").pk, "opening_balance": "0"})
        petty = BankAccount.objects.filter(company=self.company, name="Petty cash").first()
        if not self.expect("a petty-cash account sits on the Cash ledger account 1110",
                           petty is not None, str(self.form_errors(response))):
            return
        self.petty = petty
        cash0, office0 = self.balance("1110"), self.balance("6600")
        response, float_in = self.manual_line(petty, "CREDIT", "1000", "Float from owner",
                                              self.account("3100"))
        response, spend = self.manual_line(petty, "DEBIT", "300", "Printer paper",
                                           self.account("6600"))
        self.expect("a petty-cash spend of 300 moves the Cash ledger account",
                    spend is not None and self.balance("1110") == cash0 + D("700")
                    and self.balance("6600") == office0 + D("300"),
                    f"1110 {self.balance('1110') - cash0}, 6600 {self.balance('6600') - office0}")

        # An edit that moves money re-books; one that only renames does not.
        old_entry = charge.journal_entry
        response = self.admin_client.post(reverse("banking:transaction_edit", args=[charge.pk]), {
            "bank_account": kes.pk, "transaction_date": charge.transaction_date.isoformat(),
            "value_date": "", "transaction_type": "DEBIT", "amount": "8500",
            "counter_account": self.account("7100").pk, "description": "Ledger fees",
            "reference": ""})
        charge.refresh_from_db()
        old_entry.refresh_from_db()
        self.expect("an uncleared withdrawal corrected to 8,500 is re-booked",
                    response.status_code == 302 and charge.amount == D("8500")
                    and old_entry.reversed_by_id is not None
                    and charge.journal_entry_id not in (None, old_entry.pk)
                    and self.balance("7100") == charges0 + D("8500")
                    and self.balance("1200") == bank0 + D("50000") - D("21500") + D("120"),
                    f"HTTP {response.status_code}, 7100 {self.balance('7100') - charges0}")
        self.expect("and the old entry's reversal leaves nothing on the bank book",
                    not BankTransaction.objects.filter(
                        journal_entry=old_entry.reversed_by).exists()
                    and not BankTransaction.objects.filter(journal_entry=old_entry).exists())
        entry = charge.journal_entry
        self.admin_client.post(reverse("banking:transaction_edit", args=[charge.pk]), {
            "bank_account": kes.pk, "transaction_date": charge.transaction_date.isoformat(),
            "value_date": "", "transaction_type": "DEBIT", "amount": "8500",
            "counter_account": self.account("7100").pk, "description": "Bank ledger fees",
            "reference": "CHG-1"})
        charge.refresh_from_db()
        self.expect("a change of wording only keeps the same entry",
                    charge.journal_entry_id == entry.pk and charge.reference == "CHG-1")

        # Delete reverses.
        entry = loan_out.journal_entry
        response = self.admin_client.post(reverse("banking:transaction_delete", args=[loan_out.pk]))
        entry.refresh_from_db()
        self.expect("deleting an uncleared withdrawal reverses its entry",
                    not BankTransaction.objects.filter(pk=loan_out.pk).exists()
                    and entry.reversed_by_id is not None
                    and self.balance("1200") == bank0 + D("50000") - D("8500") + D("120"),
                    f"1200 moved {self.balance('1200') - bank0}")
        self.expect_flash(self.admin_client, response, "reversed by",
                          "and says which entry reversed it")
        self.expect("after the edit and the delete the bank book and the ledger still agree",
                    agree(), f"ledger {self.balance('1200') - bank0}, "
                             f"bank book {kes.statement_balance - book0}")

    # ───────────────────── 14. the ledger feeds the bank book ─────────────────

    def check_ledger_feeds_bank_book(self):
        from apps.accounting.models import JournalEntry, JournalEntryLine
        from apps.banking import bankbook, services
        from apps.banking.models import BankTransaction, Reconciliation
        from apps.expenses.models import ExpenseCategory, ExpenseClaim, ExpenseClaimLine
        from apps.hr.models import Employee
        from apps.accounting import gl_service

        self.section("14. Every posted entry on a bank account reaches its bank book")
        kes = self.kes
        today = timezone.localdate()
        bank0, book0 = self.balance("1200"), kes.statement_balance

        response, card = self.ring(self.c_nbo, {self.sugar: "1"}, payment_method="CARD")
        line = BankTransaction.objects.filter(journal_entry=card.journal_entry).first() \
            if card else None
        self.expect("a card ticket puts its takings on the bank book of the bank behind 1200",
                    line is not None and line.bank_account_id == kes.pk
                    and line.origin == "LEDGER" and line.transaction_type == "CREDIT"
                    and line.amount == card.total_amount,
                    f"{line.transaction_type} {line.amount}" if line else "no line")
        response, mpesa = self.ring(self.c_nbo, {self.milk: "1"}, payment_method="MPESA")
        self.expect("so does an M-Pesa ticket",
                    mpesa is not None and BankTransaction.objects.filter(
                        journal_entry=mpesa.journal_entry, bank_account=kes).exists(),
                    str(self.flash(response)))
        response, cash = self.ring(self.c_nbo, {self.milk: "1"}, payment_method="CASH")
        self.expect("a cash ticket does not — 1100 has no bank account behind it",
                    cash is not None and not BankTransaction.objects.filter(
                        journal_entry=cash.journal_entry).exists())
        self.expect("the takings are offered on a statement for ticking",
                    line is not None and line in list(services.outstanding(kes, today)))
        response, back = self.refund(self.c_nbo, card, {self.sugar: "1"})
        out = BankTransaction.objects.filter(journal_entry=back.journal_entry).first() \
            if back else None
        self.expect("a card refund is an outflow on the bank book",
                    out is not None and out.transaction_type == "DEBIT"
                    and out.amount == back.total_amount, str(out))
        before = BankTransaction.objects.count()
        bankbook.feed(card.journal_entry)
        self.expect("feeding the same entry again adds nothing",
                    BankTransaction.objects.count() == before)

        employee = Employee.objects.create(company=self.company, employee_number=f"E{self.stamp}",
                                           first_name="Peter", last_name="Petty",
                                           date_hired=today)
        category = ExpenseCategory.objects.create(company=self.company, name="Office tea",
                                                  gl_account=self.account("6600"))
        claim = ExpenseClaim.objects.create(company=self.company, reference=f"EXP-{self.stamp}",
                                            employee=employee, claim_date=today,
                                            status="APPROVED", total_amount=D("750"))
        ExpenseClaimLine.objects.create(claim=claim, category=category, description="tea",
                                        expense_date=today, amount=D("750"))
        # A cash box cannot pay out notes it does not hold (refuse_cash_below_zero),
        # so the box is topped up from the bank before the claim is paid from it.
        top_up = JournalEntry.objects.create(company=self.company, reference=f"JNL-TOPUP-{self.stamp}",
                                             entry_type="MANUAL", posting_date=today,
                                             notes="Petty cash top-up")
        JournalEntryLine.objects.create(journal_entry=top_up, account=self.petty.gl_account,
                                        debit_amount=D("750"), credit_amount=D("0"))
        JournalEntryLine.objects.create(journal_entry=top_up, account=self.account("1200"),
                                        debit_amount=D("0"), credit_amount=D("750"))
        top_up.post(user=self.admin)
        entry = gl_service.post_expense_claim(claim, bank_account=self.petty, user=self.admin)
        paid = BankTransaction.objects.filter(journal_entry=entry).first()
        self.expect("an expense claim paid from petty cash is on petty cash's book",
                    paid is not None and paid.bank_account_id == self.petty.pk
                    and paid.transaction_type == "DEBIT" and paid.amount == D("750"), str(paid))
        self.expect("and petty cash's book still agrees with its ledger account",
                    self.petty.current_balance == self.petty.statement_balance,
                    f"{self.petty.current_balance} / {self.petty.statement_balance}")

        def journal(amount, memo):
            je = JournalEntry.objects.create(company=self.company, reference=f"JNL-{memo}",
                                             entry_type="MANUAL", posting_date=today,
                                             notes=memo)
            JournalEntryLine.objects.create(journal_entry=je, account=self.account("1200"),
                                            debit_amount=D(amount), credit_amount=Z)
            JournalEntryLine.objects.create(journal_entry=je, account=self.account("4400"),
                                            debit_amount=Z, credit_amount=D(amount))
            je.post(user=self.admin)
            return je

        je = journal("444", "interest-1")
        fed = BankTransaction.objects.filter(journal_entry=je)
        self.expect("a hand-typed journal on 1200 is on the bank book", fed.count() == 1)
        reversal = je.reverse(user=self.admin)
        self.expect("reversed before the bank saw it, it leaves the bank book entirely",
                    not BankTransaction.objects.filter(journal_entry__in=[je, reversal]).exists())

        je = journal("555", "interest-2")
        seen = BankTransaction.objects.get(journal_entry=je)
        recon = services.create_draft(self.company, kes, f"STM-SEEN-{self.stamp}", today,
                                      D("0"), user=self.admin)
        services.clear(seen, recon)
        reversal = je.reverse(user=self.admin)
        answer = BankTransaction.objects.filter(journal_entry=reversal).first()
        self.expect("reversed after the bank saw it, the original stays ticked",
                    BankTransaction.objects.filter(pk=seen.pk, is_reconciled=True).exists())
        self.expect("and the reversal gets an outflow of its own to tick",
                    answer is not None and answer.transaction_type == "DEBIT"
                    and answer.amount == D("555") and not answer.is_reconciled, str(answer))
        services.unclear(BankTransaction.objects.get(pk=seen.pk))
        recon.delete()
        self.expect("no statement was left behind by the check",
                    not Reconciliation.objects.filter(reference=f"STM-SEEN-{self.stamp}").exists())
        response = self.admin_client.post(reverse("banking:transaction_delete",
                                                  args=[line.pk]))
        self.expect_flash(self.admin_client, response, "correct or reverse",
                          "a fed line is corrected at its source, not deleted here")
        self.expect("and it is still there", BankTransaction.objects.filter(pk=line.pk).exists())
        response = self.admin_client.get(reverse("banking:transaction_edit", args=[line.pk]))
        self.expect("nor edited there", response.status_code == 302, f"HTTP {response.status_code}")
        self.expect("1200 and its bank book moved by the same amount through all of it",
                    self.balance("1200") - bank0 == kes.statement_balance - book0,
                    f"ledger {self.balance('1200') - bank0}, "
                    f"bank book {kes.statement_balance - book0}")

    # ───────────────────── 15. transfers between own accounts ─────────────────

    def check_transfers(self):
        from apps.accounting.models import Currency, ExchangeRate, JournalEntry
        from apps.banking.models import BankTransaction

        self.section("15. Transfers: one entry, a line on each account")
        kes, usd, petty = self.kes, self.usd, self.petty
        url = reverse("banking:transfer_create")
        today = timezone.localdate()
        page = self.admin_client.get(url)
        self.expect("the transfer screen opens", page.status_code == 200,
                    f"HTTP {page.status_code}")

        def transfer(source, destination, sent, received="", description="Transfer"):
            return self.admin_client.post(url, {
                "from_account": source.pk, "to_account": destination.pk,
                "transfer_date": today.isoformat(), "amount_sent": sent,
                "amount_received": received, "reference": "", "description": description})

        bank0, cash0 = self.balance("1200"), self.balance("1110")
        response = transfer(kes, petty, "2000", description="Top up petty cash")
        out = BankTransaction.objects.filter(bank_account=kes,
                                             description__startswith="Top up petty cash").first()
        into = BankTransaction.objects.filter(bank_account=petty,
                                              description__startswith="Top up petty cash").first()
        self.expect("a KES transfer posts one entry with a line on each account",
                    response.status_code == 302 and out is not None and into is not None
                    and out.journal_entry_id == into.journal_entry_id
                    and out.transaction_type == "DEBIT" and into.transaction_type == "CREDIT"
                    and BankTransaction.objects.filter(journal_entry=out.journal_entry).count() == 2,
                    str(self.form_errors(response)))
        self.expect("and moves 2,000 from 1200 to 1110",
                    self.balance("1200") == bank0 - D("2000")
                    and self.balance("1110") == cash0 + D("2000"))
        response = transfer(kes, petty, "2000", "1900")
        self.expect("between two shilling accounts a different amount received is refused",
                    response.status_code == 200 and "amount_received" in self.form_errors(response))
        response = transfer(kes, kes, "10")
        self.expect("a transfer to the same account is refused",
                    response.status_code == 200 and "to_account" in self.form_errors(response))

        Currency.objects.get_or_create(code="USD", defaults={"name": "US Dollar"})
        usd_cur = Currency.objects.get(code="USD")
        ExchangeRate.objects.update_or_create(company=self.company, currency=usd_cur,
                                              effective_date=today, defaults={"rate": D("130")})
        response = transfer(kes, usd, "13000", description="Dollars for imports")
        self.expect("KES to USD without the dollars received is refused",
                    response.status_code == 200 and "amount_received" in self.form_errors(response))
        bank0, usd0, loss0 = self.balance("1200"), self.balance("1210"), self.balance("7300")
        usd_book0 = usd.statement_balance
        response = transfer(kes, usd, "13000", "98", description="Dollars for imports")
        out = BankTransaction.objects.filter(bank_account=kes,
                                             description__startswith="Dollars for imports").first()
        into = BankTransaction.objects.filter(bank_account=usd,
                                              description__startswith="Dollars for imports").first()
        self.expect("KES 13,000 out and USD 98 in are two lines of one entry",
                    response.status_code == 302 and out is not None and into is not None
                    and out.amount == D("13000") and into.amount == D("98")
                    and out.journal_entry_id == into.journal_entry_id,
                    str(self.form_errors(response)))
        self.expect("USD 98 at the filed 130 is KES 12,740; the 260 gap is realised exchange loss",
                    self.balance("1200") == bank0 - D("13000")
                    and self.balance("1210") == usd0 + D("12740")
                    and self.balance("7300") == loss0 + D("260"),
                    f"1200 {self.balance('1200') - bank0}, 1210 {self.balance('1210') - usd0}, "
                    f"7300 {self.balance('7300') - loss0}")
        self.expect("and the USD account's own book moves by USD 98",
                    usd.statement_balance == usd_book0 + D("98"))
        entry = out.journal_entry
        self.expect("the transfer's entry balances",
                    entry.status == "POSTED" and entry.is_balanced())

        response = self.admin_client.post(reverse("banking:transaction_delete", args=[into.pk]))
        entry.refresh_from_db()
        self.expect("deleting a transfer takes both sides off and reverses the entry",
                    not BankTransaction.objects.filter(pk__in=[out.pk, into.pk]).exists()
                    and entry.reversed_by_id is not None
                    and self.balance("1200") == bank0 and self.balance("1210") == usd0
                    and self.balance("7300") == loss0,
                    str(self.flashes(self.admin_client, response)))
        self.expect("and its reversal leaves nothing on either bank book",
                    not BankTransaction.objects.filter(journal_entry=entry.reversed_by).exists())
        # A transfer reversed behind the bank screen's back (the journal
        # screen) still leaves each bank book agreeing with its ledger
        # account, and deleting it afterwards tidies every line away.
        journal = JournalEntry.objects.filter(company=self.company,
                                              reference__startswith="JE-TRF-",
                                              reversed_by__isnull=True).first()
        if not self.expect("the shilling transfer is still standing", journal is not None):
            return
        bank0, book0 = self.balance("1200"), kes.statement_balance
        cash0, petty0 = self.balance("1110"), petty.statement_balance
        reversal = journal.reverse(user=self.admin)
        self.expect("reversed from the journal, each bank book gets the answering line",
                    BankTransaction.objects.filter(journal_entry=reversal).count() == 2
                    and self.balance("1200") - bank0 == kes.statement_balance - book0
                    and self.balance("1110") - cash0 == petty.statement_balance - petty0,
                    f"1200 {self.balance('1200') - bank0} / book {kes.statement_balance - book0}")
        side = BankTransaction.objects.filter(journal_entry=journal).first()
        self.admin_client.post(reverse("banking:transaction_delete", args=[side.pk]))
        self.expect("and deleting that transfer afterwards leaves no line behind",
                    not BankTransaction.objects.filter(
                        journal_entry__in=[journal, reversal]).exists())

    # ───────────────────── 16. statements are unwound newest first ─────────────

    def check_statement_chain(self):
        from apps.banking.models import Reconciliation

        self.section("16. An earlier statement is not reversed while a later one stands")
        petty = self.petty
        today = timezone.localdate()

        def statement(reference, day):
            self.admin_client.post(reverse("banking:reconciliation_create"), {
                "bank_account": petty.pk, "reference": reference,
                "statement_date": day.isoformat(),
                "closing_balance": str(petty.opening_balance), "notes": "Chain check only"})
            recon = Reconciliation.objects.filter(bank_account=petty, reference=reference).first()
            if recon:
                self.admin_client.post(reverse("banking:reconciliation_post", args=[recon.pk]))
                recon.refresh_from_db()
            return recon

        first = statement(f"PC-1-{self.stamp}", today - datetime.timedelta(days=2))
        second = statement(f"PC-2-{self.stamp}", today - datetime.timedelta(days=1))
        if not self.expect("two statements are posted",
                           first and second and first.status == second.status == "POSTED",
                           f"{getattr(first, 'status', None)} / {getattr(second, 'status', None)}"):
            return
        response = self.admin_client.post(reverse("banking:reconciliation_reverse",
                                                  args=[first.pk]))
        first.refresh_from_db()
        self.expect_flash(self.admin_client, response, f"Reverse {second.reference} first",
                          "reversing the earlier one is refused, naming the later one")
        self.expect("and the earlier statement is still posted", first.status == "POSTED")
        self.admin_client.post(reverse("banking:reconciliation_reverse", args=[second.pk]))
        self.admin_client.post(reverse("banking:reconciliation_reverse", args=[first.pk]))
        first.refresh_from_db()
        second.refresh_from_db()
        self.expect("newest first, both reverse",
                    first.status == "REVERSED" and second.status == "REVERSED",
                    f"{first.status} / {second.status}")

    # ───────────────────── fix3 helpers ──────────────────────────────────────

    def book_agrees(self, bank):
        """The independent book-vs-ledger check (as the testers' bank_check).

        A shilling bank account's book — its opening balance plus every line
        to today — must equal its ledger account as at today, and every
        posted entry that moves the ledger account (other than an OPENING
        one, which the opening balance stands for, and a reversed pair) must
        have exactly one line on the bank book.
        """
        from collections import defaultdict
        from apps.accounting.models import JournalEntry, JournalEntryLine
        from apps.banking.models import BankTransaction

        bank.refresh_from_db()
        today = timezone.localdate()
        ledger = bank.gl_account.balance_as_at(today)
        book = bank.statement_balance
        net = defaultdict(lambda: Z)
        for line in JournalEntryLine.objects.filter(
                account=bank.gl_account, journal_entry__status="POSTED").select_related(
                "journal_entry"):
            if line.journal_entry.entry_type == "OPENING":
                continue
            net[line.journal_entry_id] += line.debit_amount - line.credit_amount
        lines = defaultdict(int)
        for txn in BankTransaction.objects.filter(bank_account=bank):
            lines[txn.journal_entry_id] += 1
        missing = []
        for pk, value in net.items():
            if not value or pk in lines:
                continue
            entry = JournalEntry.objects.get(pk=pk)
            original = JournalEntry.objects.filter(reversed_by_id=pk).first()
            if (entry.reversed_by_id and entry.reversed_by_id not in lines) or \
                    (original and original.pk not in lines):
                continue
            missing.append(entry.reference)
        dups = [pk for pk, n in lines.items() if n > 1 or pk is None]
        ok = book == ledger and not missing and not dups
        return ok, f"book {book} / ledger {ledger}, missing {missing}, dups {len(dups)}"

    def bank_form(self, name, gl, opening="0", currency="KES", kind="CURRENT", **extra):
        data = {"name": name, "bank_name": "Fix Bank", "account_number": name[:8],
                "account_type": kind, "currency": currency, "branch": "",
                "gl_account": gl.pk, "opening_balance": opening}
        data.update(extra)
        return data

    def new_gl(self, code, name, kind="BANK"):
        from apps.accounting.models import Account
        return Account.objects.create(company=self.company, code=code, name=name,
                                      account_type=kind, parent=self.account("1000"))

    # ───────────────────── 17. one token, one action ──────────────────────────

    def check_token_kinds(self):
        from apps.pos.models import POSSale

        self.section("17. A form token used for something else is refused, not a 500")
        client = self.c_nbo
        response, sale = self.ring(client, {self.milk: "2"}, payment_method="CASH")
        if not self.expect("a cash ticket rings for the token checks", sale is not None,
                           str(self.flash(response))):
            return
        used = sale.submission_token
        line = sale.lines.first()
        response = client.post(reverse("pos:sale_refund", args=[sale.pk]),
                               {f"refund_qty_{line.pk}": "1", "refund_token": used})
        self.expect("a refund sent with the sale's own terminal token is not a server error",
                    response.status_code == 302, f"HTTP {response.status_code}")
        self.expect_flash(client, response, "already used to ring",
                          "and is refused with a message naming the ticket")
        self.expect("nothing was refunded",
                    not POSSale.objects.filter(refund_of=sale).exists())

        response, back = self.refund(client, sale, {self.milk: "1"})
        if not self.expect("a proper refund goes through", back is not None,
                           str(self.flashes(client, response))):
            return
        count = POSSale.objects.filter(session__company=self.company).count()
        response = client.post(reverse("pos:terminal"), {
            f"qty_{self.milk.pk}": "1", "payment_method": "CASH",
            "ticket_token": back.submission_token})
        self.expect("a terminal post with a spent refund token is not a server error",
                    response.status_code == 200, f"HTTP {response.status_code}")
        self.expect("and rings nothing, saying the token went on a refund",
                    POSSale.objects.filter(session__company=self.company).count() == count
                    and any("already used for refund" in m for m in self.flash(response)),
                    str(self.flash(response)))

        response, other = self.ring(client, {self.milk: "1"}, payment_method="CASH")
        response = client.post(reverse("pos:sale_refund", args=[other.pk]), {
            f"refund_qty_{other.lines.first().pk}": "1",
            "refund_token": back.submission_token})
        self.expect("another sale's refund token is refused on this sale",
                    response.status_code == 302
                    and not POSSale.objects.filter(refund_of=other).exists(),
                    f"HTTP {response.status_code}")
        self.expect_flash(client, response, "of another ticket",
                          "with a message saying it belonged to another ticket's refund")
        response = client.post(reverse("pos:sale_refund", args=[sale.pk]), {
            f"refund_qty_{line.pk}": "1", "refund_token": back.submission_token})
        self.expect_flash(client, response, "was already refunded from this form",
                          "the same refund form sent again still hands its refund back")

    # ───────────────────── 18. a bank account's history ───────────────────────

    def check_account_history(self):
        from apps.accounting.models import JournalEntry
        from apps.banking.models import BankAccount, BankTransaction

        self.section("18. A bank account's opening, its history and what is locked")
        client = self.admin_client
        gl = self.new_gl("1250", "Fix3 Bank")
        capital0 = self.balance("3100")
        response = client.post(reverse("banking:account_create"),
                               self.bank_form("Fix3 Bank", gl, opening="1000"))
        bank = BankAccount.objects.filter(company=self.company, name="Fix3 Bank").first()
        if not self.expect("a bank account with an opening balance is created", bank is not None,
                           str(self.form_errors(response))):
            return
        self.fix3 = bank
        entry = bank.opening_entry
        self.expect("its opening balance is posted Dr 1250 / Cr 3100",
                    entry is not None and entry.entry_type == "OPENING"
                    and entry.status == "POSTED"
                    and self.balance("1250") == D("1000")
                    and self.balance("3100") == capital0 - D("1000"),
                    f"1250 {self.balance('1250')}, 3100 moved "
                    f"{self.balance('3100') - capital0}")
        ok, detail = self.book_agrees(bank)
        self.expect("and the bank book agrees with the ledger from the start", ok, detail)
        edit = reverse("banking:account_edit", args=[bank.pk])
        response = client.post(edit, self.bank_form("Fix3 Bank", gl, opening="5000"))
        bank.refresh_from_db()
        self.expect("the opening balance is locked after creation",
                    response.status_code == 200 and "opening_balance" in self.form_errors(response)
                    and bank.opening_balance == D("1000"), str(self.form_errors(response)))
        other_gl = self.new_gl("1260", "Fix3 Spare")
        response = client.post(edit, self.bank_form("Fix3 Bank", other_gl, opening="1000"))
        bank.refresh_from_db()
        self.expect("its ledger account cannot move once it has history",
                    response.status_code == 200 and "gl_account" in self.form_errors(response)
                    and bank.gl_account_id == gl.pk, str(self.form_errors(response)))
        response = client.post(edit, self.bank_form("Fix3 Bank", gl, opening="1000",
                                                    currency="USD"))
        bank.refresh_from_db()
        self.expect("nor its currency",
                    response.status_code == 200 and "currency" in self.form_errors(response)
                    and bank.currency == "KES", str(self.form_errors(response)))
        response = client.post(edit, self.bank_form("Fix3 Bank renamed", gl, opening="1000"))
        bank.refresh_from_db()
        self.expect("a rename is still fine", response.status_code == 302
                    and bank.name == "Fix3 Bank renamed", str(self.form_errors(response)))
        # A spare account with no history may still be moved.
        response = client.post(reverse("banking:account_create"),
                               self.bank_form("Fix3 Spare", other_gl))
        spare = BankAccount.objects.filter(company=self.company, name="Fix3 Spare").first()
        gl3 = self.new_gl("1270", "Fix3 Third")
        response = client.post(reverse("banking:account_edit", args=[spare.pk]),
                               self.bank_form("Fix3 Spare", gl3))
        spare.refresh_from_db()
        self.expect("an account with no history may still change its ledger account",
                    spare.gl_account_id == gl3.pk, str(self.form_errors(response)))

        # The till drawer's ledger account has carried cash tickets all run.
        drawer_history = self.balance("1100")
        response = client.post(reverse("banking:account_create"), self.bank_form(
            "Till drawer", self.account("1100"), kind="PETTY_CASH"))
        drawer = BankAccount.objects.filter(company=self.company, name="Till drawer").first()
        if not self.expect("a bank account on a ledger account with history is created",
                           drawer is not None, str(self.form_errors(response))):
            return
        brought = BankTransaction.objects.filter(bank_account=drawer, origin="LEDGER").count()
        ok, detail = self.book_agrees(drawer)
        self.expect("its earlier movements are brought onto its bank book, one per entry",
                    brought > 0 and ok, f"{brought} line(s); {detail}")
        from apps.banking import bankbook
        self.expect("bringing them in again adds nothing", bankbook.backfill(drawer) == [])
        self.drawer = drawer

        # An opening reversed from the journal screen leaves the book agreeing.
        gl4 = self.new_gl("1280", "Fix3 Reversed")
        client.post(reverse("banking:account_create"),
                    self.bank_form("Fix3 Reversed", gl4, opening="300"))
        rev = BankAccount.objects.filter(company=self.company, name="Fix3 Reversed").first()
        if rev and rev.opening_entry:
            rev.opening_entry.reverse(user=self.admin)
            ok, detail = self.book_agrees(rev)
            self.expect("an opening reversed from the journal is answered on the bank book",
                        ok and self.balance("1280") == Z, detail)
        # An opening already typed as a journal is not booked twice.
        gl5 = self.new_gl("1290", "Fix3 Journal-opened")
        je = JournalEntry.objects.create(company=self.company, reference=f"OPEN-{self.stamp}",
                                         entry_type="OPENING",
                                         posting_date=timezone.localdate(), notes="opening")
        from apps.accounting.models import JournalEntryLine
        JournalEntryLine.objects.create(journal_entry=je, account=gl5, debit_amount=D("700"),
                                        credit_amount=Z)
        JournalEntryLine.objects.create(journal_entry=je, account=self.account("3100"),
                                        debit_amount=Z, credit_amount=D("700"))
        je.post(user=self.admin)
        response = client.post(reverse("banking:account_create"),
                               self.bank_form("Fix3 Typed", gl5, opening="900"))
        self.expect("an opening that disagrees with the ledger's own opening is refused",
                    response.status_code == 200 and not BankAccount.objects.filter(
                        company=self.company, name="Fix3 Typed").exists(),
                    str(self.form_errors(response)))
        client.post(reverse("banking:account_create"), self.bank_form("Fix3 Typed", gl5))
        typed = BankAccount.objects.filter(company=self.company, name="Fix3 Typed").first()
        ok, detail = self.book_agrees(typed) if typed else (False, "not created")
        self.expect("left at 0 it opens from the ledger's 700, booked once",
                    typed is not None and typed.opening_balance == D("700")
                    and typed.opening_entry_id is None and self.balance("1290") == D("700")
                    and ok, detail)

    # ───────────────────── 19. draft statements ───────────────────────────────

    def check_draft_statements(self):
        from apps.banking.models import Reconciliation

        self.section("19. Draft statements: re-opened, edited, deleted, posted with notes")
        bank = getattr(self, "fix3", None)
        if bank is None:
            return
        client = self.admin_client
        today = timezone.localdate()

        def start(reference, day, closing, notes=""):
            response = client.post(reverse("banking:reconciliation_create"), {
                "bank_account": bank.pk, "reference": reference,
                "statement_date": day.isoformat(), "closing_balance": str(closing),
                "notes": notes})
            return response, Reconciliation.objects.filter(
                bank_account=bank, reference=reference).exclude(status="REVERSED").first()

        response, later = start("FX-LATER", today, "1000")
        response, middle = start("FX-MID", today - datetime.timedelta(days=1), "1000")
        response, early = start("FX-EARLY", today - datetime.timedelta(days=2), "1250")
        if not self.expect("three drafts are started", later and middle and early):
            return
        self.expect("each opens on the account's opening balance",
                    later.opening_balance == early.opening_balance == D("1000"))
        response = client.post(reverse("banking:reconciliation_post", args=[early.pk]))
        early.refresh_from_db()
        self.expect("out of balance and with no notes, posting is refused",
                    early.status == "DRAFT")
        response = client.post(reverse("banking:reconciliation_post", args=[early.pk]),
                               {"notes": "Bank interest not yet entered"})
        early.refresh_from_db()
        self.expect("notes sent with the post let it post",
                    early.status == "POSTED" and early.notes == "Bank interest not yet entered",
                    str(self.flashes(client, response)))
        page = client.get(reverse("banking:reconciliation_detail", args=[later.pk]))
        self.expect("a later draft shown now opens on the posted statement's 1,250",
                    page.context and page.context["opening"] == D("1250"),
                    str(page.context and page.context["opening"]))

        response = client.post(reverse("banking:reconciliation_edit", args=[later.pk]), {
            "statement_date": today.isoformat(), "closing_balance": "1250",
            "notes": "Typo fixed"})
        later.refresh_from_db()
        self.expect("a draft's closing balance and notes can be edited",
                    response.status_code == 302 and later.closing_balance == D("1250")
                    and later.notes == "Typo fixed" and later.opening_balance == D("1250"),
                    str(self.form_errors(response)))
        response = client.post(reverse("banking:reconciliation_edit", args=[later.pk]), {
            "statement_date": (today - datetime.timedelta(days=5)).isoformat(),
            "closing_balance": "1250", "notes": ""})
        later.refresh_from_db()
        self.expect("but not moved beneath the posted statement",
                    response.status_code == 200 and later.statement_date == today,
                    str(self.form_errors(response)))
        response = client.post(reverse("banking:reconciliation_post", args=[later.pk]))
        later.refresh_from_db()
        self.expect("the edited draft posts, balanced", later.status == "POSTED",
                    str(self.flashes(client, response)))
        response = client.post(reverse("banking:reconciliation_post", args=[middle.pk]),
                               {"notes": "Late"})
        middle.refresh_from_db()
        self.expect("a draft dated beneath a posted statement cannot be posted",
                    middle.status == "DRAFT")
        self.expect_flash(client, response, "run forward only", "and is told why")
        response, back_dated = start("FX-OLD", today - datetime.timedelta(days=10), "1000")
        self.expect("nor can a new statement be started beneath it",
                    back_dated is None and "statement_date" in self.form_errors(response),
                    str(self.form_errors(response)))
        response = client.post(reverse("banking:reconciliation_delete", args=[middle.pk]))
        self.expect("a draft can be deleted",
                    not Reconciliation.objects.filter(pk=middle.pk).exists(),
                    str(self.flashes(client, response)))
        response = client.post(reverse("banking:reconciliation_delete", args=[later.pk]))
        self.expect("a posted statement cannot be",
                    Reconciliation.objects.filter(pk=later.pk, status="POSTED").exists())
        response = client.get(reverse("banking:reconciliation_edit", args=[later.pk]))
        self.expect("nor edited", response.status_code == 302, f"HTTP {response.status_code}")
        client.post(reverse("banking:reconciliation_reverse", args=[later.pk]))
        client.post(reverse("banking:reconciliation_reverse", args=[early.pk]))

    # ───────────────────── 20. dated after today ──────────────────────────────

    def check_future_lines(self):
        from apps.accounting.models import JournalEntry, JournalEntryLine
        from apps.banking import services

        self.section("20. Lines dated after today stay out of today's balance")
        bank = getattr(self, "fix3", None)
        if bank is None:
            return
        tomorrow = timezone.localdate() + datetime.timedelta(days=4)
        je = JournalEntry.objects.create(company=self.company, reference=f"FUT-{self.stamp}",
                                         entry_type="MANUAL", posting_date=tomorrow,
                                         notes="Receipt dated ahead")
        JournalEntryLine.objects.create(journal_entry=je, account=bank.gl_account,
                                        debit_amount=D("400"), credit_amount=Z)
        JournalEntryLine.objects.create(journal_entry=je, account=self.account("4400"),
                                        debit_amount=Z, credit_amount=D("400"))
        je.post(user=self.admin)
        line = bank.transactions.filter(journal_entry=je).first()
        self.expect("a future-dated entry still reaches the bank book", line is not None)
        ok, detail = self.book_agrees(bank)
        self.expect("but the balance shown is as at today, book and ledger alike", ok, detail)
        self.expect("it is not offered on a statement",
                    line not in list(services.outstanding(bank, tomorrow)))
        page = self.admin_client.get(reverse("banking:account_detail", args=[bank.pk]))
        self.expect("it is listed apart on the account's page",
                    page.context and line in list(page.context["future"])
                    and line not in list(page.context["transactions"]))

    # ───────────────────── 21. cash cannot go below zero ──────────────────────

    def check_cash_floor(self):
        from apps.banking.models import BankAccount

        self.section("21. A cash box cannot pay out notes it does not have")
        bank = getattr(self, "fix3", None)
        # A box of its own with a 500 float, so the figures are this check's.
        self.admin_client.post(reverse("banking:account_create"), self.bank_form(
            "Fix3 cash box", self.new_gl("1120", "Fix3 Cash Box", kind="CASH"),
            kind="PETTY_CASH"))
        petty = BankAccount.objects.filter(company=self.company, name="Fix3 cash box").first()
        if not self.expect("a fresh cash box is opened", petty is not None):
            return
        self.manual_line(petty, "CREDIT", "500", "Float", self.account("3100"))
        response, ok_spend = self.manual_line(petty, "DEBIT", "500", "All of it",
                                              self.account("6600"))
        self.expect("spending exactly what the box holds is fine", ok_spend is not None,
                    str(self.form_errors(response)))
        self.manual_line(petty, "CREDIT", "500", "Float again", self.account("3100"))
        held = petty.statement_balance
        response, spend = self.manual_line(petty, "DEBIT", str(held + D("1")), "Too much tea",
                                           self.account("6600"))
        self.expect(f"a spend of {held + 1} from petty cash holding {held} is refused",
                    spend is None and response.status_code == 200, f"HTTP {response.status_code}")
        response = self.admin_client.post(reverse("banking:transfer_create"), {
            "from_account": petty.pk, "to_account": self.kes.pk,
            "transfer_date": timezone.localdate().isoformat(),
            "amount_sent": str(held + D("1000")), "amount_received": "", "reference": "",
            "description": "Overdraw petty"})
        self.expect("so is a transfer out of it larger than it holds",
                    response.status_code == 200
                    and petty.statement_balance == held, f"HTTP {response.status_code}")
        day = timezone.localdate() - datetime.timedelta(days=30)
        response, early = self.manual_line(petty, "DEBIT", "1", "Back-dated tea",
                                           self.account("6600"), day=day)
        self.expect("and a back-dated spend before the box held anything",
                    early is None, f"HTTP {response.status_code}")
        if bank is not None:
            owed = bank.statement_balance + D("500")
            response, over = self.manual_line(bank, "DEBIT", str(owed), "Overdraft",
                                              self.account("7100"))
            self.expect("a bank account may go overdrawn", over is not None
                        and bank.statement_balance < 0, str(self.form_errors(response)))

    # ───────────────────── 22. where till takings land ────────────────────────

    def check_takings_accounts(self):
        from apps.banking.models import BankAccount, BankTransaction

        self.section("22. Card and M-Pesa takings land in the flagged bank account")
        bank = getattr(self, "fix3", None)
        if bank is None:
            return
        client = self.admin_client
        bank.refresh_from_db()
        response = client.post(reverse("banking:account_edit", args=[bank.pk]), self.bank_form(
            bank.name, bank.gl_account, opening=str(bank.opening_balance),
            receives_pos_card="on"))
        bank.refresh_from_db()
        self.expect("a bank account is flagged to receive card takings",
                    bank.receives_pos_card, str(self.form_errors(response)))
        response = client.post(reverse("banking:account_edit", args=[self.kes.pk]),
                               self.bank_form("Equity KES", self.account("1200"),
                                              opening=str(self.kes.opening_balance),
                                              receives_pos_card="on"))
        self.expect("a second card account is refused",
                    "receives_pos_card" in self.form_errors(response),
                    str(self.form_errors(response)))
        page = client.get(reverse("banking:account_edit", args=[bank.pk])).content.decode()
        self.expect("the flags are on the bank account form",
                    "receives_pos_card" in page and "receives_pos_mpesa" in page)

        gl0, bank1200 = self.balance("1250"), self.balance("1200")
        response, card = self.ring(self.c_nbo, {self.milk: "1"}, payment_method="CARD")
        line = BankTransaction.objects.filter(journal_entry=card.journal_entry).first() \
            if card else None
        self.expect("a card ticket debits the flagged account's ledger account",
                    card is not None and self.balance("1250") == gl0 + card.total_amount
                    and self.balance("1200") == bank1200,
                    f"1250 {self.balance('1250') - gl0}, 1200 {self.balance('1200') - bank1200}")
        self.expect("and lands on its bank book", line is not None and line.bank_account_id == bank.pk)
        response, back = self.refund(self.c_nbo, card, {self.milk: "1"})
        self.expect("its refund comes back out of the same account",
                    back is not None and self.balance("1250") == gl0,
                    f"1250 {self.balance('1250') - gl0}")
        response, mpesa = self.ring(self.c_nbo, {self.milk: "1"}, payment_method="MPESA")
        self.expect("with no M-Pesa account flagged, M-Pesa still falls back to 1200",
                    mpesa is not None and self.balance("1200") == bank1200 + mpesa.total_amount)
        ok, detail = self.book_agrees(bank)
        self.expect("the flagged account's book still agrees with its ledger", ok, detail)
        BankAccount.objects.filter(pk=bank.pk).update(receives_pos_card=False)

    # ───────────────────── 23. a refund at another branch ─────────────────────

    def check_cross_branch_refund_cost(self):
        from apps.inventory.models import StockMovement

        self.section("23. A refund at another branch comes back at the cost it was sold at")
        msa = self.session_of(self.msa_cashier) or self.open_till(self.c_msa, self.msa_cashier)
        if not self.expect("Mombasa has a till open", msa is not None):
            return
        stock0, cogs0 = self.balance("1400"), self.balance("5000")
        response, sale = self.ring(self.c_nbo, {self.sugar: "2"}, payment_method="CASH")
        if not self.expect("Nairobi sells two sugar", sale is not None):
            return
        issued = StockMovement.objects.get(reference=sale.reference, movement_type="DELIVERY")
        response, back = self.refund(self.c_msa, sale, {self.sugar: "2"})
        if not self.expect("Mombasa takes both back", back is not None,
                           str(self.flashes(self.c_msa, response))):
            return
        returned = StockMovement.objects.get(reference=back.reference, movement_type="RETURN_IN")
        self.expect("they come in at the cost they went out at, not Mombasa's shelf average",
                    returned.warehouse_id == self.wh_msa.pk
                    and returned.unit_cost == issued.unit_cost,
                    f"{returned.unit_cost} in against {issued.unit_cost} out")
        self.expect("so the full refund leaves inventory and cost of sales where they were",
                    self.balance("1400") == stock0 and self.balance("5000") == cogs0,
                    f"1400 {self.balance('1400') - stock0}, 5000 {self.balance('5000') - cogs0}")

    # ───────────────────── 24. the cash floor, everywhere ─────────────────────

    def check_central_cash_floor(self):
        from django.core.exceptions import ValidationError
        from apps.accounting.models import JournalEntry, JournalEntryLine
        from apps.banking import services as bank_services
        from apps.banking.models import BankAccount, BankTransaction

        self.section("24. The cash floor is kept by the ledger itself, on every path")
        client = self.admin_client
        today = timezone.localdate()
        kes = getattr(self, "kes", None)
        if kes is None:
            return
        client.post(reverse("banking:account_create"), self.bank_form(
            "Floor box", self.new_gl("1130", "Floor Box", kind="CASH"), kind="PETTY_CASH"))
        box = BankAccount.objects.filter(company=self.company, name="Floor box").first()
        if not self.expect("a fresh petty-cash box is opened at nothing", box is not None):
            return

        def journal(lines, day=None, memo="Floor journal"):
            entry = JournalEntry.objects.create(
                company=self.company, reference=f"JNL-FL-{JournalEntry.objects.count()}",
                entry_type="MANUAL", posting_date=day or today, notes=memo)
            for account, dr, cr in lines:
                JournalEntryLine.objects.create(journal_entry=entry, account=account,
                                                debit_amount=D(dr), credit_amount=D(cr))
            return entry

        def refused(entry):
            try:
                with transaction.atomic():
                    entry.post(user=self.admin)
            except ValidationError as exc:
                return exc.messages[0]
            return ""

        # A journal -- any screen's entry -- is held to the floor as it posts.
        why = refused(journal([(self.account("6600"), "100", "0"),
                               (box.gl_account, "0", "100")]))
        self.expect("a journal paying 100 out of an empty petty-cash box is refused",
                    "cannot pay out notes" in why and "Floor box" in why, why)
        self.expect("and the refused entry is left a draft",
                    not JournalEntry.objects.filter(notes="Floor journal",
                                                    status="POSTED").exists())

        # A Cash ledger account with no bank account behind it at all.
        loose = self.new_gl("1140", "Loose Cash", kind="CASH")
        why = refused(journal([(self.account("6600"), "50", "0"), (loose, "0", "50")]))
        self.expect("a Cash ledger account with no bank account is held to the floor too",
                    "1140 Loose Cash" in why, why)

        # A deposit that was spent cannot be deleted -- and saying so is not a 500.
        _, deposit = self.manual_line(box, "CREDIT", "500", "Floor deposit", self.account("3100"))
        _, spend = self.manual_line(box, "DEBIT", "400", "Floor spend", self.account("6600"))
        if not self.expect("the box takes a 500 deposit and a 400 spend",
                           deposit is not None and spend is not None):
            return
        response = client.post(reverse("banking:transaction_delete", args=[deposit.pk]))
        self.expect("deleting the spent deposit is refused with a message, not a 500",
                    response.status_code == 302
                    and str(deposit.pk) in (response.get("Location") or ""),
                    f"HTTP {response.status_code} {response.get('Location')}")
        self.expect_flash(client, response, "Deleting this deposit would leave Floor box",
                          "and the message names the box")
        self.expect("the deposit and its entry are still there",
                    BankTransaction.objects.filter(pk=deposit.pk, journal_entry__isnull=False,
                                                   journal_entry__reversed_by__isnull=True)
                    .exists())

        def edit(line, **change):
            data = {"bank_account": box.pk, "transaction_date": today.isoformat(),
                    "value_date": "", "transaction_type": "CREDIT", "amount": "500",
                    "counter_account": self.account("3100").pk,
                    "description": "Floor deposit", "reference": ""}
            data.update(change)
            return client.post(reverse("banking:transaction_edit", args=[line.pk]), data)

        # Moving the spent deposit to another account checks the account it left.
        response = edit(deposit, bank_account=kes.pk)
        deposit.refresh_from_db()
        self.expect("moving the spent deposit to the bank is refused",
                    response.status_code == 200 and deposit.bank_account_id == box.pk
                    and box.statement_balance == D("100"),
                    f"HTTP {response.status_code}, box {box.statement_balance}")

        # Re-dating money: a top-up dated two days ago, spent yesterday, cannot be
        # moved to today -- the box would be short in between.
        two, one = today - datetime.timedelta(days=2), today - datetime.timedelta(days=1)
        _, early = self.manual_line(box, "CREDIT", "300", "Floor early top-up",
                                    self.account("3100"), day=two)
        _, used = self.manual_line(box, "DEBIT", "250", "Floor early spend",
                                   self.account("6600"), day=one)
        if self.expect("a back-dated top-up and its spend are recorded",
                       early is not None and used is not None):
            response = edit(early, transaction_date=today.isoformat(), amount="300",
                            description="Floor early top-up")
            early.refresh_from_db()
            self.expect("re-dating the top-up to after its spend is refused",
                        response.status_code == 200 and early.transaction_date == two,
                        f"HTTP {response.status_code}, dated {early.transaction_date}")
            earlier = two - datetime.timedelta(days=1)
            response = edit(early, transaction_date=earlier.isoformat(), amount="300",
                            description="Floor early top-up")
            early.refresh_from_db()
            self.expect("re-dating it earlier is fine",
                        response.status_code == 302 and early.transaction_date == earlier,
                        str(self.form_errors(response)))

        # A transfer into the box, spent, then deleted or reversed on the journal screen.
        client.post(reverse("banking:transfer_create"), {
            "from_account": kes.pk, "to_account": box.pk, "transfer_date": today.isoformat(),
            "amount_sent": "1000", "amount_received": "", "reference": "",
            "description": "Floor top-up"})
        _, big = self.manual_line(box, "DEBIT", str(box.statement_balance), "Floor big spend",
                                  self.account("6600"))
        moved_in = BankTransaction.objects.filter(bank_account=box, origin="TRANSFER").first()
        if self.expect("a 1000 transfer into the box is spent to nothing",
                       moved_in is not None and big is not None
                       and box.statement_balance == 0, str(box.statement_balance)):
            response = client.post(reverse("banking:transaction_delete", args=[moved_in.pk]))
            self.expect_flash(client, response, "Deleting this transfer would leave Floor box",
                              "deleting the spent transfer is refused")
            response = client.post(reverse("accounting:journal_entry_reverse",
                                           args=[moved_in.journal_entry_id]))
            moved_in.journal_entry.refresh_from_db()
            self.expect("and so is reversing its entry from the journal screen",
                        moved_in.journal_entry.reversed_by_id is None
                        and box.statement_balance == 0, str(self.flashes(client, response)))
        ok, detail = self.book_agrees(box)
        self.expect("the box's book still agrees with its ledger, and is not below zero",
                    ok and box.statement_balance >= 0, detail)

        # A line dated before the account's opening takes the opening back with it.
        client.post(reverse("banking:account_create"), self.bank_form(
            "Opened today", self.new_gl("1150", "Opened Today", kind="CASH"),
            opening="1000", kind="PETTY_CASH"))
        opened = BankAccount.objects.filter(company=self.company, name="Opened today").first()
        back = today - datetime.timedelta(days=4)
        if self.expect("a box opened today with 1000",
                       opened is not None and opened.opening_entry_id is not None):
            _, old = self.manual_line(opened, "DEBIT", "800", "Last week's tea",
                                      self.account("6600"), day=back)
            opened.refresh_from_db()
            self.expect("a spend dated four days back is taken", old is not None)
            self.expect("and the opening moves back beneath it",
                        opened.opening_entry.posting_date == back,
                        str(opened.opening_entry.posting_date))
            book = opened.book_balance_at(back)
            ledger = bank_services.gl_balance_at(opened.gl_account, back)
            self.expect("so the book and the ledger agree on that day",
                        book == ledger == D("200"), f"book {book}, ledger {ledger}")
            why = opened.opening_entry.generic_reversal_refusal()
            self.expect("the journal screen calls it 'an opening balance entry'",
                        "as an opening balance entry" in why, why)

        # A card refund goes back through the account the sale was banked in.
        bank = getattr(self, "fix3", None)
        if bank is not None:
            BankAccount.objects.filter(company=self.company).update(receives_pos_card=False)
            BankAccount.objects.filter(pk=bank.pk).update(receives_pos_card=True)
            banked = bank.gl_account.code
            response, card = self.ring(self.c_nbo, {self.milk: "1"}, payment_method="CARD")
            BankAccount.objects.filter(pk=bank.pk).update(receives_pos_card=False)
            BankAccount.objects.filter(pk=kes.pk).update(receives_pos_card=True)
            there, here = self.balance(banked), self.balance("1200")
            back_ticket = None
            if card is not None:
                response, back_ticket = self.refund(self.c_nbo, card, {self.milk: "1"})
            self.expect("after the card flag moves, the refund still comes out of the "
                        "account the sale was banked in",
                        back_ticket is not None
                        and self.balance(banked) == there - card.total_amount
                        and self.balance("1200") == here,
                        f"{banked} {self.balance(banked) - there}, "
                        f"1200 {self.balance('1200') - here}")
            BankAccount.objects.filter(pk=kes.pk).update(receives_pos_card=False)

        # The till list is ordered, so its pages never shuffle.
        response = client.get(reverse("pos:session_list"))
        ctx = response.context
        listed = list(ctx["sessions"]) if ctx else []
        self.expect("the till list pages an ordered list, newest first",
                    response.status_code == 200 and ctx["paginator"].object_list.ordered
                    and listed == sorted(listed, key=lambda s: s.opened_at, reverse=True),
                    f"HTTP {response.status_code}")

    # ──────────────────────── 25-29. the full-screen till ────────────────────

    def entry_lines(self, entry):
        """{account code: debit - credit} on one journal entry."""
        out = {}
        if entry is None:
            return out
        for line in entry.lines.select_related("account"):
            out[line.account.code] = (out.get(line.account.code, Z)
                                      + line.debit_amount - line.credit_amount)
        return out

    def till_client(self, user, ip):
        client = Client(raise_request_exception=False, REMOTE_ADDR=ip)
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        return client

    def check_till_terminal(self):
        from apps.inventory.models import StockLevel
        from apps.pos.models import POSCashMovement

        self.section("25. The full-screen till still rings the old post, with a salesperson")
        self.till_cashier = self.make_user("cash.till", "POS_CASHIER", self.hq)
        self.till_sup = self.make_user("sup.till", "SALES_MANAGER", self.hq)
        cashier = self.till_client(self.till_cashier, "10.25.0.1")
        sup = self.till_client(self.till_sup, "10.25.0.2")
        self.c_till, self.c_sup = cashier, sup
        till = self.open_till(cashier, self.till_cashier, float_="1000")
        sup_till = self.open_till(sup, self.till_sup, float_="1000")
        if not self.expect("a cashier and a supervisor each open a till", till and sup_till):
            return
        page = cashier.get(reverse("pos:terminal"))
        body = page.content.decode("utf-8", "replace")
        self.expect("the terminal renders the till screen with its function keys",
                    page.status_code == 200 and 'data-fn="tender"' in body
                    and 'id="pos-products"' in body and "F12" in body, f"HTTP {page.status_code}")
        self.expect("a cashier sees Apply Discount disabled with the reason",
                    'data-fn="discount" aria-disabled="true"' in body)
        sup_body = sup.get(reverse("pos:terminal")).content.decode("utf-8", "replace")
        self.expect("a supervisor's Apply Discount is live",
                    'data-fn="discount" aria-disabled' not in sup_body)

        response, sale = self.ring(cashier, {self.milk: "2"}, payment_method="CASH",
                                   amount_tendered="200", salesperson=str(self.till_sup.pk),
                                   customer_reference="LPO-77", notes="till check")
        self.expect("the ticket post (qty_<pk>, ticket_token, tendered) still rings",
                    sale is not None and sale.total_amount == D("139.20")
                    and sale.change_given == D("60.80"), f"HTTP {response.status_code}")
        if sale:
            self.expect("and keeps the salesperson and the reference on the ticket",
                        sale.salesperson_id == self.till_sup.pk
                        and sale.customer_reference == "LPO-77")
            lines = self.entry_lines(sale.journal_entry)
            self.expect("and books Dr till 139.20 / Cr revenue 120 / Cr VAT 19.20",
                        lines.get("1100") == D("139.20") and lines.get("4200") == D("-120.00")
                        and lines.get("2200") == D("-19.20"), str(lines))
        outsider = self.make_user("not.staff")
        response, stranger = self.ring(cashier, {self.milk: "1"},
                                       salesperson=str(outsider.pk))
        self.expect("a salesperson from outside the company is refused, not rung",
                    stranger is None and response.status_code == 200
                    and any("salesperson" in m for m in self.flash(response)),
                    str(self.flash(response)))

        self.section("26. A line discount: supervisors only, booked at the discounted figure")
        response, refused = self.ring(cashier, {self.sugar: "2"}, **{f"disc_{self.sugar.pk}": "10"})
        self.expect("a cashier's discount is refused and nothing is rung",
                    refused is None and any("supervisor" in m.lower() for m in self.flash(response)),
                    str(self.flash(response)))
        response, zero = self.ring(cashier, {self.milk: "1"}, **{f"disc_{self.milk.pk}": "0"})
        self.expect("a zero discount from a cashier is no discount, and rings",
                    zero is not None and zero.discount_amount == Z)
        for bad in ("150", "-5", "abc", "100"):
            response, odd = self.ring(sup, {self.sugar: "1"}, **{f"disc_{self.sugar.pk}": bad})
            self.expect(f"a discount of {bad!r} is refused with a message",
                        odd is None and response.status_code == 200 and self.flash(response),
                        str(self.flash(response)))
        level = StockLevel.objects.get(product=self.sugar, warehouse=self.wh_hq)
        avg = level.avg_cost
        cogs0, inv0 = self.balance("5000"), self.balance("1400")
        response, disc = self.ring(sup, {self.sugar: "2", self.milk: "1"},
                                   **{f"disc_{self.sugar.pk}": "10"})
        if not self.expect("a supervisor rings 2 sugar at 10% off with a milk",
                           disc is not None, str(self.flash(response))):
            return
        line = disc.lines.get(product=self.sugar)
        self.expect("the line keeps 10% and 40.00 off 400.00; VAT on the 360.00",
                    line.discount_percent == D("10") and line.discount_amount == D("40.00")
                    and line.tax_amount == D("57.60") and line.line_total == D("417.60"),
                    f"{line.discount_percent}% {line.discount_amount} tax {line.tax_amount} "
                    f"total {line.line_total}")
        self.expect("the ticket totals: subtotal 420.00, discount 40.00, VAT 67.20, total 487.20",
                    disc.subtotal == D("420.00") and disc.discount_amount == D("40.00")
                    and disc.tax_amount == D("67.20") and disc.total_amount == D("487.20"),
                    f"{disc.subtotal} / {disc.discount_amount} / {disc.tax_amount} / "
                    f"{disc.total_amount}")
        lines = self.entry_lines(disc.journal_entry)
        self.expect("revenue and VAT are credited at the discounted amounts",
                    lines.get("1100") == D("487.20") and lines.get("4200") == D("-420.00")
                    and lines.get("2200") == D("-67.20") and sum(lines.values()) == Z,
                    str(lines))
        cost = (avg * 2).quantize(D("0.01")) + D("40.00")
        self.expect("cost of sales stays at cost — the discount never reaches it",
                    self.balance("5000") - cogs0 == cost and inv0 - self.balance("1400") == cost,
                    f"COGS +{self.balance('5000') - cogs0}, inventory "
                    f"-{inv0 - self.balance('1400')}, expected {cost}")
        response, back = self.refund(sup, disc, {self.sugar: "1"})
        self.expect("a refund of a discounted line pays back what was paid (208.80)",
                    back is not None and back.total_amount == D("208.80"),
                    str(back.total_amount if back else self.flashes(sup, response)))

        self.section("27. Payouts and pay-ins move the drawer and the ledger")
        till.refresh_from_db()
        drawer0, cash0, exp0 = till.expected_cash, self.balance("1100"), self.balance("6000")

        def move(client, session, direction, amount, reason="Delivery fare", account=None,
                 token=None, **extra):
            data = {"direction": direction, "amount": amount, "reason": reason,
                    "movement_token": token if token is not None else self.form_token(
                        client, reverse("pos:terminal"), "movement_token")}
            if account is not None:
                data["account"] = account
            data.update(extra)
            return client.post(reverse("pos:cash_movement", args=[session.pk]), data)

        import uuid
        token = uuid.uuid4().hex
        first = move(cashier, till, "OUT", "300", token=token)
        move(cashier, till, "OUT", "300", token=token)
        payouts = POSCashMovement.objects.filter(session=till, direction="OUT")
        out = payouts.first()
        self.expect("a payout is recorded once, however often the form is sent",
                    first.status_code == 302 and payouts.count() == 1, str(payouts.count()))
        if out:
            lines = self.entry_lines(out.journal_entry)
            self.expect("the payout books Dr expense (6000) 300 / Cr till (1100) 300",
                        lines == {"6000": D("300.00"), "1100": D("-300.00")}
                        and out.journal_entry.status == "POSTED"
                        and out.journal_entry.posting_date == timezone.localdate(), str(lines))
        till.refresh_from_db()
        self.expect("and the drawer is expected to hold 300 less",
                    till.expected_cash == drawer0 - D("300"),
                    f"{drawer0} -> {till.expected_cash}")
        self.expect("the ledger's till and expense accounts moved by the same 300",
                    cash0 - self.balance("1100") == D("300") and self.balance("6000") - exp0 == D("300"))
        before = POSCashMovement.objects.filter(session=till).count()
        response = move(cashier, till, "OUT", str(till.expected_cash + 1))
        self.expect("a payout larger than the drawer holds is refused",
                    POSCashMovement.objects.filter(session=till).count() == before
                    and any("cannot pay out" in m for m in self.flashes(cashier, response)))
        for label, amount, reason in [("a zero amount", "0", "x"), ("words", "lots", "x"),
                                      ("no reason", "10", "")]:
            response = move(cashier, till, "OUT", amount, reason=reason)
            self.expect(f"a payout with {label} is refused, not a server error",
                        response.status_code == 302
                        and POSCashMovement.objects.filter(session=till).count() == before)
        revenue = self.account("4200")
        response = move(cashier, till, "OUT", "10", account=str(revenue.pk))
        self.expect("a payout cannot be booked against a revenue account",
                    POSCashMovement.objects.filter(session=till).count() == before)
        response = move(cashier, till, "OUT", "10", token="")
        self.expect("a payout without its one-time token is refused",
                    POSCashMovement.objects.filter(session=till).count() == before)
        equity = self.account("3100")
        cap0 = self.balance("3100")
        move(cashier, till, "IN", "1000", reason="Float top-up from the owner",
             account=str(equity.pk))
        pay_in = POSCashMovement.objects.filter(session=till, direction="IN").first()
        lines = self.entry_lines(pay_in.journal_entry if pay_in else None)
        self.expect("a pay-in books Dr till (1100) 1000 / Cr float equity (3100) 1000",
                    lines == {"1100": D("1000.00"), "3100": D("-1000.00")}
                    and cap0 - self.balance("3100") == D("1000"), str(lines))
        till.refresh_from_db()
        self.expect("and the drawer is expected to hold 1000 more",
                    till.expected_cash == drawer0 - D("300") + D("1000"), str(till.expected_cash))
        report = self.c_till.get(reverse("pos:cash_up", args=[till.pk])).context["report"]
        self.expect("the cash-up shows float + cash sales - cash refunds - payouts + pay-ins",
                    report["paid_out"] == D("300.00") and report["paid_in"] == D("1000.00")
                    and report["expected_cash"] == report["opening_cash"] + report["cash_taken"]
                    - report["cash_given"] - D("300") + D("1000")
                    and report["expected_cash"] == till.expected_cash, str(report["expected_cash"]))
        page = cashier.get(reverse("pos:session_detail", args=[till.pk]))
        self.expect("the session page lists the payout and the pay-in",
                    out is not None and pay_in is not None
                    and out.reference in page.content.decode()
                    and pay_in.reference in page.content.decode())
        n = POSCashMovement.objects.filter(session=till).count()
        move(self.c_msa, till, "OUT", "5", token=uuid.uuid4().hex)
        self.expect("another cashier cannot pay money out of this drawer",
                    POSCashMovement.objects.filter(session=till).count() == n)
        response = move(self.c_acct, till, "OUT", "5", token=uuid.uuid4().hex)
        self.expect("an accountant is stopped at the door",
                    response.status_code == 403
                    and POSCashMovement.objects.filter(session=till).count() == n,
                    f"HTTP {response.status_code}")
        move(sup, till, "OUT", "20", reason="Milk for the office", token=uuid.uuid4().hex,
             next="session")
        self.expect("a supervisor can pay out of the cashier's drawer on their behalf",
                    POSCashMovement.objects.filter(session=till).count() == n + 1)
        till.refresh_from_db()
        counted = till.expected_cash
        cashier.post(reverse("pos:session_close", args=[till.pk]),
                     {"closing_cash": str(counted), "then": "cash_up"})
        till.refresh_from_db()
        self.expect("a drawer counted at float + sales - payouts + pay-ins closes with no variance",
                    till.status == "CLOSED" and till.variance == Z and not till.variance_entry_id,
                    f"variance {till.variance}")
        response = move(sup, till, "OUT", "5", token=uuid.uuid4().hex)
        self.expect("a closed drawer takes no payout",
                    POSCashMovement.objects.filter(session=till).count() == n + 1)

        self.section("28. The item lookup: this company's active goods, 25 a page")
        self.check_item_lookup(sup)

        self.section("29. Lock Station: unlock with the cashier's password, with the lockout")
        self.check_unlock()

        dr, cr = self.book_balances()
        self.expect("the book balances after the till checks", dr == cr, f"Dr {dr} / Cr {cr}")

    def check_item_lookup(self, client):
        from apps.inventory.models import Product, ProductCategory, UnitOfMeasure
        from apps.organizations.models import Company

        uom = UnitOfMeasure.objects.filter(company=self.company).first()
        drinks = ProductCategory.objects.create(company=self.company, name="DRINKS")
        carb = ProductCategory.objects.create(company=self.company, name="Carbonated", parent=drinks)
        sugar = ProductCategory.objects.create(company=self.company, name="Sugar", parent=carb)
        food = ProductCategory.objects.create(company=self.company, name="FOOD")
        made = []
        for i in range(30):
            cat = (drinks, carb, sugar)[i % 3]
            made.append(Product.objects.create(
                company=self.company, sku=f"LKP-{i:02d}", barcode=f"6001{i:04d}",
                name=f"Lookup soda {i:02d}", category=cat, unit_of_measure=uom,
                selling_price=D("16.50"), cost_price=D("10")))
        Product.objects.create(company=self.company, sku="LKP-FOOD", name="Lookup bread",
                               category=food, unit_of_measure=uom, selling_price=D("60"))
        Product.objects.create(company=self.company, sku="LKP-OLD", name="Lookup soda retired",
                               category=sugar, unit_of_measure=uom, selling_price=D("10"),
                               is_active=False)
        other = Company.objects.exclude(pk=self.company.pk).first()
        if other is not None:
            other_uom = (UnitOfMeasure.objects.filter(company=other).first()
                         or UnitOfMeasure.objects.create(company=other, name="Pieces",
                                                         abbreviation="PCS", is_base=True))
            Product.objects.create(company=other, sku="LKP-ELSEWHERE", name="Lookup soda abroad",
                                   unit_of_measure=other_uom, selling_price=D("1"))

        url = reverse("pos:item_lookup")
        data = client.get(url, {"q": "lookup soda"}).json()
        skus = {r["sku"] for r in data["results"]}
        self.expect("30 matches, 25 on the first page, two pages",
                    data["count"] == 30 and len(data["results"]) == 25 and data["num_pages"] == 2,
                    f"{data['count']} / {len(data['results'])} / {data['num_pages']}")
        self.expect("another company's product and a retired one are never listed",
                    "LKP-ELSEWHERE" not in skus and "LKP-OLD" not in skus)
        page2 = client.get(url, {"q": "lookup soda", "page": "2"}).json()
        self.expect("the second page holds the last 5",
                    page2["page"] == 2 and len(page2["results"]) == 5
                    and not ({r["sku"] for r in page2["results"]} & skus))
        junk = client.get(url, {"q": "lookup soda", "page": "abc"}).json()
        self.expect("a page that is not a number answers page 1", junk["page"] == 1)
        row = data["results"][0]
        self.expect("each row carries sku, barcode, name, unit, price, tax rate, on hand, image",
                    all(k in row for k in ("sku", "barcode", "name", "unit", "price", "tax_rate",
                                           "on_hand", "image")))
        top = client.get(url, {"category": str(drinks.pk)}).json()
        mid = client.get(url, {"category": str(carb.pk)}).json()
        leaf = client.get(url, {"category": str(sugar.pk)}).json()
        self.expect("a category takes in everything under it (30 / 20 / 10)",
                    top["count"] == 30 and mid["count"] == 20 and leaf["count"] == 10,
                    f"{top['count']} / {mid['count']} / {leaf['count']}")
        exact = client.get(url, {"q": "60010007", "exact": "1"}).json()
        self.expect("a scanned barcode finds exactly its product",
                    exact["count"] == 1 and exact["results"][0]["sku"] == "LKP-07")
        shelf = client.get(url, {"q": "Sugar 1kg", "warehouse": str(self.wh_msa.pk),
                                 "stock": "all"}).json()
        self.expect("a Head Office till cannot count on another branch's shelf, but the price "
                    "check sees stock everywhere",
                    shelf["warehouse"] and shelf["warehouse"]["id"] == str(self.wh_hq.pk)
                    and len(shelf["results"][0]["stock"]) >= 2, str(shelf.get("warehouse")))
        response = self.c_acct.get(url, {"q": "lookup"})
        self.expect("an accountant cannot use the till's lookup",
                    response.status_code == 403, f"HTTP {response.status_code}")
        response = self.c_till.get(url, {"q": "lookup"})
        self.expect("a cashier can", response.status_code == 200, f"HTTP {response.status_code}")

    def check_unlock(self):
        from apps.pos.models import POSSale

        user = self.make_user("cash.lock", "POS_CASHIER", self.hq)
        client = self.till_client(user, "10.29.0.9")
        till = self.open_till(client, user, float_="100")
        if not self.expect("a cashier opens a till to lock", till is not None):
            return
        response = client.post(reverse("pos:lock"))
        self.expect("F7 locks the station", response.status_code == 200 and response.json()["ok"])
        page = client.get(reverse("pos:terminal"))
        self.expect("a reload still shows the lock screen",
                    page.context and page.context["locked"] is True)
        before = POSSale.objects.filter(session=till).count()
        self.ring(client, {self.milk: "1"})
        self.expect("a locked till rings nothing",
                    POSSale.objects.filter(session=till).count() == before)
        wrong = client.post(reverse("pos:unlock"), {"password": "not-it"})
        self.expect("a wrong password is refused and the till stays locked",
                    wrong.status_code == 403 and client.get(reverse("pos:terminal")).context["locked"])
        right = client.post(reverse("pos:unlock"), {"password": "fix-pos-pw"})
        self.expect("the cashier's own password unlocks it",
                    right.status_code == 200 and right.json()["ok"]
                    and client.get(reverse("pos:terminal")).context["locked"] is False)
        response, sale = self.ring(client, {self.milk: "1"})
        self.expect("and the till rings again", sale is not None)
        client.post(reverse("pos:lock"))
        for _ in range(5):
            client.post(reverse("pos:unlock"), {"password": "guess"})
        locked = client.post(reverse("pos:unlock"), {"password": "fix-pos-pw"})
        self.expect("five wrong passwords lock unlocking out, even with the right one next",
                    locked.status_code == 429 and locked.json().get("locked_out"),
                    f"HTTP {locked.status_code}")
        from apps.accounts.views import login_locked_out
        self.expect("through the same lockout the sign-in page uses",
                    login_locked_out(user.email, "10.29.0.9"))
        other = self.till_client(self.till_cashier, "10.29.0.10")
        self.expect("a colleague's unlock does not lift this lock (it is per session)",
                    other.post(reverse("pos:unlock"), {"password": "fix-pos-pw"}).status_code == 200
                    and client.get(reverse("pos:terminal")).context["locked"] is True)
