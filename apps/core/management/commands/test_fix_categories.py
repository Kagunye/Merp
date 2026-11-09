"""Regression checks for multi-level product categories.

Usage:
    python manage.py test_fix_categories          # rolls the whole run back
    python manage.py test_fix_categories --keep   # leave the test company behind

One company, set up through the company screen, and a second one to prove
the tree never leaks between companies. Everything is driven through the
real views as a company ADMIN, with role checks as a VIEWER, a cashier and a
warehouse manager.

    1. depth rule            -> level 1, 2 and 3 are accepted; a 4th level is
                                refused by the form and by the model
    2. cycles                -> a category cannot be its own parent or sit
                                under one of its own descendants
    3. sibling names         -> unique among siblings ignoring case; the same
                                name under another parent is fine
    4. moving deeper         -> moving a category with children under a
                                level-2 category is refused; the parent picker
                                offers only valid parents
    5. helpers               -> level, path, path_label, descendant_ids, tree
    6. children endpoint     -> level 1 with no parent, children of a parent,
                                another company's id is a 404, any role that
                                sees products may read it
    7. product filtering     -> product list, stock levels, movements and the
                                count sheet filter by a category including
                                everything beneath it; the product form posts
                                one category through the picker
    8. deactivation          -> refused while an active product uses the
                                category or a descendant; retires the subtree
                                once empty
    9. sales by category     -> rolled-up rows add up to the totals, and the
                                totals agree with the ledger's revenue and
                                cost of goods sold for the invoices posted
   10. pages and roles       -> every new page renders for ADMIN, 403 for a
                                role without access
"""
import datetime
import json
import re
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db import transaction
from django.test import Client
from django.test.utils import setup_test_environment, teardown_test_environment
from django.urls import reverse

D = Decimal
Z = D("0")

BLANK_LINE = {
    "product": "", "description": "", "quantity": "", "unit_price": "",
    "discount_percent": "0", "tax_rate": "",
}


def formset(prefix, items, blank=BLANK_LINE, blanks=1):
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
        for key, value in (blank or {}).items():
            data[f"{prefix}-{len(items) + j}-{key}"] = "" if value is None else str(value)
    return data


