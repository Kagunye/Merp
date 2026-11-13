"""Checks for the restaurant layer of the till: PIN staff, happy hours, bills,
manager voids, closing a bill through a till, and the day end.

Usage:
    python manage.py test_fix_restaurant          # rolls the whole run back
    python manage.py test_fix_restaurant --keep   # leave the test company behind

    1. PINs                -> easy PINs refused; five wrong in a row lock the
                              person out; the right PIN works again after
    2. happy hours         -> a rule applies only on its days, in its window
                              (overnight too), at its branches, to its items;
                              the best running rule wins
    3. bills               -> one live bill per table; adding after the bill
                              was printed reopens it; voids need a manager and
                              a reason; another company's staff are refused
    4. closing a bill      -> rung through the cashier's own open till at the
                              same branch: a sale for exactly the bill, the
                              happy-hour discount on it, stock out, a balanced
                              entry; it closes once
    5. the day end         -> takings, P&L, voids and waiters add up; the day
                              will not close with a till or bill open; it
                              closes once
    6. the screens         -> back office, station and cashier pages load for
                              the right people
"""
import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
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
    help = "Checks for the restaurant layer of the till (stations, bills, day end)."

    def add_arguments(self, parser):
        parser.add_argument("--keep", action="store_true")

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

    # ── helpers ──
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

    def refused(self, label, fn, needle=""):
        try:
            fn()
        except ValidationError as exc:
            msg = " ".join(exc.messages)
            return self.expect(label, needle.lower() in msg.lower(), msg)
        return self.expect(label, False, "was allowed")

    def account(self, code):
        from apps.accounting.models import Account
        return Account.objects.get(company=self.company, code=code)

    def make_user(self, name, role=None, branch=None):
        from apps.accounts.models import User
        from apps.organizations.models import CompanyMembership
        user = User.objects.create(username=f"{name}.{self.stamp}",
                                   email=f"{name}.{self.stamp}@fixrest.local",
                                   first_name=name.split(".")[0].title(), last_name="Test")
        user.set_password("fix-rest-pw")
        user.save()
        if role:
            CompanyMembership.objects.create(company=self.company, user=user, role=role,
                                             branch=branch, is_default=True)
        return user

    def login(self, user):
        client = Client(raise_request_exception=False)
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        return client

    def at(self, day_offset, hh, mm=0):
        """An aware moment in Nairobi: today + day_offset at hh:mm."""
        day = timezone.localdate() + datetime.timedelta(days=day_offset)
        return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hh, mm)))

    # ── fixtures ──
    def setup(self):
        from apps.accounting.models import TaxRate
        from apps.inventory.models import Product, ProductCategory, StockMovement, UnitOfMeasure
        from apps.organizations.models import Branch, Company, Warehouse
        from apps.pos import restaurant
        from apps.pos.models import POSMenu, POSMenuItem, POSMenuSection, POSStaff, POSStation, POSTable

        self.stamp = datetime.datetime.now().strftime("%H%M%S%f")
        self.admin = self.make_user("rest.admin")
        self.admin_client = self.login(self.admin)
        name = f"Fix Restaurant {self.stamp}"
        self.admin_client.post(reverse("organizations:company_create"), {
            "name": name, "legal_name": f"{name} Limited", "registration_number": f"PVT-{self.stamp}",
            "tax_id": f"A{self.stamp}Z", "vat_number": "VAT-FIX", "currency": "KES",
            "timezone": "Africa/Nairobi", "fiscal_year_start_month": "1", "industry": "Hospitality",
            "phone": "+254 700 000 002", "email": f"info@{self.stamp}.fixrest.local", "website": "",
            "address_line1": "2 Moi Avenue", "address_line2": "", "city": "Nairobi",
            "state_province": "Nairobi", "postal_code": "00100", "country": "Kenya"})
        self.company = Company.objects.filter(name=name).first()
        if not self.expect("company created", self.company is not None):
            raise SystemExit(1)
        c = self.company
        self.hq = c.branches.get(is_headquarters=True)
        self.msa = Branch.objects.create(company=c, name="Mombasa", code="MSA", city="Mombasa",
                                         country="Kenya")
        self.wh = Warehouse.objects.get(company=c, is_default=True)
        uom = (UnitOfMeasure.objects.filter(company=c, abbreviation="PCS").first()
               or UnitOfMeasure.objects.create(company=c, name="Pieces", abbreviation="PCS", is_base=True))
        drinks = ProductCategory.objects.create(company=c, name="Drinks")
        self.beers = ProductCategory.objects.create(company=c, name="Beer", parent=drinks)
        food = ProductCategory.objects.create(company=c, name="Food")
        vat = TaxRate.objects.filter(company=c, rate=D("16")).first()

        def product(sku, nm, cost, price, cat):
            return Product.objects.create(
                company=c, sku=sku, name=nm, category=cat, unit_of_measure=uom,
                cost_price=D(cost), selling_price=D(price), tax_rate=vat, track_inventory=True,
                product_type="GOODS", income_account=self.account("4200"),
                cogs_account=self.account("5000"), inventory_account=self.account("1400"))

        self.tusker = product("TUSK", "Tusker 500ml", "180", "300", self.beers)
        self.chips = product("CHIP", "Chips (plate)", "60", "250", food)
        for item, qty, cost in [(self.tusker, 100, "180"), (self.chips, 50, "60")]:
            StockMovement.objects.create(product=item, warehouse=self.wh, movement_type="RECEIPT",
                                         quantity=D(qty), unit_cost=D(cost),
                                         movement_date=timezone.localdate(),
                                         reference=f"GRN-{item.sku}", performed_by=self.admin)

        self.menu = POSMenu.objects.create(company=c, name="Main menu")
        self.s_drinks = POSMenuSection.objects.create(menu=self.menu, name="Drinks", sort_order=1)
        self.s_food = POSMenuSection.objects.create(menu=self.menu, name="Food", sort_order=2)
        self.i_tusker = POSMenuItem.objects.create(section=self.s_drinks, product=self.tusker)
        self.i_chips = POSMenuItem.objects.create(section=self.s_food, product=self.chips,
                                                  display_name="Chips", price=D("280"))
        self.station = POSStation.objects.create(company=c, branch=self.hq, name="Terrace",
                                                 code="T1", warehouse=self.wh, menu=self.menu)
        self.msa_station = POSStation.objects.create(company=c, branch=self.msa, name="MSA Bar",
                                                     code="M1", kind="BAR", menu=self.menu)
        self.table = POSTable.objects.create(company=c, branch=self.hq, station=self.station,
                                             name="Table 4", area="Terrace")
        self.waiter = POSStaff.objects.create(company=c, branch=self.hq, name="Wanjiku",
                                              code="W01", role="WAITER")
        self.manager = POSStaff.objects.create(company=c, name="Otieno", code="M01", role="MANAGER")
        restaurant.set_pin(self.waiter, "4826")
        restaurant.set_pin(self.manager, "7391")
        self.cashier = self.make_user("rest.cashier", "POS_CASHIER", self.hq)
        self.supervisor = self.make_user("rest.super", "SALES_MANAGER", self.hq)
        self.viewer = self.make_user("rest.viewer", "VIEWER", self.hq)

    def run(self):
        self.section("0. setup")
        self.setup()
        self.check_pins()
        self.check_happy_hours()
        self.check_bills()
        self.check_close()
        self.check_day_end()
        self.check_screens()
        self.check_split()

    # ── 1 ──
    def check_pins(self):
        from apps.pos import restaurant
        from apps.pos.models import POSStaff
        self.section("1. PINs")
        self.refused("a repeated-digit PIN is refused", lambda: restaurant.set_pin(self.waiter, "1111"),
                     "too easy")
        self.refused("a running PIN is refused", lambda: restaurant.set_pin(self.waiter, "1234"), "too easy")
        self.refused("a 3-digit PIN is refused", lambda: restaurant.set_pin(self.waiter, "482"), "4 to 8")
        ok = restaurant.verify_pin(self.waiter, "4826")
        self.expect("the right PIN identifies the waiter", ok.pk == self.waiter.pk)
        for _ in range(4):
            self.refused("a wrong PIN is refused and counted",
                         lambda: restaurant.verify_pin(self.waiter, "0000"), "wrong pin")
        self.refused("the fifth wrong PIN locks the waiter out",
                     lambda: restaurant.verify_pin(self.waiter, "0000"), "locked")
        self.refused("while locked, even the right PIN is refused",
                     lambda: restaurant.verify_pin(self.waiter, "4826"), "locked")
        POSStaff.objects.filter(pk=self.waiter.pk).update(locked_until=None, failed_pins=0)
        self.expect("after the lock is lifted the right PIN works",
                    restaurant.verify_pin(self.waiter, "4826").pk == self.waiter.pk)
        self.refused("a waiter cannot authorise as a manager",
                     lambda: restaurant.verify_manager(self.company, self.hq, self.waiter.pk, "4826"),
                     "manager")
        self.expect("a manager with the right PIN can authorise",
                    restaurant.verify_manager(self.company, self.hq, self.manager.pk, "7391").pk
                    == self.manager.pk)

    # ── 2 ──
    def check_happy_hours(self):
        from apps.pos import restaurant
        from apps.pos.models import POSPriceRule
        self.section("2. happy hours")
        today = timezone.localdate()
        wd = today.weekday()
        evening = POSPriceRule.objects.create(
            company=self.company, name="Beer o'clock", discount_percent=D("20"),
            days=str(wd), start_time=datetime.time(17), end_time=datetime.time(19),
            category=self.beers)
        late = POSPriceRule.objects.create(
            company=self.company, name="Late drinks", discount_percent=D("10"),
            days=str(wd), start_time=datetime.time(22), end_time=datetime.time(2),
            section=self.s_drinks)
        mombasa = POSPriceRule.objects.create(
            company=self.company, name="Mombasa food", discount_percent=D("50"),
            days="0,1,2,3,4,5,6", start_time=datetime.time(0), end_time=datetime.time(23, 59),
            product=self.chips)
        mombasa.branches.add(self.msa)

        price, pct, rule = restaurant.price_for(self.i_tusker, self.hq, self.at(0, 18))
        self.expect("18:00: beer gets the 20% evening discount (category and below)",
                    pct == D("20") and rule == evening and price == D("300"), f"{pct} {rule}")
        _, pct, rule = restaurant.price_for(self.i_tusker, self.hq, self.at(0, 19, 30))
        self.expect("19:30: the window has shut", pct == Z and rule is None, f"{pct}")
        _, pct, rule = restaurant.price_for(self.i_tusker, self.hq, self.at(0, 23, 30))
        self.expect("23:30: the overnight rule runs", pct == D("10") and rule == late, f"{pct}")
        _, pct, rule = restaurant.price_for(self.i_tusker, self.hq, self.at(1, 1, 0))
        self.expect("01:00 tomorrow: still last night's overnight rule", pct == D("10"), f"{pct}")
        _, pct, rule = restaurant.price_for(self.i_tusker, self.hq, self.at(1, 18))
        self.expect("18:00 tomorrow: not one of its days", pct == Z, f"{pct}")
        price, pct, rule = restaurant.price_for(self.i_chips, self.hq, self.at(0, 12))
        self.expect("chips at Head Office: menu price 280, no Mombasa discount",
                    price == D("280") and pct == Z, f"{price} {pct}")
        _, pct, _ = restaurant.price_for(self.i_chips, self.msa, self.at(0, 12))
        self.expect("chips at Mombasa: the Mombasa-only rule applies", pct == D("50"), f"{pct}")
        evening.discount_percent = D("5")
        evening.end_time = datetime.time(23, 45)
        evening.save()
        _, pct, rule = restaurant.price_for(self.i_tusker, self.hq, self.at(0, 23, 0))
        self.expect("two rules running: the bigger discount wins", pct == D("10") and rule == late,
                    f"{pct} {rule}")
        POSPriceRule.objects.filter(company=self.company).update(is_active=False)
        self.now_rule = POSPriceRule.objects.create(
            company=self.company, name="All day beer", discount_percent=D("25"),
            days="0,1,2,3,4,5,6", start_time=datetime.time(0), end_time=datetime.time(23, 59, 59),
            product=self.tusker)

    # ── 3 ──
    def check_bills(self):
        from apps.pos import restaurant
        from apps.pos.models import POSStaff
        self.section("3. bills")
        bill = restaurant.open_order(self.station, self.waiter, table=self.table, covers=3)
        self.bill = bill
        self.expect("a bill opens with a BILL- number", bill.reference.startswith("BILL-"))
        self.refused("a second bill on the same table is refused",
                     lambda: restaurant.open_order(self.station, self.waiter, table=self.table), "already")
        restaurant.add_item(bill, self.i_tusker, 3, self.waiter)
        restaurant.add_item(bill, self.i_chips, 2, self.waiter, note="no salt")
        lines = list(bill.lines.order_by("added_at"))
        self.expect("the beer carries the running 25% happy hour",
                    lines[0].discount_percent == D("25") and lines[0].price_rule_id == self.now_rule.pk)
        self.expect("the chips sell at the menu price 280", lines[1].unit_price == D("280"))
        t = restaurant.order_totals(bill)
        # 3 x 300 = 900, less 25% = 675; 2 x 280 = 560 -> net 1235, VAT 16% = 108 + 89.60
        self.expect("bill totals: net 1235.00, VAT 197.60, total 1432.60",
                    (t["subtotal"], t["tax"], t["total"]) == (D("1235.00"), D("197.60"), D("1432.60")),
                    str(t))
        restaurant.print_bill(bill)
        bill.refresh_from_db()
        self.expect("printing marks the bill BILLED", bill.status == "BILLED")
        extra = restaurant.add_item(bill, self.i_chips, 1, self.waiter)
        bill.refresh_from_db()
        self.expect("ordering after the bill was printed reopens it", bill.status == "OPEN")
        self.refused("a void with no reason is refused",
                     lambda: restaurant.void_line(extra, self.manager, self.waiter, ""), "reason")
        self.refused("a waiter cannot void",
                     lambda: restaurant.void_line(extra, self.waiter, self.waiter, "wrong table"), "manager")
        restaurant.void_line(extra, self.manager, self.waiter, "Ordered by mistake")
        extra.refresh_from_db()
        self.expect("the manager's void is recorded with who asked and who approved",
                    extra.status == "VOID" and extra.voided_by_id == self.manager.pk
                    and extra.void_requested_by_id == self.waiter.pk)
        self.expect("a voided line leaves the total alone",
                    restaurant.order_totals(bill)["total"] == D("1432.60"))
        self.refused("a voided line cannot be voided again",
                     lambda: restaurant.void_line(extra, self.manager, self.waiter, "again"), "already")

        from apps.organizations.models import Company
        other = Company.objects.exclude(pk=self.company.pk).first()
        if other is not None:
            stranger = POSStaff(company=other, name="Stranger", code="X", role="WAITER")
            self.refused("staff from another company cannot add to the bill",
                         lambda: restaurant.add_item(bill, self.i_tusker, 1, stranger), "company")
        self.refused("a Mombasa station's waiter rule: HQ waiter cannot open there",
                     lambda: restaurant.open_order(self.msa_station, self.waiter), "another branch")

        walkin = restaurant.open_order(self.station, self.waiter)
        restaurant.add_item(walkin, self.i_tusker, 1, self.waiter)
        restaurant.void_order(walkin, self.manager, "Customer left")
        walkin.refresh_from_db()
        self.expect("a voided bill is VOID with every line voided",
                    walkin.status == "VOID" and not walkin.lines.filter(status="ACTIVE").exists())
        self.refused("a voided bill takes no more orders",
                     lambda: restaurant.add_item(walkin, self.i_tusker, 1, self.waiter), "voided")

    # ── 4 ──
    def check_close(self):
        from apps.accounting.models import JournalEntryLine
        from apps.inventory.models import StockMovement
        from apps.pos import restaurant, services
        self.section("4. closing a bill")
        bill = self.bill
        self.refused("with no open till the bill cannot be paid",
                     lambda: restaurant.close_order(bill, self.cashier, payment_method="CASH",
                                                    token=services.new_token()), "open your till")
        msa_user = self.make_user("rest.msa", "POS_CASHIER", self.msa)
        msa_till = services.open_session(self.company, msa_user, self.msa, D("0"))
        self.refused("a till at another branch cannot take this bill",
                     lambda: restaurant.close_order(bill, msa_user, payment_method="CASH",
                                                    token=services.new_token()), "belongs to")
        self.till = services.open_session(self.company, self.cashier, self.hq, D("1000"))
        sale = restaurant.close_order(bill, self.cashier, payment_method="CASH", tendered="1500",
                                      token=services.new_token())
        bill.refresh_from_db()
        self.expect("the bill closes against a sale", bill.status == "CLOSED" and bill.sale_id == sale.pk)
        self.expect("the sale is exactly the bill: 1432.60, change 67.40",
                    sale.total_amount == D("1432.60") and sale.change_given == D("67.40"),
                    f"{sale.total_amount} {sale.change_given}")
        beer_line = sale.lines.get(product=self.tusker)
        self.expect("the happy-hour discount is on the sale line (25%, 225.00 off)",
                    beer_line.discount_percent == D("25") and beer_line.discount_amount == D("225.00"))
        moved = StockMovement.objects.filter(reference=sale.reference, movement_type="DELIVERY")
        self.expect("stock left the station's shelf: 3 beers, 2 chips (voided one not issued)",
                    {m.product_id: m.quantity for m in moved} == {self.tusker.pk: D("3"),
                                                                    self.chips.pk: D("2")})
        je = sale.journal_entry
        lines = JournalEntryLine.objects.filter(journal_entry=je)
        dr = lines.aggregate(v=Sum("debit_amount"))["v"]
        cr = lines.aggregate(v=Sum("credit_amount"))["v"]
        self.expect("a posted, balanced entry", je and je.status == "POSTED" and dr == cr, f"{dr} {cr}")
        self.refused("the bill cannot be paid twice",
                     lambda: restaurant.close_order(bill, self.cashier, payment_method="CASH",
                                                    token=services.new_token()), "closed")
        self.refused("a closed bill cannot be voided",
                     lambda: restaurant.void_order(bill, self.manager, "oops"), "closed")
        services.close_session(msa_till, D("0"), user=msa_user)

    # ── 5 ──
    def check_day_end(self):
        from apps.pos import restaurant, services
        from apps.pos.models import POSDayEnd
        self.section("5. day end")
        day = timezone.localdate()
        rep = restaurant.day_report(self.company, self.hq, day)
        self.expect("takings: 1432.60 cash", rep["takings"] == D("1432.60"), str(rep["by_method"]))
        self.expect("P&L revenue ex VAT 1235.00", rep["pnl"]["revenue"] == D("1235.00"), str(rep["pnl"]))
        self.expect("P&L cost of sales 660.00 (3 x 180 + 2 x 60)", rep["pnl"]["cogs"] == D("660.00"),
                    str(rep["pnl"]["cogs"]))
        self.expect("happy-hour discount given 225.00", rep["pnl"]["happy_hour"] == D("225.00"))
        self.expect("the waiter served 1 bill, 3 guests",
                    rep["waiters"] and rep["waiters"][0]["bills"] == 1 and rep["waiters"][0]["covers"] == 3)
        self.expect("both voids are listed with the manager",
                    len(rep["voids"]) >= 2 and all(v["manager"] == "Otieno" for v in rep["voids"]))
        stock = {r["sku"]: r for r in rep["stock"]}
        self.expect("stock movement: beer 0 -> +100 in, 3 sold, 97 closing",
                    stock.get("TUSK", {}).get("closing") == D("97") and stock["TUSK"]["sold"] == D("3"),
                    str(stock.get("TUSK")))
        self.refused("the day will not close with a till open",
                     lambda: restaurant.close_day(self.company, self.hq, day, self.supervisor), "till")
        services.close_session(self.till, D("2432.60"), user=self.cashier)
        live = restaurant.open_order(self.station, self.waiter)
        restaurant.add_item(live, self.i_chips, 1, self.waiter)
        self.refused("nor with a bill open", lambda: restaurant.close_day(
            self.company, self.hq, day, self.supervisor), "bill")
        restaurant.void_order(live, self.manager, "Test bill")
        end = restaurant.close_day(self.company, self.hq, day, self.supervisor)
        self.expect("the day closes and keeps its figures",
                    isinstance(end, POSDayEnd) and end.summary.get("takings") == "1432.60",
                    str(end.summary.get("takings")))
        self.refused("the day closes once", lambda: restaurant.close_day(
            self.company, self.hq, day, self.supervisor), "already closed")

    # ── 6 ──
    def check_screens(self):
        self.section("6. the screens")
        sup = self.login(self.supervisor)
        viewer = self.login(self.viewer)
        cashier = self.login(self.cashier)

        def status(client, name, *args):
            try:
                return client.get(reverse(name, args=args)).status_code
            except Exception as exc:     # noqa: BLE001 - reported as a failure
                return f"error {exc}"
        for name in ("pos:bo_home", "pos:bo_settings", "pos:bo_station_list", "pos:bo_table_list",
                     "pos:bo_menu_list", "pos:bo_rule_list", "pos:bo_staff_list", "pos:bo_day_end"):
            self.expect(f"sales manager opens {name}", status(sup, name) == 200, str(status(sup, name)))
        self.expect("the menu page opens", status(sup, "pos:bo_menu_detail", self.menu.pk) == 200)
        self.expect("a viewer is kept out of the back office", status(viewer, "pos:bo_home") == 403,
                    str(status(viewer, "pos:bo_home")))
        for name, args in (("pos:station_pick", ()), ("pos:station_floor", (self.station.pk,)),
                           ("pos:cashier_panel", ()), ("pos:bill_detail", (self.bill.pk,)),
                           ("pos:bill_print", (self.bill.pk,))):
            st = status(cashier, name, *args)
            self.expect(f"the cashier opens {name}", st in (200, 302), str(st))
        self.expect("a viewer is kept off the station", status(viewer, "pos:station_pick") == 403,
                    str(status(viewer, "pos:station_pick")))

    # ── 7 ──
    def check_split(self):
        from apps.inventory.models import StockMovement
        from apps.pos import restaurant, services
        from apps.pos.models import POSOrder, POSTable
        self.section("7. split bills")
        table = POSTable.objects.create(company=self.company, branch=self.hq, station=self.station,
                                        name="Table 9", area="Terrace")
        bill = restaurant.open_order(self.station, self.waiter, table=table, covers=3)
        beer = restaurant.add_item(bill, self.i_tusker, "5", self.waiter)
        chips = restaurant.add_item(bill, self.i_chips, "2", self.waiter)
        whole = restaurant.order_totals(bill)["total"]

        self.refused("more than is on the bill cannot move",
                     lambda: restaurant.split_items(bill, self.waiter, {beer.pk: "6"}), "only 5")
        self.refused("moving everything is refused (just pay the bill)",
                     lambda: restaurant.split_items(bill, self.waiter, {beer.pk: "5", chips.pk: "2"}), "everything")
        part = restaurant.split_items(bill, self.waiter, {beer.pk: "2", chips.pk: "1"})
        qty = lambda o: {l.product_id: l.quantity for l in o.lines.filter(status="ACTIVE")}   # noqa: E731
        self.expect("by item: 2 beers and 1 chips move to a new bill on the same table",
                    part.table_id == table.pk and part.split_from_id == bill.pk
                    and qty(part) == {self.tusker.pk: D("2"), self.chips.pk: D("1")}
                    and qty(bill) == {self.tusker.pk: D("3"), self.chips.pk: D("1")}, str(qty(part)))
        moved_line = part.lines.get(product=self.tusker)
        self.expect("…at the price and discount they were ordered at",
                    moved_line.unit_price == beer.unit_price and moved_line.discount_percent == beer.discount_percent)
        two = restaurant.order_totals(bill)["total"] + restaurant.order_totals(part)["total"]
        self.expect("the two bills add up to the original", abs(two - whole) <= D("0.02"), f"{two} vs {whole}")

        parts = restaurant.split_even(bill, self.waiter, 3)
        beers = sum((qty(o).get(self.tusker.pk, D("0")) for o in parts), D("0"))
        self.expect("equally 3 ways: three bills whose beers add back to exactly 3",
                    len(parts) == 3 and beers == D("3") and all(o.table_id == table.pk for o in parts), f"{beers}")
        self.refused("between 2 and 20 guests only", lambda: restaurant.split_even(part, self.waiter, 1), "between 2")

        till = services.open_session(self.company, self.cashier, self.hq, D("0"))
        sales = []
        for order, method in zip(parts + [part], ["CASH", "CARD", "MPESA", "CASH"]):
            order.refresh_from_db()
            sales.append(restaurant.close_order(order, self.cashier, payment_method=method,
                                                token=services.new_token()))
        paid = sum((s.total_amount for s in sales), D("0"))
        self.expect("each part is paid on its own, by cash, card or M-Pesa",
                    sorted(s.payment_method for s in sales) == ["CARD", "CASH", "CASH", "MPESA"]
                    and abs(paid - whole) <= D("0.04"), f"paid {paid} for {whole}")
        out = sum((m.quantity for m in StockMovement.objects.filter(
            reference__in=[s.reference for s in sales], product=self.tusker)), D("0"))
        self.expect("stock: exactly the 5 beers left the shelf across the parts", out == D("5"), f"{out}")
        self.expect("no part of the table is left open",
                    not POSOrder.objects.filter(table=table, status__in=POSOrder.LIVE).exists())

        # the screen: PIN in, split from the bill page
        cashier = self.login(self.cashier)
        bill2 = restaurant.open_order(self.station, self.waiter, table=table)
        restaurant.add_item(bill2, self.i_tusker, "2", self.waiter)
        cashier.post(reverse("pos:station_pin", args=[self.station.pk]),
                     {"staff": self.waiter.pk, "pin": "4826", "bill": bill2.pk})
        page = cashier.get(reverse("pos:bill_detail", args=[bill2.pk]))
        self.expect("the bill page has the Split button", page.status_code == 200 and b'data-open="dlg-split"' in page.content,
                    str(page.status_code))
        r = cashier.post(reverse("pos:bill_split", args=[bill2.pk]), {"mode": "even", "ways": "2"})
        self.expect("splitting from the screen makes two bills",
                    r.status_code == 302 and POSOrder.objects.filter(table=table, status__in=POSOrder.LIVE).count() == 2,
                    str(r.status_code))
        floor = cashier.get(reverse("pos:station_floor", args=[self.station.pk]))
        self.expect("the floor shows the table with 2 bills", b"2 bills (split)" in floor.content)
        r = self.login(self.viewer).post(reverse("pos:bill_split", args=[bill2.pk]), {"mode": "even", "ways": "2"})
        self.expect("a viewer cannot split a bill", r.status_code == 403, str(r.status_code))
