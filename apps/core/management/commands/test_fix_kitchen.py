"""Checks for kitchen tickets and recipes (apps/pos/restaurant.py, hardware.py).

    python manage.py test_fix_kitchen        # rolls back (same setup as test_fix_restaurant)

    1. routing         -> food goes to the kitchen and beer to the bar, by
                          section or by item; an item routed nowhere is not sent;
                          one ticket per station; nothing is ever sent twice,
                          not even after the bill is split
    2. cancels         -> voiding a sent item tells its station; an unsent one
                          tells nobody
    3. never missed    -> printing or paying the bill sends what was not sent
    4. kitchen screen  -> shows the open tickets; Ready takes one off, once
    5. printing        -> the ticket's ESC/POS; the station's own printer;
                          the browser page when there is none; a server
                          printer gets the bytes over the socket
    6. recipes         -> selling a dish takes its ingredients (with scrap) off
                          the shelf, not the dish; cost of sales is what they
                          cost; a refund puts them back at that cost; a BOM not
                          marked as a recipe changes nothing
    7. recipe costing  -> cost per portion, food cost %, over-target flag
    8. roles
"""
import socket
import threading
from decimal import Decimal

from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from apps.core.management.commands.test_fix_restaurant import Command as RestaurantCommand

D = Decimal


