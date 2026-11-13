"""Checks for a company keeping its data in its own database (apps.core.tenancy).

    python manage.py test_fix_tenancy      # rolls back; the company database
                                           # is a temporary SQLite file

    1. shared, as always -> two companies in the shared database, each with
                            an invoice and a POS sale; nothing routed
    2. move out          -> the company's rows copied to its own database,
                            row counts and the ledger checked; the URL kept
                            encrypted; the other company's rows not copied;
                            member copies cannot sign in there
    3. one never sees the other -> lists, details and reports of each show
                            only its own; a key from the other is refused
    4. posting           -> an invoice posted in the company's database,
                            balanced, nothing written to the shared one
    5. POS               -> a ticket rung there, stock and books with it
    6. reports           -> trial balance, P&L, balance sheet and aged
                            debtors read the company's database
    7. control stays shared -> sign-in, memberships and roles live in the
                            shared database; a role change reaches the copy
    8. outside a request -> company_db(), transaction.atomic() and callbacks
                            (find_anywhere) use the right database;
                            migrate_companies brings it up to date
    9. move back         -> everything, the new rows too, back in the shared
                            database and checked; the other company untouched
"""
import datetime
import os
import re
import shutil
import tempfile
import uuid
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import DEFAULT_DB_ALIAS, transaction
from django.test import Client
from django.test.utils import setup_test_environment, teardown_test_environment
from django.urls import reverse
from django.utils import timezone

D = Decimal


