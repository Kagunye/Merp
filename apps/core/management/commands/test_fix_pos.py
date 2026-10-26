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

    def ring(self, client, items, **extra):
        from apps.pos.models import POSSale
        data = {f"qty_{product.pk}": qty for product, qty in items.items()}
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

        def txn_form(amount, kind="CREDIT", description="Deposit", account=kes):
            return {"bank_account": account.pk, "transaction_date": day.isoformat(),
                    "value_date": "", "transaction_type": kind, "amount": amount,
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