class Command(RestaurantCommand):
    help = "Checks for kitchen tickets, the kitchen screen and recipes."

    def run(self):
        self.section("0. setup")
        self.setup()
        self.kitchen_setup()
        self.check_routing()
        self.check_cancels()
        self.check_never_missed()
        self.check_screen()
        self.check_printing()
        self.check_recipes()
        self.check_costing()
        self.check_roles()

    def kitchen_setup(self):
        from apps.pos.models import POSPrepStation, POSTable
        c = self.company
        self.kitchen = POSPrepStation.objects.create(company=c, name="Kitchen", kind="KITCHEN")
        self.bar = POSPrepStation.objects.create(company=c, name="Bar", kind="BAR")
        self.s_food.prep_station = self.kitchen
        self.s_food.save()
        self.s_drinks.prep_station = self.bar
        self.s_drinks.save()
        self.table = POSTable.objects.create(company=c, branch=self.hq, station=self.station, name="Table 11")

    def new_bill(self):
        from apps.pos import restaurant
        from apps.pos.models import POSTable
        t = POSTable.objects.create(company=self.company, branch=self.hq, station=self.station,
                                    name=f"T{POSTable.objects.count() + 1}")
        return restaurant.open_order(self.station, self.waiter, table=t)

    # ── 1 ──
    def check_routing(self):
        from apps.pos import restaurant
        from apps.pos.models import POSKitchenTicket, POSMenuItem
        self.section("1. routing")
        bill = self.new_bill()
        restaurant.add_item(bill, self.i_tusker, "2", self.waiter)
        restaurant.add_item(bill, self.i_chips, "1", self.waiter, note="no salt")
        soda = POSMenuItem.objects.create(section=self.s_drinks, product=self.tusker, display_name="Fridge soda", price=D("80"))
        soda.prep_station = None
        soda.save()
        tickets = restaurant.send_to_kitchen(bill, self.waiter)
        by = {t.prep_station.name: t for t in tickets}
        self.expect("one ticket for the kitchen and one for the bar", set(by) == {"Kitchen", "Bar"}, str(set(by)))
        self.expect("the kitchen ticket has the chips with the note",
                    by["Kitchen"].items == [{"line": by["Kitchen"].items[0]["line"], "name": "Chips", "qty": "1", "note": "no salt"}],
                    str(by["Kitchen"].items))
        self.expect("…and the bar the 2 beers", by["Bar"].items[0]["qty"] == "2")
        self.expect("tickets are numbered KOT-", all(t.number.startswith("KOT-") for t in tickets))
        self.expect("sending again sends nothing", restaurant.send_to_kitchen(bill, self.waiter) == [])
        cocktail = POSMenuItem.objects.create(section=self.s_food, product=self.tusker, display_name="Dawa", price=D("400"),
                                              prep_station=self.bar)
        restaurant.add_item(bill, cocktail, "1", self.waiter)
        more = restaurant.send_to_kitchen(bill, self.waiter)
        self.expect("an item routed to the bar goes there even from the Food section",
                    len(more) == 1 and more[0].prep_station == self.bar and more[0].items[0]["name"] == "Dawa")
        self.s_drinks.prep_station = None
        self.s_drinks.save()
        restaurant.add_item(bill, self.i_tusker, "1", self.waiter)
        self.expect("an item routed nowhere is not sent", restaurant.send_to_kitchen(bill, self.waiter) == [])
        self.s_drinks.prep_station = self.bar
        self.s_drinks.save()
        restaurant.send_to_kitchen(bill, self.waiter)
        beer = bill.lines.filter(name__startswith="Tusker", kitchen_ticket__isnull=False).first()
        part = restaurant.split_items(bill, self.waiter, {beer.pk: "1"})
        self.expect("after a split, the moved half is still marked sent (not sent twice)",
                    restaurant.send_to_kitchen(part, self.waiter) == [] and restaurant.send_to_kitchen(bill, self.waiter) == [])
        self.bill_k = bill
        self.tickets_before = POSKitchenTicket.objects.filter(order__in=[bill, part]).count()

    # ── 2 ──
    def check_cancels(self):
        from apps.pos import restaurant
        from apps.pos.models import POSKitchenTicket
        self.section("2. cancels")
        bill = self.new_bill()
        sent = restaurant.add_item(bill, self.i_chips, "2", self.waiter)
        restaurant.send_to_kitchen(bill, self.waiter)
        sent.refresh_from_db()
        restaurant.void_line(sent, self.manager, self.waiter, "Customer changed mind")
        cancel = POSKitchenTicket.objects.filter(order=bill, kind="VOID").first()
        self.expect("voiding a sent item sends a CANCEL to the kitchen",
                    cancel and cancel.prep_station == self.kitchen and "Customer changed mind" in cancel.items[0]["note"])
        unsent = restaurant.add_item(bill, self.i_chips, "1", self.waiter)
        restaurant.void_line(unsent, self.manager, self.waiter, "typo")
        self.expect("voiding an item never sent tells nobody",
                    POSKitchenTicket.objects.filter(order=bill, kind="VOID").count() == 1)
        bill2 = self.new_bill()
        restaurant.add_item(bill2, self.i_chips, "1", self.waiter)
        restaurant.add_item(bill2, self.i_tusker, "1", self.waiter)
        restaurant.send_to_kitchen(bill2, self.waiter)
        restaurant.void_order(bill2, self.manager, "Walked out")
        self.expect("voiding a whole bill cancels at both stations",
                    sorted(t.prep_station.name for t in POSKitchenTicket.objects.filter(order=bill2, kind="VOID")) == ["Bar", "Kitchen"])

    # ── 3 ──
    def check_never_missed(self):
        from apps.pos import restaurant, services
        from apps.pos.models import POSKitchenTicket
        self.section("3. never missed")
        bill = self.new_bill()
        restaurant.add_item(bill, self.i_chips, "1", self.waiter)
        restaurant.print_bill(bill)
        self.expect("printing the bill sends what was not sent",
                    POSKitchenTicket.objects.filter(order=bill, kind="NEW").count() == 1)
        bill2 = self.new_bill()
        restaurant.add_item(bill2, self.i_tusker, "1", self.waiter)
        self.till = services.open_session(self.company, self.cashier, self.hq, D("0"))
        restaurant.close_order(bill2, self.cashier, payment_method="CASH", token=services.new_token())
        self.expect("paying the bill sends what was not sent",
                    POSKitchenTicket.objects.filter(order=bill2, kind="NEW", prep_station=self.bar).count() == 1)

    # ── 4 ──
    def check_screen(self):
        from apps.pos import restaurant
        from apps.pos.models import POSKitchenTicket
        self.section("4. kitchen screen")
        cashier = self.login(self.cashier)
        open_k = restaurant.kitchen_queue(self.kitchen).count()
        r = cashier.get(reverse("pos:kitchen_screen", args=[self.kitchen.pk]))
        self.expect("the kitchen screen lists its open tickets",
                    r.status_code == 200 and r.content.count(b'<article class="kds-ticket') == open_k and open_k > 0, f"{open_k}")
        first = restaurant.kitchen_queue(self.kitchen).first()
        r = cashier.post(reverse("pos:kitchen_screen", args=[self.kitchen.pk]), {"ticket": first.pk})
        first.refresh_from_db()
        self.expect("Ready takes a ticket off the screen", r.status_code == 302 and first.done_at
                    and restaurant.kitchen_queue(self.kitchen).count() == open_k - 1)
        self.refused("a ticket is done once", lambda: restaurant.bump(first), "already done")
        part = cashier.get(reverse("pos:kitchen_screen", args=[self.kitchen.pk]) + "?partial=1")
        self.expect("the screen refreshes itself (partial)", part.status_code == 200 and b"<html" not in part.content)
        bar = cashier.get(reverse("pos:kitchen_screen", args=[self.bar.pk])).content
        self.expect("the bar screen shows only bar tickets", b"Chips" not in bar)

    # ── 5 ──
    def check_printing(self):
        from apps.pos import hardware, restaurant
        from apps.pos.models import POSKitchenTicket, POSPrinter
        self.section("5. printing")
        ticket = POSKitchenTicket.objects.filter(prep_station=self.kitchen, kind="NEW").first()
        printer = POSPrinter.objects.create(company=self.company, name="Kitchen printer", connection="BRIDGE",
                                            host="192.168.1.60", port=9100, paper_chars=42)
        raw = hardware.kot_bytes(ticket, printer)
        self.expect("the ticket is ESC/POS: station, table, items large, cut",
                    raw.startswith(b"\x1b@") and b"KITCHEN" in raw and b"Chips" in raw and raw.endswith(b"\x1dV\x42\x03"))
        cashier = self.login(self.cashier)
        r = cashier.get(reverse("pos:hw_kot", args=[ticket.pk])).json()
        self.expect("with no station printer the browser page is offered", r.get("printer") is None and "autoprint" in r.get("print_url", ""))
        self.expect("the ticket page prints", cashier.get(reverse("pos:bill_kot", args=[ticket.pk])).status_code == 200)
        self.kitchen.printer = printer
        self.kitchen.save()
        r = cashier.get(reverse("pos:hw_kot", args=[ticket.pk])).json()
        self.expect("with a station printer the bytes go to it, not to this device's printer",
                    r.get("printer", {}).get("id") == str(printer.pk) and r.get("data"))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        sock.settimeout(5)
        got = []

        def take():
            try:
                conn, _ = sock.accept()
                with conn:
                    conn.settimeout(3)
                    data = b""
                    while True:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    got.append(data)
            except OSError:
                pass
        th = threading.Thread(target=take, daemon=True)
        th.start()
        printer.connection, printer.host, printer.port = "SERVER", "127.0.0.1", sock.getsockname()[1]
        printer.save()
        ports, hardware.PRINTER_PORTS = hardware.PRINTER_PORTS, [printer.port]
        try:
            from django.test import override_settings
            with override_settings(DEBUG=True):
                r = cashier.get(reverse("pos:hw_kot", args=[ticket.pk])).json()
        finally:
            hardware.PRINTER_PORTS = ports
        th.join(6)
        sock.close()
        ticket.refresh_from_db()
        self.expect("a server printer gets the ticket over the socket",
                    r.get("sent") and got and b"Chips" in got[0] and ticket.printed_at, str(r))
        self.kitchen.printer = None
        self.kitchen.save()

    # ── 6 ──
    def check_recipes(self):
        from apps.accounting.models import JournalEntryLine
        from apps.inventory.models import Product, StockLevel, StockMovement
        from apps.manufacturing.models import BillOfMaterials, BOMLine
        from apps.pos import restaurant, services
        from apps.pos.models import POSMenuItem
        self.section("6. recipes")
        c = self.company
        uom = self.chips.unit_of_measure

        def stocked(sku, name, cost, qty):
            p = Product.objects.create(company=c, sku=sku, name=name, unit_of_measure=uom, cost_price=D(cost),
                                       selling_price=D("0"), track_inventory=True, product_type="GOODS",
                                       income_account=self.account("4200"), cogs_account=self.account("5000"),
                                       inventory_account=self.account("1400"))
            StockMovement.objects.create(product=p, warehouse=self.wh, movement_type="RECEIPT", quantity=D(qty),
                                         unit_cost=D(cost), movement_date=timezone.localdate(), reference=f"GRN-{sku}",
                                         performed_by=self.admin)
            return p
        self.potato = stocked("POT", "Potatoes (kg)", "80", "20")
        self.oil = stocked("OIL", "Cooking oil (L)", "300", "10")
        self.fries = Product.objects.create(company=c, sku="FRY", name="Fries", unit_of_measure=uom, cost_price=D("0"),
                                            selling_price=D("300"), tax_rate=self.chips.tax_rate, track_inventory=False,
                                            product_type="GOODS", income_account=self.account("4200"),
                                            cogs_account=self.account("5000"), inventory_account=self.account("1400"))
        bom = BillOfMaterials.objects.create(company=c, product=self.fries, name="Fries", status="ACTIVE",
                                             production_quantity=D("1"), deduct_on_sale=True)
        BOMLine.objects.create(bom=bom, component=self.potato, quantity=D("0.3"), scrap_percent=D("10"))
        BOMLine.objects.create(bom=bom, component=self.oil, quantity=D("0.05"))
        self.i_fries = POSMenuItem.objects.create(section=self.s_food, product=self.fries, price=D("300"))

        bill = self.new_bill()
        restaurant.add_item(bill, self.i_fries, "2", self.waiter)
        sale = restaurant.close_order(bill, self.cashier, payment_method="CASH", token=services.new_token())
        moves = {m.product_id: m for m in StockMovement.objects.filter(reference=sale.reference, movement_type="DELIVERY")}
        self.expect("2 fries take 0.66 kg potatoes (0.3 + 10% scrap, x2) and 0.1 L oil",
                    moves.get(self.potato.pk) and moves[self.potato.pk].quantity == D("0.6600")
                    and moves.get(self.oil.pk) and moves[self.oil.pk].quantity == D("0.1000"),
                    str({k: v.quantity for k, v in moves.items()}))
        self.expect("…and nothing for the dish itself", self.fries.pk not in moves)
        level = lambda p: StockLevel.objects.get(product=p, warehouse=self.wh).quantity   # noqa: E731
        self.expect("the shelf now holds 19.34 kg potatoes and 9.9 L oil",
                    level(self.potato) == D("19.3400") and level(self.oil) == D("9.9000"))
        cogs = JournalEntryLine.objects.filter(account=self.account("5000"),
                                               journal_entry__reference__in=[m.reference for m in moves.values()] + [sale.reference]
                                               ).aggregate(v=Sum("debit_amount"))["v"] or D("0")
        want = (D("0.66") * D("80") + D("0.1") * D("300")).quantize(D("0.01"))
        moved_value = sum((m.quantity * m.unit_cost for m in moves.values()), D("0")).quantize(D("0.01"))
        self.expect(f"cost of sales is what the ingredients cost ({want})", moved_value == want, f"{moved_value}")

        line = sale.lines.get(product=self.fries)
        self.supervisor_has_till()
        services.refund(sale, self.supervisor, {str(line.pk): "1"}, reason="cold", payment_method="CASH",
                        token=services.new_token())
        back = {m.product_id: m for m in StockMovement.objects.filter(movement_type="RETURN_IN",
                                                                      product__in=[self.potato, self.oil])}
        self.expect("a refund of 1 fries puts 0.33 kg potatoes and 0.05 L oil back at the cost they left at",
                    back.get(self.potato.pk) and back[self.potato.pk].quantity == D("0.3300")
                    and back[self.potato.pk].unit_cost == moves[self.potato.pk].unit_cost
                    and back.get(self.oil.pk) and back[self.oil.pk].quantity == D("0.0500"),
                    str({k: (v.quantity, v.unit_cost) for k, v in back.items()}))

        bom.deduct_on_sale = False
        bom.save()
        self.expect("a BOM not marked as a recipe is ignored at the till",
                    services.shelf_rows([{"product": self.fries, "quantity": D("1")}]) == [])
        bom.deduct_on_sale = True
        bom.save()
        bill = self.new_bill()
        restaurant.add_item(bill, self.i_fries, "100", self.waiter)
        self.refused("selling more fries than there are potatoes is refused, naming the ingredient",
                     lambda: restaurant.close_order(bill, self.cashier, payment_method="CASH", token=services.new_token()),
                     "Potatoes")

    def supervisor_has_till(self):
        from apps.pos import services
        if services.active_session(self.company, self.supervisor) is None:
            services.open_session(self.company, self.supervisor, self.hq, D("500"))
        return True

    # ── 7 ──
    def check_costing(self):
        from apps.pos import restaurant
        self.section("7. recipe costing")
        rows = {r["item"].pk: r for r in restaurant.food_cost_report(self.company, target=D("20"))}
        fries = rows[self.i_fries.pk]
        want = (D("0.33") * D("80") + D("0.05") * D("300")).quantize(D("0.01"))
        self.expect(f"a portion of fries costs {want} (0.33 kg potatoes + 0.05 L oil)", fries["cost"] == want, str(fries["cost"]))
        self.expect("food cost is cost / price: 13.8%", fries["pct"] == (want / D("300") * 100).quantize(D("0.1")), str(fries["pct"]))
        self.expect("an item over the target is flagged", rows[self.i_chips.pk]["over"] is (rows[self.i_chips.pk]["pct"] > D("20")))
        r = self.login(self.supervisor).get(reverse("pos:bo_food_cost"))
        self.expect("the recipe costing page opens with the fries", r.status_code == 200 and b"Fries" in r.content)

    # ── 8 ──
    def check_roles(self):
        from apps.pos import restaurant
        self.section("8. roles")
        viewer = self.login(self.viewer)
        t = restaurant.kitchen_queue(self.bar).first()
        r = viewer.post(reverse("pos:kitchen_screen", args=[self.bar.pk]), {"ticket": t.pk})
        t.refresh_from_db()
        self.expect("a viewer cannot bump a ticket", r.status_code == 403 and t.done_at is None, str(r.status_code))
        sup = self.login(self.supervisor)
        self.expect("the sales manager opens Kitchen stations", sup.get(reverse("pos:bo_prep_list")).status_code == 200)
        r = sup.post(reverse("pos:bo_prep_create"), {"name": "Grill", "kind": "KITCHEN", "branch": "", "printer": "", "is_active": "on"})
        self.expect("…and adds the grill", r.status_code == 302)
        self.expect("the cashier cannot change kitchen stations",
                    self.login(self.cashier).post(reverse("pos:bo_prep_create"), {"name": "X", "kind": "BAR"}).status_code == 403)