class Command(BaseCommand):
    help = "Checks for a company with its own database."

    def handle(self, *args, **opts):
        from apps.core import tenancy
        self.passed, self.failures = 0, []
        self.tmp = tempfile.mkdtemp(prefix="mofisi-tenancy-")
        self.alias = None
        setup_test_environment()
        try:
            with transaction.atomic():
                self.run()
                transaction.set_rollback(True)
        finally:
            teardown_test_environment()
            tenancy.deactivate()
            if self.alias:
                tenancy.forget(self.alias)
            shutil.rmtree(self.tmp, ignore_errors=True)
        self.stdout.write("")
        self.stdout.write(f"Checks passed: {self.passed}")
        if self.failures:
            self.stdout.write(self.style.ERROR(f"Failures: {len(self.failures)}"))
            for f in self.failures:
                self.stderr.write(self.style.ERROR(f"  FAIL {f}"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("Failures: 0"))

    def section(self, t):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"-- {t}"))

    def expect(self, label, cond, detail=""):
        if cond:
            self.passed += 1
            self.stdout.write(f"  ok   {label}" + (f"  ({detail})" if detail else ""))
            return True
        self.failures.append(f"{label} — {detail}" if detail else label)
        self.stdout.write(self.style.ERROR(f"  FAIL {label} {detail}"))
        return False

    # ── helpers ──────────────────────────────────────────────────────────

    def client_for(self, u):
        c = Client(raise_request_exception=False)
        c.force_login(u, backend="django.contrib.auth.backends.ModelBackend")
        return c

    def make_user(self, name):
        from apps.accounts.models import User
        u = User.objects.create(username=f"{name}.{self.stamp}", email=f"{name}.{self.stamp}@fixten.local",
                                first_name=name.split(".")[0].title(), last_name="Fix")
        u.set_password("fix-ten-pw")
        u.save()
        return u

    def member(self, company, name, role, branch=None):
        from apps.organizations.models import CompanyMembership
        u = self.make_user(name)
        CompanyMembership.objects.create(company=company, user=u, role=role, branch=branch, is_default=True)
        return u

    def company(self, client, tag):
        from apps.organizations.models import Company
        name = f"Fix Tenancy {tag} {self.stamp}"
        client.post(reverse("organizations:company_create"), {
            "name": name, "legal_name": name, "registration_number": f"PVT-{tag}-{self.stamp}", "tax_id": "",
            "vat_number": "", "currency": "KES", "timezone": "Africa/Nairobi", "fiscal_year_start_month": "1",
            "industry": "Trading", "phone": "+254 700 000 009", "email": f"i@{tag}{self.stamp}.fixten.local",
            "website": "", "address_line1": "9 Kimathi Street", "address_line2": "", "city": "Nairobi",
            "state_province": "Nairobi", "postal_code": "00100", "country": "Kenya"})
        return Company.objects.get(name=name)

    def account(self, co, code):
        from apps.accounting.models import Account
        return Account.objects.get(company=co, code=code)

    def invoice(self, client, co, customer, amount, ref):
        from apps.sales.models import Invoice, InvoiceLine
        from apps.sales.views import _recalculate_invoice_totals
        today = timezone.localdate()
        inv = Invoice.objects.create(company=co, reference=ref, customer=customer, invoice_date=today,
                                     due_date=today + datetime.timedelta(days=30))
        InvoiceLine.objects.create(invoice=inv, description="Consulting", quantity=D("1"), unit_price=D(amount))
        _recalculate_invoice_totals(inv)
        client.post(reverse("sales:invoice_post", args=[inv.pk]), {})
        inv.refresh_from_db()
        return inv

    def stock_up(self, co, admin, sku, price):
        from apps.accounting.models import TaxRate
        from apps.inventory.models import Product, ProductCategory, StockMovement, UnitOfMeasure
        from apps.organizations.models import Warehouse
        uom = (UnitOfMeasure.objects.filter(company=co, abbreviation="PCS").first()
               or UnitOfMeasure.objects.create(company=co, name="Pieces", abbreviation="PCS", is_base=True))
        cat = ProductCategory.objects.create(company=co, name=f"General {sku}")
        p = Product.objects.create(
            company=co, sku=sku, name=f"Item {sku}", category=cat, unit_of_measure=uom,
            cost_price=D("50"), selling_price=D(price), tax_rate=TaxRate.objects.filter(company=co, rate=D("16")).first(),
            track_inventory=True, product_type="GOODS", income_account=self.account(co, "4200"),
            cogs_account=self.account(co, "5000"), inventory_account=self.account(co, "1400"))
        StockMovement.objects.create(product=p, warehouse=Warehouse.objects.get(company=co, is_default=True),
                                     movement_type="RECEIPT", quantity=D("40"), unit_cost=D("50"),
                                     movement_date=timezone.localdate(), reference=f"GRN-{sku}",
                                     performed_by=admin, notes="Opening stock")
        return p

    def ring(self, client, product, qty):
        from apps.core import tenancy
        from apps.pos.models import POSSale
        client.post(reverse("pos:session_open"), {"opening_cash": "1000"})
        page = client.get(reverse("pos:terminal"))
        ctx = getattr(page, "context", None)
        token = (ctx.get("ticket_token") if ctx else None) or uuid.uuid4().hex
        r = client.post(reverse("pos:terminal"), {f"qty_{product.pk}": str(qty), "ticket_token": token})
        m = re.search(r"/sales/([0-9a-fA-F-]{36})/?(?:\?rung=1)?$", r.get("Location") or "")
        with tenancy.company_db(product.company):
            return POSSale.objects.filter(pk=m.group(1)).first() if m else None, r

    def rows_of(self, co, alias):
        """Rows in `alias` carrying the company directly, summed over every table."""
        from apps.core import tenancy_move as move
        from apps.organizations.models import Company
        n = 0
        scoped, _ = move.classify()
        for m in scoped:
            for f in move._relations(m):
                if f.related_model is Company:
                    n += m._base_manager.using(alias).filter(**{f.attname: co.pk}).count()
        return n

    # ── the checks ───────────────────────────────────────────────────────

    def run(self):
        from apps.accounting.models import JournalEntry
        from apps.accounts.models import User
        from apps.core import tenancy
        from apps.core import tenancy_move as move
        from apps.crm.models import Customer
        from apps.organizations.models import Company, CompanyMembership
        from apps.pos.models import POSSale
        from apps.sales.models import Invoice

        self.stamp = datetime.datetime.now().strftime("%H%M%S%f")
        quiet = StringIO()
        say = quiet.write

        # ── 1 ──
        self.section("1. shared, as always")
        admin_a, admin_b = self.make_user("ten.admin.a"), self.make_user("ten.admin.b")
        ca, cb = self.client_for(admin_a), self.client_for(admin_b)
        A, B = self.company(ca, "A"), self.company(cb, "B")
        self.expect("two companies in the shared database, neither with its own",
                    not A.own_database and not B.own_database)
        cust_a = Customer.objects.create(company=A, name=f"Kamau Hardware {self.stamp}", email="a@kamau.example")
        cust_b = Customer.objects.create(company=B, name=f"Wanjiku Stores {self.stamp}", email="b@wanjiku.example")
        inv_a = self.invoice(ca, A, cust_a, "3333", f"TA-{self.stamp}")
        inv_b = self.invoice(cb, B, cust_b, "7777", f"TB-{self.stamp}")
        self.expect("each posts its invoice", inv_a.status != "DRAFT" and inv_b.status != "DRAFT",
                    f"{inv_a.status}/{inv_b.status}")
        cash_b = self.member(B, "ten.cash.b", "POS_CASHIER", B.branches.get(is_headquarters=True))
        sugar = self.stock_up(B, admin_b, f"SUG-{self.stamp[-6:]}", "200")
        sale1, _ = self.ring(self.client_for(cash_b), sugar, 2)
        self.expect("a POS ticket rung in the shared database", sale1 is not None)
        self.expect("with nothing routed, no company database is active", tenancy.current_alias() is None)
        book_a = move.ledger(A, DEFAULT_DB_ALIAS)
        book_b = move.ledger(B, DEFAULT_DB_ALIAS)
        self.expect("both have a ledger", bool(book_a) and bool(book_b))

        # ── 2 ──
        self.section("2. move out")
        url = "sqlite:///" + os.path.join(self.tmp, "company_b.sqlite3").replace("\\", "/")
        dry = move.move_out(B, url=url, dry_run=True, out=say)
        self.expect("a dry run counts the rows and changes nothing",
                    sum(len(v) for v in dry["owned"].values()) > 20 and not Company.objects.get(pk=B.pk).own_database)
        try:
            move.move_out(A, url=None, out=say)
            refused = False
        except move.MoveError:
            refused = True
        self.expect("a move with no URL is refused", refused)
        result = move.move_out(B, url=url, out=say)
        self.alias = result["alias"]
        B.refresh_from_db()
        owned = result["owned"]
        self.expect("the company now keeps its data in its own database", B.own_database)
        self.expect("the URL is stored encrypted, not as typed",
                    B.database_url_encrypted and "sqlite" not in B.database_url_encrypted and not B.database_url_env)
        self.expect("and reads back as typed", B.database_url() == url)
        self.expect("every table's rows arrived", not move.verify(B, DEFAULT_DB_ALIAS, self.alias, owned))
        self.expect("the ledger matches account by account", move.ledger(B, self.alias) == book_b,
                    str(move.ledger_totals(book_b)))
        self.expect("the invoice and the ticket are in the company's database",
                    Invoice.objects.using(self.alias).filter(pk=inv_b.pk).exists()
                    and POSSale.objects.using(self.alias).filter(pk=sale1.pk).exists())
        self.expect("none of the other company's rows were copied", self.rows_of(A, self.alias) == 0)
        self.expect("the other company still has all of its rows in the shared database",
                    move.ledger(A, DEFAULT_DB_ALIAS) == book_a)
        copy = User.objects.using(self.alias).get(pk=admin_b.pk)
        self.expect("member copies in the company database cannot sign in", not copy.has_usable_password())
        self.expect("users of the other company are not copied",
                    not User.objects.using(self.alias).filter(pk=admin_a.pk).exists())
        self.expect("the company row's copy carries no database key",
                    Company.objects.using(self.alias).get(pk=B.pk).database_url_encrypted == "")
        try:
            move.move_out(B, url=url, out=say)
            again = False
        except move.MoveError:
            again = True
        self.expect("moving it out twice is refused", again)

        # ── 3 ──
        self.section("3. one never sees the other")
        page_b = cb.get(reverse("crm:customer_list")).content.decode()
        page_a = ca.get(reverse("crm:customer_list")).content.decode()
        self.expect("B's customer list shows its customer", cust_b.name in page_b)
        self.expect("and not A's", cust_a.name not in page_b)
        self.expect("A's list shows its own and not B's", cust_a.name in page_a and cust_b.name not in page_a)
        self.expect("A cannot open B's customer by its key",
                    ca.get(reverse("crm:customer_detail", args=[cust_b.pk])).status_code in (403, 404))
        self.expect("B cannot open A's customer by its key",
                    cb.get(reverse("crm:customer_detail", args=[cust_a.pk])).status_code in (403, 404))
        self.expect("B opens its own customer", cb.get(reverse("crm:customer_detail", args=[cust_b.pk])).status_code == 200)
        self.expect("B's invoice list shows its invoice, not A's",
                    (lambda p: inv_b.reference in p and inv_a.reference not in p)(
                        cb.get(reverse("sales:invoice_list")).content.decode()))
        self.expect("after each request the shared database is current again", tenancy.current_alias() is None)

        # ── 4 ──
        self.section("4. posting")
        with tenancy.company_db(B):
            cust_b2 = Customer.objects.create(company=B, name=f"Otieno Agencies {self.stamp}")
            before = JournalEntry.objects.filter(company=B).count()
        inv_b2 = None
        with tenancy.company_db(B):
            inv_b2 = self.invoice(cb, B, cust_b2, "1250", f"TB2-{self.stamp}")
            je = JournalEntry.objects.filter(company=B).count()
            posted = Invoice.objects.get(pk=inv_b2.pk)
        self.expect("an invoice posts in the company's database", posted.status != "DRAFT", posted.status)
        self.expect("with its journal entry there", je == before + 1, f"{before} -> {je}")
        entry = JournalEntry.objects.using(self.alias).filter(company=B).order_by("-created_at").first()
        lines = list(entry.lines.all()) if entry is not None else []
        dr, cr = sum(l.debit_amount for l in lines), sum(l.credit_amount for l in lines)
        self.expect("balanced", dr == cr > 0, f"Dr {dr} / Cr {cr}")
        self.expect("nothing of it written to the shared database",
                    not Invoice.objects.using(DEFAULT_DB_ALIAS).filter(pk=inv_b2.pk).exists()
                    and not Customer.objects.using(DEFAULT_DB_ALIAS).filter(pk=cust_b2.pk).exists())

        # ── 5 ──
        self.section("5. POS")
        sale2, r = self.ring(self.client_for(cash_b), sugar, 3)
        self.expect("a ticket rings on the company's till", sale2 is not None, f"status {r.status_code}")
        if sale2 is not None:
            self.expect("stored in the company's database only",
                        POSSale.objects.using(self.alias).filter(pk=sale2.pk).exists()
                        and not POSSale.objects.using(DEFAULT_DB_ALIAS).filter(pk=sale2.pk).exists())
            self.expect("with its books there", sale2.journal_entry_id is not None
                        if hasattr(sale2, "journal_entry_id") else True)
        from apps.inventory.models import StockMovement
        moved = StockMovement.objects.using(self.alias).filter(product=sugar, quantity__lt=0).count()             or StockMovement.objects.using(self.alias).filter(product=sugar).exclude(movement_type="RECEIPT").count()
        self.expect("the goods left the company's stock", moved >= 2, f"{moved} movements")

        # ── 6 ──
        self.section("6. reports")
        for name in ("trial_balance", "profit_loss", "balance_sheet", "aged_debtors"):
            r = cb.get(reverse(f"reporting:{name}"))
            self.expect(f"{name.replace('_', ' ')} opens", r.status_code == 200, str(r.status_code))
        tb_b = cb.get(reverse("reporting:trial_balance")).content.decode()
        tb_a = ca.get(reverse("reporting:trial_balance")).content.decode()
        def money(v):
            return f"{v:,.2f}"
        total_b = move.ledger_totals(move.ledger(B, self.alias))[1]
        total_a = move.ledger_totals(move.ledger(A, DEFAULT_DB_ALIAS))[1]
        self.expect("B's trial balance totals its own database's ledger", money(total_b) in tb_b, money(total_b))
        self.expect("A's trial balance totals its own and carries none of B's",
                    money(total_a) in tb_a and money(total_b) not in tb_a and "7,777" not in tb_a)
        aged = cb.get(reverse("reporting:aged_debtors")).content.decode()
        self.expect("aged debtors lists B's customers from its database", cust_b2.name in aged or cust_b.name in aged)

        # ── 7 ──
        self.section("7. control stays shared")
        fresh = Client()
        self.expect("a member signs in with the password kept in the shared database",
                    fresh.login(username=cash_b.email, password="fix-ten-pw")
                    or fresh.login(email=cash_b.email, password="fix-ten-pw"))
        self.expect("their membership lives in the shared database",
                    CompanyMembership.objects.using(DEFAULT_DB_ALIAS).filter(company=B, user=cash_b).exists())
        m = CompanyMembership.objects.get(company=B, user=cash_b)
        m.role = "SALES_REP"
        m.save()
        self.expect("a role change reaches the company's copy",
                    CompanyMembership.objects.using(self.alias).get(pk=m.pk).role == "SALES_REP")
        m.role = "POS_CASHIER"
        m.save()
        newbie = self.member(B, "ten.new.b", "VIEWER")
        self.expect("a new member is copied in with them",
                    User.objects.using(self.alias).filter(pk=newbie.pk).exists()
                    and CompanyMembership.objects.using(self.alias).filter(user=newbie).exists())

        C = self.company(cb, "C")
        from apps.organizations.models import Branch
        self.expect("a company created from inside B's pages starts in the shared database",
                    not C.own_database and Branch.objects.using(DEFAULT_DB_ALIAS).filter(company=C).exists()
                    and not Branch.objects.using(self.alias).filter(company=C).exists()
                    and self.rows_of(C, self.alias) == 0)

        # ── 8 ──
        self.section("8. outside a request")
        with tenancy.company_db(B) as alias:
            self.expect("company_db() switches to the company's database", tenancy.current_alias() == alias == self.alias)
            self.expect("transaction.atomic() opens there", transaction.get_connection().alias == self.alias)
            self.expect("queries read there", Customer.objects.filter(pk=cust_b2.pk).exists())
            self.expect("users and companies still read from the shared one",
                        Company.objects.db == DEFAULT_DB_ALIAS and User.objects.db == DEFAULT_DB_ALIAS)
        self.expect("and back to the shared one after", tenancy.current_alias() is None
                    and transaction.get_connection().alias == DEFAULT_DB_ALIAS)
        with tenancy.company_db(A) as alias:
            self.expect("a shared-database company stays in the shared database", alias is None)
        found = tenancy.find_anywhere(lambda: Customer.objects.filter(name=cust_b2.name).first())
        self.expect("a callback finds a row in the company's database", found is not None and found.pk == cust_b2.pk)
        self.expect("and is left working in it", tenancy.current_alias() == self.alias)
        tenancy.deactivate()
        stale = tenancy.find_anywhere(lambda: Customer.objects.filter(pk=cust_b.pk).first())
        self.expect("a stale copy left in the shared database is not used",
                    stale is not None and tenancy.current_alias() == self.alias)
        tenancy.deactivate()
        found_a = tenancy.find_anywhere(lambda: Customer.objects.filter(pk=cust_a.pk).first())
        self.expect("a shared-database company's row is found where it lives",
                    found_a is not None and tenancy.current_alias() is None)
        from apps.payments.models import PaymentGateway
        with tenancy.company_db(B):
            gw = PaymentGateway.objects.create(company=B, provider="MPESA", environment="SIMULATOR")
        anon = Client()
        self.expect("an M-Pesa callback with the company's key reaches its database",
                    anon.post(reverse("payments:c2b_validate", args=[gw.webhook_key]), "{}",
                              content_type="application/json").status_code == 200)
        self.expect("an unknown key is still refused",
                    anon.post(reverse("payments:c2b_validate", args=["no-such-key"]), "{}",
                              content_type="application/json").status_code == 404)
        self.expect("the callback left no company database active", tenancy.current_alias() is None)
        out = StringIO()
        call_command("migrate_companies", stdout=out, stderr=out)
        self.expect("migrate_companies brings the company database up to date", "up to date" in out.getvalue(),
                    out.getvalue().strip()[-80:])

        # ── 9 ──
        self.section("9. move back")
        final_b = move.ledger(B, self.alias)
        back = move.move_back(B, out=say)
        B.refresh_from_db()
        alias, self.alias = self.alias, None
        self.expect("the company is back in the shared database", not B.own_database and not B.database_url_encrypted)
        self.expect("its ledger, new entries too, matches", move.ledger(B, DEFAULT_DB_ALIAS) == final_b,
                    str(move.ledger_totals(final_b)))
        self.expect("rows made in its own database came back",
                    Invoice.objects.filter(pk=inv_b2.pk).exists() and Customer.objects.filter(pk=cust_b2.pk).exists()
                    and (sale2 is None or POSSale.objects.filter(pk=sale2.pk).exists()))
        self.expect("no row twice", Customer.objects.filter(company=B).count()
                    == len(back["owned"].get(Customer, ())))
        self.expect("the other company never moved", move.ledger(A, DEFAULT_DB_ALIAS) == book_a)
        page_b = cb.get(reverse("crm:customer_list")).content.decode()
        self.expect("B's pages read the shared database again", cust_b2.name in page_b and cust_a.name not in page_b)
        self.expect("and its company database is let go", alias not in __import__("django").db.connections.settings)