class Command(BaseCommand):
    help = "Regression checks for three-level product categories and the sales-by-category report."

    def add_arguments(self, parser):
        parser.add_argument("--keep", action="store_true",
                            help="Commit the test data instead of rolling it back.")

    def handle(self, *args, **opts):
        self.passed = 0
        self.failures = []
        self.today = datetime.date.today()
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
            self.stdout.write(f"  ok   {label}")
            return True
        self.failures.append(f"{label} — {detail}" if detail else label)
        self.stdout.write(self.style.ERROR(f"  FAIL {label} {detail}"))
        return False

    def flashes(self, client, response):
        if response is None:
            return []
        location = response.get("Location") if response.status_code in (301, 302, 303) else None
        target = client.get(location) if location else response
        ctx = getattr(target, "context", None)
        if not ctx or "messages" not in ctx:
            return []
        return [str(m.message) for m in ctx["messages"]]

    def errors(self, response):
        ctx = getattr(response, "context", None) or {}
        form = ctx.get("form")
        if form is not None and form.errors:
            return [f"{k}: {' '.join(v)}" for k, v in form.errors.items()]
        lines = ctx.get("line_formset")
        if lines is not None:
            return [str(e) for e in lines.non_form_errors()] + [str(dict(e)) for e in lines.errors if e]
        return []

    def send(self, client, label, url, data):
        try:
            response = client.post(url, data)
        except Exception as exc:
            self.expect(f"{label} answers without a server error", False,
                        f"{type(exc).__name__}: {exc}")
            return None
        if response.status_code >= 500:
            self.expect(f"{label} answers without a server error", False,
                        f"HTTP {response.status_code}")
            return None
        return response

    def accepted(self, client, label, url, data):
        response = self.send(client, label, url, data)
        ok = response is not None and response.status_code in (301, 302, 303)
        self.expect(f"{label} is accepted", ok,
                    "" if ok or response is None else
                    f"HTTP {response.status_code} {self.errors(response)}")
        return response

    def refused(self, client, label, url, data, field=None, needle=None):
        response = self.send(client, label, url, data)
        if response is None:
            return None
        ctx = getattr(response, "context", None) or {}
        form = ctx.get("form")
        errs = dict(form.errors) if form is not None else {}
        ok = response.status_code == 200 and bool(errs)
        if ok and field:
            ok = field in errs
        if ok and needle:
            ok = any(needle.lower() in " ".join(v).lower() for v in errs.values())
        self.expect(f"{label} is refused by the form", ok,
                    f"HTTP {response.status_code} {errs}")
        return response

    def model_refused(self, label, fn, field):
        try:
            fn()
        except ValidationError as exc:
            names = set(getattr(exc, "error_dict", {}) or {})
            return self.expect(label, field in names, str(exc))
        return self.expect(label, False, "no ValidationError")

    def get(self, client, label, url):
        try:
            response = client.get(url)
        except Exception as exc:
            self.expect(f"{label} renders", False, f"{type(exc).__name__}: {exc}")
            return None
        return response

    # ─────────────────────────────── fixtures ────────────────────────────────

    def make_user(self, tag):
        from apps.accounts.models import User
        name = f"catfix.{tag}.{self.stamp}".lower()
        user = User.objects.create(username=name, email=f"{name}@smoke.local",
                                   first_name=tag.title(), last_name="Cat")
        user.set_password("smoke-test-pw")
        user.save()
        return user

    def login(self, user, company):
        client = Client(raise_request_exception=False)
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        session = client.session
        session["active_company_id"] = str(company.id)
        session.save()
        return client

    def make_company(self, client, name):
        from apps.organizations.models import Company, CompanyMembership
        client.post(reverse("organizations:company_create"), {
            "name": name, "legal_name": f"{name} Limited", "registration_number": "",
            "tax_id": "", "vat_number": "", "currency": "KES", "timezone": "Africa/Nairobi",
            "fiscal_year_start_month": "1", "industry": "Retail", "phone": "", "email": "",
            "website": "", "address_line1": "", "address_line2": "", "city": "Nairobi",
            "state_province": "", "postal_code": "", "country": "Kenya",
        })
        company = Company.objects.filter(name=name).first()
        if company is not None:
            CompanyMembership.objects.filter(company=company).update(role="ADMIN")
        return company

    def seat(self, user, company, role):
        from apps.organizations.models import CompanyMembership
        CompanyMembership.objects.update_or_create(
            company=company, user=user, defaults={"role": role, "is_active": True})

    def cat_post(self, name, parent=None, **extra):
        data = {"name": name, "parent": parent.pk if parent else "", "code": "",
                "sort_order": "0", "description": ""}
        data.update(extra)
        return data

    def cat(self, name, parent=None):
        from apps.inventory.models import ProductCategory
        return ProductCategory.objects.filter(
            company=self.company, name=name, parent=parent, is_active=True).first()

    # ──────────────────────────────── the run ────────────────────────────────

    def run(self):
        self.stamp = datetime.datetime.now().strftime("%H%M%S%f")
        self.admin = self.make_user("admin")
        boot = Client()
        boot.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        self.section("0. Two companies")
        self.company = self.make_company(boot, f"Catfix Retail {self.stamp}")
        self.other = self.make_company(boot, f"Catfix Other {self.stamp}")
        if not self.expect("both companies created through the form",
                           self.company is not None and self.other is not None):
            raise SystemExit(1)
        self.client_ = self.login(self.admin, self.company)
        self.client_.get(reverse("inventory:index"))

        self.section("1. Three levels and no more")
        self.check_depth()
        self.section("2. No loops")
        self.check_cycles()
        self.section("3. Names are unique among siblings")
        self.check_siblings()
        self.section("4. A move may not push a subtree past level 3")
        self.check_move_deeper()
        self.section("5. Tree helpers")
        self.check_helpers()
        self.section("6. The children endpoint")
        self.check_children_endpoint()
        self.section("7. Filters take in every level beneath")
        self.check_filters()
        self.section("8. Retiring a category")
        self.check_deactivate()
        self.section("9. Sales by category agrees with the ledger")
        self.check_report()
        self.section("10. New pages render, and refuse roles without access")
        self.check_pages()

    # ──────────────────────────────── checks ─────────────────────────────────

    def check_depth(self):
        from apps.inventory.models import ProductCategory
        c, url = self.client_, reverse("inventory:category_create")
        self.accepted(c, "level 1: DRINKS", url, self.cat_post("DRINKS"))
        drinks = self.cat("DRINKS")
        self.accepted(c, "level 2: DRINKS > Carbonated", url, self.cat_post("Carbonated", drinks))
        carbonated = self.cat("Carbonated", drinks)
        self.accepted(c, "level 3: DRINKS > Carbonated > Sugar", url,
                      self.cat_post("Sugar", carbonated))
        sugar = self.cat("Sugar", carbonated)
        if not self.expect("the three levels were saved", bool(drinks and carbonated and sugar)):
            raise SystemExit(1)
        self.drinks, self.carbonated, self.sugar = drinks, carbonated, sugar
        self.refused(c, "a fourth level under Sugar", url, self.cat_post("Cans", sugar),
                     field="parent")
        self.expect("nothing was saved at level 4",
                    not ProductCategory.objects.filter(company=self.company, name="Cans").exists())
        self.model_refused(
            "the model refuses level 4 on its own",
            lambda: ProductCategory(company=self.company, name="Cans", parent=sugar).full_clean(),
            "parent")
        # The rest of the tree used below.
        self.accepted(c, "DRINKS > Carbonated > Sugar-free", url,
                      self.cat_post("Sugar-free", carbonated))
        self.accepted(c, "DRINKS > Juice", url, self.cat_post("Juice", drinks))
        self.juice = self.cat("Juice", drinks)
        self.accepted(c, "DRINKS > Juice > 340ml", url, self.cat_post("340ml", self.juice))
        self.accepted(c, "DRINKS > Juice > 500ml", url, self.cat_post("500ml", self.juice))
        self.accepted(c, "FOOD", url, self.cat_post("FOOD"))
        self.food = self.cat("FOOD")
        self.sugar_free = self.cat("Sugar-free", carbonated)
        self.j340, self.j500 = self.cat("340ml", self.juice), self.cat("500ml", self.juice)

    def check_cycles(self):
        from apps.inventory.models import ProductCategory
        c = self.client_
        url = reverse("inventory:category_update", args=[self.drinks.pk])
        self.refused(c, "DRINKS as its own parent", url,
                     self.cat_post("DRINKS", self.drinks), field="parent")
        self.refused(c, "DRINKS under its own grandchild Sugar", url,
                     self.cat_post("DRINKS", self.sugar), field="parent")
        self.drinks.refresh_from_db()
        self.expect("DRINKS is still at the top", self.drinks.parent_id is None)
        loop = ProductCategory.objects.get(pk=self.carbonated.pk)
        loop.parent = self.sugar
        self.model_refused("the model refuses Carbonated under its own child",
                           loop.full_clean, "parent")

    def check_siblings(self):
        c, url = self.client_, reverse("inventory:category_create")
        self.refused(c, "'carbonated' again under DRINKS (another case)", url,
                     self.cat_post("carbonated", self.drinks), field="name")
        self.refused(c, "a second level-1 'Drinks'", url, self.cat_post("Drinks"), field="name")
        self.accepted(c, "'Sugar' under FOOD (a different parent)", url,
                      self.cat_post("Sugar", self.food))
        self.food_sugar = self.cat("Sugar", self.food)
        self.expect("two categories called Sugar under different parents",
                    bool(self.food_sugar) and self.food_sugar.pk != self.sugar.pk)
        url = reverse("inventory:category_update", args=[self.j500.pk])
        self.refused(c, "renaming Juice > 500ml to '340ML'", url,
                     self.cat_post("340ML", self.juice), field="name")
        # Another company may use the same names freely.
        from apps.inventory.models import ProductCategory
        theirs = ProductCategory(company=self.other, name="DRINKS")
        try:
            theirs.full_clean()
            theirs.save()
            ok = True
        except ValidationError as exc:
            ok = str(exc)
        self.expect("another company may have its own DRINKS", ok is True, str(ok))
        self.their_drinks = theirs

    def check_move_deeper(self):
        c = self.client_
        # Carbonated has children (level 3), so it cannot move under Juice (level 2).
        url = reverse("inventory:category_update", args=[self.carbonated.pk])
        resp = self.refused(c, "moving Carbonated (with children) under DRINKS > Juice",
                            url, self.cat_post("Carbonated", self.juice), field="parent")
        self.carbonated.refresh_from_db()
        self.expect("Carbonated stayed under DRINKS", self.carbonated.parent_id == self.drinks.pk)
        # FOOD has one child level: it may go under a level-1 category, not a level-2 one.
        url = reverse("inventory:category_update", args=[self.food.pk])
        self.refused(c, "moving FOOD (with a child) under DRINKS > Juice", url,
                     self.cat_post("FOOD", self.juice), field="parent")
        page = c.get(url)
        choices = [str(pk) for pk, _ in page.context["form"].fields["parent"].choices if pk]
        self.expect("FOOD's parent picker offers level-1 categories only, not itself",
                    str(self.drinks.pk) in choices and str(self.juice.pk) not in choices
                    and str(self.food.pk) not in choices
                    and str(self.food_sugar.pk) not in choices, f"{len(choices)} choices")
        page = c.get(reverse("inventory:category_create"))
        choices = [str(pk) for pk, _ in page.context["form"].fields["parent"].choices if pk]
        self.expect("a new category may go under levels 1 and 2, never level 3",
                    str(self.drinks.pk) in choices and str(self.juice.pk) in choices
                    and str(self.sugar.pk) not in choices
                    and str(self.their_drinks.pk) not in choices)
        # A leaf may move under a level-2 category.
        url = reverse("inventory:category_update", args=[self.food_sugar.pk])
        self.accepted(c, "moving the childless FOOD > Sugar under DRINKS > Juice", url,
                      self.cat_post("Sugar", self.juice))
        self.food_sugar.refresh_from_db()
        self.expect("the leaf moved to level 3", self.food_sugar.level == 3)
        self.accepted(c, "and back under FOOD as Sugar", url, self.cat_post("Sugar", self.food))

    def check_helpers(self):
        from apps.inventory.models import ProductCategory
        sugar = ProductCategory.objects.get(pk=self.sugar.pk)
        self.expect("level", (self.drinks.level, self.carbonated.level, sugar.level) == (1, 2, 3))
        self.expect("path", [c.pk for c in sugar.path]
                    == [self.drinks.pk, self.carbonated.pk, sugar.pk])
        self.expect("path_label", sugar.path_label == "DRINKS > Carbonated > Sugar",
                    sugar.path_label)
        ids = set(self.drinks.descendant_ids())
        want = {self.drinks.pk, self.carbonated.pk, sugar.pk, self.sugar_free.pk,
                self.juice.pk, self.j340.pk, self.j500.pk}
        self.expect("descendant_ids is self plus everything beneath", ids == want,
                    f"{len(ids)} vs {len(want)}")
        tree = ProductCategory.tree(self.company)
        names = [n["category"].name for n in tree]
        drinks = next((n for n in tree if n["category"].pk == self.drinks.pk), None)
        self.expect("tree() nests level 1 > 2 > 3 for this company only",
                    names.count("DRINKS") == 1 and drinks is not None
                    and {c["category"].name for c in drinks["children"]} == {"Carbonated", "Juice"}
                    and all(g["level"] == 3 for ch in drinks["children"] for g in ch["children"]),
                    str(names))

    def check_children_endpoint(self):
        c = self.client_
        url = reverse("inventory:category_children")
        top = json.loads(c.get(url).content)
        names = [r["name"] for r in top["results"]]
        self.expect("no parent gives level 1", top["parent"] is None
                    and "DRINKS" in names and "FOOD" in names and "Carbonated" not in names,
                    str(names))
        kids = json.loads(c.get(url, {"parent": str(self.drinks.pk)}).content)
        self.expect("children of DRINKS, flagged as having children",
                    {r["name"] for r in kids["results"]} == {"Carbonated", "Juice"}
                    and all(r["has_children"] for r in kids["results"])
                    and kids["parent"]["label"] == "DRINKS")
        leaf = json.loads(c.get(url, {"parent": str(self.sugar.pk)}).content)
        self.expect("a level-3 category has no children", leaf["results"] == [])
        r = c.get(url, {"parent": str(self.their_drinks.pk)})
        self.expect("another company's category is a 404", r.status_code == 404, r.status_code)
        r = c.get(url, {"parent": "not-a-uuid"})
        self.expect("junk is a 404, not a 500", r.status_code == 404, r.status_code)
        for role in ("VIEWER", "POS_CASHIER", "SALES_REP", "WAREHOUSE_MANAGER"):
            user = self.make_user(role)
            self.seat(user, self.company, role)
            client = self.login(user, self.company)
            r = client.get(url)
            self.expect(f"a {role} may read the tree", r.status_code == 200, r.status_code)
        user = self.make_user("hr")
        self.seat(user, self.company, "HR_MANAGER")
        r = self.login(user, self.company).get(url)
        self.expect("an HR manager (no product access) may not", r.status_code == 403,
                    r.status_code)

    def check_filters(self):
        from apps.inventory.models import Product, StockMovement, UnitOfMeasure
        from apps.organizations.models import Warehouse
        c = self.client_
        uom = UnitOfMeasure.objects.filter(company=self.company, abbreviation="PCS").first()
        self.uom = uom
        self.warehouse = Warehouse.objects.filter(company=self.company, is_default=True).first()

        # The product form posts one category id through the picker.
        page = c.get(reverse("inventory:product_create"))
        html = page.content.decode()
        self.expect("the product form draws the cascading picker",
                    "data-category-picker" in html and 'name="category"' in html
                    and "DRINKS &gt; Carbonated &gt; Sugar" in html)
        data = {"sku": "COLA-340", "name": "Cola 340ml", "product_type": "GOODS",
                "category": str(self.sugar.pk), "unit_of_measure": uom.pk,
                "cost_price": "40", "selling_price": "70", "tax_rate": "",
                "reorder_level": "0", "reorder_quantity": "0", "track_inventory": "on",
                "valuation_method": "WAVG", "income_account": "", "cogs_account": "",
                "inventory_account": "", "barcode": "", "description": ""}
        self.accepted(c, "a product filed under DRINKS > Carbonated > Sugar",
                      reverse("inventory:product_create"), data)
        cola = Product.objects.filter(company=self.company, sku="COLA-340").first()
        self.expect("it is saved at level 3", cola is not None and cola.category_id == self.sugar.pk)

        def product(sku, name, cat, price, cost):
            p = Product.objects.create(
                company=self.company, sku=sku, name=name, product_type="GOODS",
                category=cat, unit_of_measure=uom, selling_price=D(price),
                cost_price=D(cost), track_inventory=True)
            StockMovement.objects.create(
                product=p, warehouse=self.warehouse, movement_type="OPENING",
                quantity=D("100"), unit_cost=D(cost), movement_date=self.today,
                reference=f"OPEN-{sku}")
            return p
        StockMovement.objects.create(
            product=cola, warehouse=self.warehouse, movement_type="OPENING",
            quantity=D("100"), unit_cost=D("40"), movement_date=self.today, reference="OPEN-COLA")
        self.cola = cola
        self.diet = product("DIET-500", "Diet cola 500ml", self.sugar_free, "90", "50")
        self.mango = product("MANGO-340", "Mango juice 340ml", self.j340, "80", "45")
        self.orange = product("ORANGE-500", "Orange juice 500ml", self.j500, "110", "60")
        self.juice_box = product("JUICE-MIX", "Juice variety box", self.juice, "500", "300")
        self.rice = product("RICE-2", "Rice 2kg", self.food, "250", "180")
        self.loose = product("MISC-1", "Loose item", None, "20", "10")

        def listed(url, params, key="products"):
            r = c.get(url, params)
            if r.status_code != 200:
                return None
            return r
        r = listed(reverse("inventory:product_list"), {"category": str(self.drinks.pk)})
        skus = {p.sku for p in r.context["products"]} if r else set()
        self.expect("product list: DRINKS takes in all three levels",
                    skus == {"COLA-340", "DIET-500", "MANGO-340", "ORANGE-500", "JUICE-MIX"},
                    str(sorted(skus)))
        r = listed(reverse("inventory:product_list"), {"category": str(self.juice.pk)})
        skus = {p.sku for p in r.context["products"]} if r else set()
        self.expect("product list: Juice takes in 340ml, 500ml and the box at level 2",
                    skus == {"MANGO-340", "ORANGE-500", "JUICE-MIX"}, str(sorted(skus)))
        r = listed(reverse("inventory:product_list"), {"category": str(self.j340.pk), "q": "mango"})
        skus = {p.sku for p in r.context["products"]} if r else set()
        self.expect("product list: category and search combine", skus == {"MANGO-340"})
        r = listed(reverse("inventory:product_list"), {"category": str(self.their_drinks.pk)})
        self.expect("product list: another company's category is ignored, not a leak",
                    r is not None and r.context["selected_category"] == ""
                    and len(r.context["products"]) >= 7)
        r = listed(reverse("inventory:product_list"), {"category": "junk"})
        self.expect("product list: a junk category is ignored", r is not None)

        r = listed(reverse("inventory:stock_levels"), {"category": str(self.carbonated.pk)})
        skus = {s.product.sku for s in r.context["stock_levels"]} if r else set()
        self.expect("stock levels: Carbonated takes in Sugar and Sugar-free",
                    skus == {"COLA-340", "DIET-500"}, str(sorted(skus)))
        self.expect("stock levels: the listed valuation is only those shelves",
                    r is not None and r.context["listed_value"] == D("9000.00"),
                    str(r.context["listed_value"] if r else "-"))

        r = listed(reverse("inventory:movement_list"), {"category": str(self.food.pk)})
        refs = {m.reference for m in r.context["movements"]} if r else set()
        self.expect("movements: FOOD lists only the rice", refs == {"OPEN-RICE-2"}, str(refs))

        count_url = reverse("inventory:inventory_count", args=[self.warehouse.pk])
        r = listed(count_url, {"category": str(self.juice.pk)})
        rows = {s.product.sku for s in r.context["stock_levels"]} if r else set()
        self.expect("count sheet: one category at a time",
                    rows == {"MANGO-340", "ORANGE-500", "JUICE-MIX"}, str(sorted(rows)))
        # Posting a Juice count touches nothing outside Juice, even if a box
        # for another product is sent along.
        from apps.inventory.models import StockLevel
        mango_level = StockLevel.objects.get(product=self.mango, warehouse=self.warehouse)
        rice_level = StockLevel.objects.get(product=self.rice, warehouse=self.warehouse)
        self.send(c, "post the Juice count", count_url, {
            "movement_date": str(self.today), "category": str(self.juice.pk),
            f"count_{mango_level.pk}": "98", f"count_{rice_level.pk}": "1"})
        mango_level.refresh_from_db()
        rice_level.refresh_from_db()
        self.expect("the Juice count moved mango, and left the rice alone",
                    mango_level.quantity == D("98") and rice_level.quantity == D("100"),
                    f"mango {mango_level.quantity} rice {rice_level.quantity}")

    def check_deactivate(self):
        from apps.inventory.models import Product, ProductCategory
        c = self.client_
        url = reverse("inventory:category_deactivate", args=[self.juice.pk])
        resp = self.send(c, "retire Juice while products use it", url, {})
        self.juice.refresh_from_db()
        msgs = " ".join(self.flashes(c, resp))
        self.expect("refused while active products sit under it or beneath",
                    self.juice.is_active and "cannot be retired" in msgs, msgs)
        r = c.get(reverse("inventory:category_deactivate", args=[self.juice.pk]))
        self.expect("retiring is POST only", r.status_code == 405, r.status_code)
        # An empty branch: FOOD > Snacks > Crisps, with no products.
        create = reverse("inventory:category_create")
        self.accepted(c, "FOOD > Snacks", create, self.cat_post("Snacks", self.food))
        snacks = self.cat("Snacks", self.food)
        self.accepted(c, "FOOD > Snacks > Crisps", create, self.cat_post("Crisps", snacks))
        crisps = self.cat("Crisps", snacks)
        # A product that was retired does not hold its category up.
        old = Product.objects.create(company=self.company, sku="OLD-CRISP", name="Old crisps",
                                     category=crisps, unit_of_measure=self.uom,
                                     track_inventory=False, is_active=False)
        resp = self.send(c, "retire Snacks", reverse("inventory:category_deactivate",
                                                       args=[snacks.pk]), {})
        snacks.refresh_from_db()
        crisps.refresh_from_db()
        self.expect("an empty branch retires with its sub-categories",
                    not snacks.is_active and not crisps.is_active)
        r = c.get(reverse("inventory:category_children"), {"parent": str(self.food.pk)})
        names = [x["name"] for x in json.loads(r.content)["results"]]
        self.expect("a retired category leaves the picker", "Snacks" not in names, str(names))
        self.accepted(c, "a new 'Snacks' may now be added under FOOD", create,
                      self.cat_post("Snacks", self.food))
        r = c.get(reverse("inventory:category_update", args=[crisps.pk]))
        self.expect("a retired category cannot be edited", r.status_code == 404, r.status_code)
        r = self.send(c, "retire another company's category",
                      reverse("inventory:category_deactivate", args=[self.their_drinks.pk]), {})
        self.their_drinks.refresh_from_db()
        self.expect("another company's category is a 404 and stays active",
                    r is not None and r.status_code == 404 and self.their_drinks.is_active)
        # Moving the products out first lets it retire.
        lone = ProductCategory.objects.create(company=self.company, name="Seasonal",
                                              parent=self.food)
        Product.objects.filter(pk=self.rice.pk).update(category=lone)
        resp = self.send(c, "retire Seasonal with rice in it",
                         reverse("inventory:category_deactivate", args=[lone.pk]), {})
        lone.refresh_from_db()
        self.expect("refused with the rice in it", lone.is_active)
        Product.objects.filter(pk=self.rice.pk).update(category=self.food)
        self.send(c, "retire Seasonal once the rice moved back",
                  reverse("inventory:category_deactivate", args=[lone.pk]), {})
        lone.refresh_from_db()
        self.expect("retired once empty", not lone.is_active)
        old.delete()

    def check_report(self):
        from apps.accounting.models import JournalEntryLine, TaxRate
        from apps.crm.models import Customer
        from apps.reporting.services import sales_by_category
        from apps.sales.models import Invoice
        c = self.client_
        vat = TaxRate.objects.filter(company=self.company, code="VAT16").first()
        customer = Customer.objects.create(company=self.company, name="Duka la Mama")
        start = set(JournalEntryLine.objects.filter(
            journal_entry__company=self.company).values_list("pk", flat=True))

        def line(product, qty, price):
            return {"product": product.pk, "description": product.name, "quantity": qty,
                    "unit_price": price, "discount_percent": "0",
                    "tax_rate": vat.pk if vat else ""}

        def invoice(ref, lines):
            header = {"reference": ref, "customer": customer.pk,
                      "invoice_date": str(self.today),
                      "due_date": str(self.today + datetime.timedelta(days=30)),
                      "currency": "KES", "notes": ""}
            data = dict(header)
            data.update(formset("lines", lines))
            self.accepted(c, f"invoice {ref}", reverse("sales:invoice_create"), data)
            inv = Invoice.objects.filter(company=self.company, reference=ref).first()
            if inv is not None and not inv.journal_entry_id:
                c.post(reverse("sales:invoice_post", args=[inv.pk]), {})
                inv.refresh_from_db()
            return inv

        a = invoice("CAT-INV-1", [line(self.cola, "10", "70"), line(self.mango, "4", "80"),
                                  line(self.orange, "3", "110")])
        b = invoice("CAT-INV-2", [line(self.diet, "5", "90"), line(self.rice, "2", "250"),
                                  line(self.loose, "7", "20"), line(self.juice_box, "1", "500")])
        if not self.expect("both invoices posted", bool(a and a.journal_entry_id
                                                        and b and b.journal_entry_id)):
            return
        # Two mangoes come back on a credit note.
        mango_line = a.lines.get(product=self.mango)
        c.post(reverse("sales:invoice_credit_note", args=[a.pk]),
               {f"credit_qty_{mango_line.pk}": "2", "reason": "damaged", "goods_returned": "1"})
        note = Invoice.objects.filter(original_invoice=a, status="CREDIT_NOTE").first()
        self.expect("a goods-returned credit note for 2 mangoes", note is not None)

        report = sales_by_category(self.company, self.today, self.today)
        rows, totals = report["rows"], report["totals"]
        new_lines = JournalEntryLine.objects.filter(
            journal_entry__company=self.company, journal_entry__status="POSTED",
            journal_entry__posting_date=self.today).exclude(pk__in=start)
        rev = sum(((l.credit_amount - l.debit_amount) for l in new_lines.filter(
            account__account_type="REVENUE")), Z)
        cogs = sum(((l.debit_amount - l.credit_amount) for l in new_lines.filter(
            account__account_type="COST_OF_GOODS")), Z)
        self.expect("revenue total = ledger revenue for the invoices and the note",
                    totals["revenue"] == rev and rev > 0, f"{totals['revenue']} vs {rev}")
        self.expect("cost of sales total = ledger cost of goods sold",
                    totals["cost"] == cogs and cogs > 0, f"{totals['cost']} vs {cogs}")

        def find(label, level):
            return next((r for r in rows if r["label"] == label and r["level"] == level), None)
        drinks, food, unc = find("DRINKS", 1), find("FOOD", 1), find("Uncategorised", 1)
        top = [r for r in rows if r["level"] == 1]
        self.expect("level-1 rows plus Uncategorised add up to the totals",
                    sum((r["revenue"] for r in top), Z) == totals["revenue"]
                    and sum((r["cost"] for r in top), Z) == totals["cost"]
                    and sum((r["qty"] for r in top), Z) == totals["qty"])
        self.expect("uncategorised products are reported as Uncategorised",
                    unc is not None and unc["revenue"] == D("140.00") and unc["qty"] == D("7"),
                    str(unc and unc["revenue"]))
        self.expect("FOOD: 2 rice, 500.00 revenue, 360.00 cost",
                    food is not None and food["qty"] == D("2") and food["revenue"] == D("500.00")
                    and food["cost"] == D("360.00"), str(food))

        # Each parent's children (with any "Directly under" row) add up to it.
        ok, idx = True, {id(r): i for i, r in enumerate(rows)}
        for i, r in enumerate(rows):
            kids, j = [], i + 1
            while j < len(rows) and rows[j]["level"] > r["level"]:
                if rows[j]["level"] == r["level"] + 1:
                    kids.append(rows[j])
                j += 1
            if kids and r["kind"] == "category":
                for k in ("revenue", "cost", "qty"):
                    if sum((x[k] for x in kids), Z) != r[k]:
                        ok = False
        self.expect("every parent equals the sum of the rows beneath it", ok)
        juice = find("Juice", 2)
        direct = find("Directly under Juice", 3)
        j340, j500 = find("340ml", 3), find("500ml", 3)
        self.expect("Juice: 340ml (4 sold, 2 back) vs 500ml (3) vs the box directly under",
                    juice and direct and j340 and j500
                    and j340["qty"] == D("2") and j340["revenue"] == D("160.00")
                    and j340["cost"] == D("90.00")
                    and j500["qty"] == D("3") and j500["revenue"] == D("330.00")
                    and direct["revenue"] == D("500.00")
                    and juice["revenue"] == D("990.00"),
                    str([(r["label"], r["qty"], r["revenue"], r["cost"]) for r in rows]))
        self.expect("DRINKS rolls up all of it",
                    drinks is not None and drinks["revenue"] == D("700.00") + D("450.00")
                    + D("990.00"), str(drinks and drinks["revenue"]))

        r = c.get(reverse("reporting:sales_by_category"),
                  {"date_from": str(self.today), "date_to": str(self.today)})
        html = r.content.decode() if r.status_code == 200 else ""
        self.expect("the report page renders the same totals",
                    r.status_code == 200 and "Uncategorised" in html and "Directly under Juice" in html
                    and r.context["report"]["totals"]["revenue"] == totals["revenue"],
                    r.status_code)
        r = c.get(reverse("reporting:sales_by_category"), {"source": "pos",
                  "date_from": str(self.today), "date_to": str(self.today)})
        self.expect("till only, with no till sales, is empty",
                    r.status_code == 200 and r.context["report"]["rows"] == [])
        r = c.get(reverse("reporting:sales_by_category"),
                  {"date_from": str(self.today), "date_to": str(self.today - datetime.timedelta(days=3))})
        self.expect("a window that ends before it starts is a message",
                    r.status_code == 200 and r.context["error"])

    def check_pages(self):
        c = self.client_
        pages = [
            ("category list", reverse("inventory:category_list")),
            ("new category", reverse("inventory:category_create")),
            ("edit category", reverse("inventory:category_update", args=[self.juice.pk])),
            ("sales by category", reverse("reporting:sales_by_category")),
            ("product list", reverse("inventory:product_list")),
            ("product form", reverse("inventory:product_edit", args=[self.cola.pk])),
            ("stock levels", reverse("inventory:stock_levels")),
            ("movements", reverse("inventory:movement_list")),
            ("count sheet", reverse("inventory:inventory_count", args=[self.warehouse.pk])),
            ("inventory home", reverse("inventory:index")),
            ("reports home", reverse("reporting:index")),
        ]
        for label, url in pages:
            r = self.get(c, label, url)
            self.expect(f"ADMIN: {label} renders", r is not None and r.status_code == 200,
                        getattr(r, "status_code", "-"))
        html = c.get(reverse("inventory:category_list")).content.decode()
        self.expect("the tree page shows the three levels with counts incl. descendants",
                    "DRINKS" in html and "Level 3" in html and "340ml" in html)
        r = c.get(reverse("inventory:category_list"))
        drinks = next((x for x in r.context["rows"] if x["category"].pk == self.drinks.pk), None)
        self.expect("DRINKS counts the products in all its levels", drinks and drinks["total"] == 5
                    and drinks["direct"] == 0, str(drinks and drinks["total"]))
        idx = c.get(reverse("inventory:index")).content.decode()
        self.expect("the inventory home links the category tree and the report",
                    reverse("inventory:category_list") in idx
                    and reverse("reporting:sales_by_category") in idx)
        self.expect("the reports home links the report",
                    reverse("reporting:sales_by_category") in c.get(
                        reverse("reporting:index")).content.decode())

        roles = {"VIEWER": {"category_list": 403, "sales_by_category": 403},
                 "POS_CASHIER": {"category_list": 403, "sales_by_category": 403},
                 "SALES_MANAGER": {"category_list": 403, "sales_by_category": 200},
                 "AUDITOR": {"category_list": 200, "sales_by_category": 200},
                 "WAREHOUSE_MANAGER": {"category_list": 200, "sales_by_category": 403},
                 "ACCOUNTANT": {"category_list": 200, "sales_by_category": 200}}
        for role, want in roles.items():
            user = self.make_user(f"pg{role}")
            self.seat(user, self.company, role)
            client = self.login(user, self.company)
            got = {"category_list": client.get(reverse("inventory:category_list")).status_code,
                   "sales_by_category": client.get(reverse("reporting:sales_by_category")).status_code}
            self.expect(f"{role}: tree {want['category_list']}, report {want['sales_by_category']}",
                        got == want, str(got))
            create = client.post(reverse("inventory:category_create"), self.cat_post(f"X{role}"))
            may = role == "WAREHOUSE_MANAGER"
            self.expect(f"{role}: {'may' if may else 'may not'} create a category",
                        (create.status_code in (301, 302)) if may else create.status_code == 403,
                        create.status_code)
            if role == "AUDITOR":
                html = client.get(reverse("inventory:category_list")).content.decode()
                self.expect("an auditor sees no Edit, Retire or New buttons",
                            reverse("inventory:category_deactivate", args=[self.juice.pk]) not in html
                            and "New Category" not in html
                            and reverse("inventory:category_update", args=[self.juice.pk]) not in html)
            if role == "SALES_MANAGER":
                html = client.get(reverse("inventory:index")).content.decode()
                self.expect("a sales manager is shown the report tile, not the tree",
                            reverse("reporting:sales_by_category") in html
                            and f'href="{reverse("inventory:category_list")}"' not in html)
            if role == "VIEWER":
                html = client.get(reverse("inventory:product_list")).content.decode()
                self.expect("a viewer still gets the category filter on the product list",
                            "data-category-picker" in html
                            and f'href="{reverse("inventory:category_list")}"' not in html)
