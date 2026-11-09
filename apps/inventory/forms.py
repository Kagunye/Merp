"""Inventory forms."""
from decimal import Decimal

from django import forms
from apps.core.forms import date_input
from .numbers import PlainNumbersMixin
from .models import Product, StockMovement, ProductCategory, UnitOfMeasure


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            "sku", "name", "product_type", "category", "unit_of_measure",
            "cost_price", "selling_price", "tax_rate", "reorder_level", "reorder_quantity",
            "track_inventory", "allow_negative_stock", "valuation_method",
            "income_account", "cogs_account", "inventory_account",
            "barcode", "image", "description",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not company:
            return
        from apps.accounting.models import Account, TaxRate
        from apps.core.services import create_default_units
        from .models import ProductCategory, UnitOfMeasure
        # A product without a unit cannot be counted, and an empty required
        # dropdown would leave the bookkeeper stuck. Companies provisioned
        # before units existed get the standard set on first use.
        if not UnitOfMeasure.objects.filter(company=company).exists():
            create_default_units(company)
        self.fields["category"].queryset = ProductCategory.objects.filter(company=company, is_active=True)
        self.fields["category"].empty_label = "— No category"
        # The cascading Level 1 / 2 / 3 picker; it still posts one `category`.
        self.fields["category"].widget = CategoryPickerWidget(company=company)
        self.fields["unit_of_measure"].queryset = UnitOfMeasure.objects.filter(company=company, is_active=True)
        self.fields["tax_rate"].queryset = TaxRate.objects.filter(company=company, is_active=True)
        self.fields["tax_rate"].empty_label = "— Use default tax"
        from apps.accounting.gl_service import stock_holding_accounts
        accounts = Account.objects.filter(company=company, is_active=True)
        for fname, acct_type in (
            ("income_account", "REVENUE"),
            ("cogs_account", "COST_OF_GOODS"),
        ):
            field = self.fields[fname]
            field.queryset = accounts.filter(account_type=acct_type)
            field.empty_label = "— System default"
        # Stock is held only on an account that holds stock: 1400 and the
        # accounts beside it (1410 Spares, say). Work in progress (1450) is
        # where materials wait while they are being made into something; a
        # product kept there would be drawn into production and out again on
        # the same account. Banks, receivables, the receipt accrual, the
        # revaluation and depreciation accounts and the headers above other
        # accounts each answer for something else entirely.
        self.fields["inventory_account"].queryset = stock_holding_accounts(company)
        self.fields["inventory_account"].empty_label = "— System default"
        self.company = company

    # A price or a reorder point below zero has no meaning: a negative cost
    # would value stock at less than nothing on the balance sheet, a negative
    # selling price would turn every sale into a credit note, and a negative
    # reorder level would never trigger a reorder.
    NON_NEGATIVE = {
        "cost_price": "The cost price can't be negative. Enter 0 or more.",
        "selling_price": "The selling price can't be negative. Enter 0 or more.",
        "reorder_level": "The reorder level can't be negative. Enter 0 or more.",
        "reorder_quantity": "The reorder quantity can't be negative. Enter 0 or more.",
    }

    def _non_negative(self, name):
        value = self.cleaned_data.get(name)
        if value is not None and value < 0:
            raise forms.ValidationError(self.NON_NEGATIVE[name])
        return value

    def clean_cost_price(self):
        return self._non_negative("cost_price")

    def clean_selling_price(self):
        return self._non_negative("selling_price")

    def clean_reorder_level(self):
        return self._non_negative("reorder_level")

    def clean_reorder_quantity(self):
        return self._non_negative("reorder_quantity")

    def clean(self):
        """Refuse the two edits that would break the inventory account.

        Stock already on a shelf was debited to the product's inventory account
        when it arrived, and goods received and not yet billed are standing on
        the received-not-invoiced accrual against it. While either is so:

        * turning stock tracking off would make those goods a service. Nothing
          takes them off the shelf any more, a sale no longer costs them out,
          and their bill charges them to expense — the inventory account keeps
          their value for ever, with no stock behind it;
        * moving the product to another inventory account would leave the old
          account holding the value of the goods already there, while every
          issue from now on credits the new one and drives it below nothing.

        So both wait until the shelves are empty and every receipt is billed.
        Sell, use or write off what is on hand (or bill what arrived) first.
        """
        cleaned = super().clean()
        product = self.instance
        if product._state.adding or not product.pk:
            return cleaned
        from apps.accounting.models import Account
        from .models import Product, StockLevel

        saved = Product.objects.filter(pk=product.pk).select_related("inventory_account").first()
        if saved is None:
            return cleaned
        self._check_unit_change(saved, cleaned)
        self._check_retracking(saved, cleaned)
        untracking = saved.track_inventory and not cleaned.get("track_inventory")
        default = Account.objects.filter(company=saved.company, code="1400").first()
        was = saved.inventory_account or default
        now = cleaned.get("inventory_account") or default
        moving = saved.track_inventory and (getattr(was, "pk", None) != getattr(now, "pk", None))
        if not (untracking or moving):
            return cleaned

        levels = StockLevel.objects.filter(product=saved).exclude(quantity=0, value=0)
        on_hand = sum((l.quantity for l in levels), Decimal("0"))
        worth = sum((l.value for l in levels), Decimal("0"))
        from apps.procurement.models import PurchaseOrderLine
        unbilled = [l for l in PurchaseOrderLine.objects.filter(
            product=saved, order__company=saved.company).exclude(
            order__status="CANCELLED").select_related("order") if l.to_bill > 0]
        if not (levels.exists() or unbilled):
            return cleaned

        from .numbers import money, qty as show_qty
        reasons = []
        if levels.exists():
            reasons.append(f"{show_qty(on_hand)} {saved.unit_of_measure} worth "
                           f"{money(worth)} is on hand")
        if unbilled:
            refs = ", ".join(sorted({l.order.reference for l in unbilled}))
            reasons.append(f"goods received on {refs} have not been billed yet")
        why = " and ".join(reasons)
        if untracking:
            self.add_error(
                "track_inventory",
                f"{saved.name} cannot stop being stock-tracked while {why}: those goods "
                "would be left on the inventory account as if they were a service. "
                "Sell, use or write off the stock and bill what arrived first, then "
                "turn tracking off.")
        else:
            self.add_error(
                "inventory_account",
                f"{saved.name} cannot move to {now.code if now else 'another account'} while "
                f"{why}: the value already on {was.code if was else 'its account'} would stay "
                "there while every issue from now on came off the new account. Use, sell "
                "or write off the stock and bill what arrived first, then change the "
                "account.")
        return cleaned


    def _check_unit_change(self, saved, cleaned):
        """Refuse a new counting unit once the product has stock or history.

        Every quantity on record — on the shelf, on each movement, on its order
        lines and in the recipes that use it — is a number of the product's
        unit. Changing the unit does not change those numbers, so 12 pieces on
        the shelf would become 12 boxes with nothing having moved.
        """
        from .models import StockLevel, StockMovement
        unit = cleaned.get("unit_of_measure")
        if unit is None or unit.pk == saved.unit_of_measure_id:
            return
        has_stock = StockLevel.objects.filter(product=saved).exclude(quantity=0, value=0).exists()
        if has_stock or StockMovement.objects.filter(product=saved).exists():
            self.add_error(
                "unit_of_measure",
                f"{saved.name} already has stock or stock movements counted in "
                f"{saved.unit_of_measure}, so its unit cannot change to {unit}: every quantity "
                "on record would silently mean something else. Keep the unit, or set up "
                "a new product counted in the new unit.")

    def _check_retracking(self, saved, cleaned):
        """Turning stock tracking ON has to start from an empty shelf.

        While a product is untracked nothing it does reaches the inventory
        account, so a shelf it still holds is not in the ledger. Tracking it
        again would put that stock into the valuation with nothing behind it
        in 1400 — so those shelves are emptied when tracking comes on (the
        view does it, and says so), and the stock is counted on afresh.

        Goods received on an order while the product was untracked, and not
        yet billed, are refused outright: nothing was accrued for them, but
        once the product is tracked "Bill this order" would clear an accrual
        for them anyway, and the difference would land on the price variance.
        Bill what arrived first, then turn tracking on.
        """
        self.retracking = (not saved.track_inventory) and bool(cleaned.get("track_inventory"))
        if not self.retracking:
            return
        from apps.procurement.models import PurchaseOrderLine
        unbilled = [l for l in PurchaseOrderLine.objects.filter(
            product=saved, order__company=saved.company).exclude(
            order__status="CANCELLED").select_related("order") if l.to_bill > 0]
        if unbilled:
            refs = ", ".join(sorted({l.order.reference for l in unbilled}))
            self.add_error(
                "track_inventory",
                f"{saved.name} cannot start being stock-tracked while goods received on "
                f"{refs} have not been billed: they arrived untracked, so nothing was "
                "accrued for them, and the bill would clear an accrual that is not there. "
                "Bill this order first, then turn tracking on.")

    retracking = False


class StockMovementForm(PlainNumbersMixin, forms.ModelForm):
    """A stock adjustment typed in by hand, outside any order.

    Only movements that make sense on their own are offered. A transfer has two
    ends and is made on the transfer screen, which writes both; one end typed
    here would put stock on a shelf that nothing left, or take it off with
    nowhere for it to go. Receipts, issues, returns, production and opening
    balances each belong to the document that explains them.

    Stock going out is always valued at the shelf's own average, whatever is
    typed in the cost box: the shelf's value is its quantity times that
    average, and taking goods off at any other price would leave the inventory
    account and the stock valuation apart for good. Stock coming in needs a
    real, positive cost — or, for a surplus found on a count, the shelf's
    average when the box is left empty.
    """
    # What the hand-entry screen offers: adjustments, and nothing else. Goods
    # come in from a supplier through a goods receipt on a purchase order —
    # that is what accrues them on 2110 against the order the bill will later
    # answer — and they go out to a customer through a sales delivery or the
    # till, which is what charges their cost against the sale. A receipt typed
    # here had no order to accrue against, so its 2110 credit could only ever
    # be cleared by a bill coded to the accrual by hand, and an issue typed
    # here charged cost of sales with no sale behind it. Both are refused; a
    # count that differs from the shelf is an adjustment, and that is all this
    # screen records.
    CHOICES = [
        ("ADJUSTMENT_PLUS", "Adjustment increase — stock found or added on a count"),
        ("ADJUSTMENT_MINUS", "Adjustment decrease — stock lost, damaged or written off"),
    ]
    OUTGOING = ("ADJUSTMENT_MINUS",)

    class Meta:
        model = StockMovement
        fields = ["product", "warehouse", "movement_type", "quantity", "unit_cost", "movement_date", "notes", "reference"]
        widgets = {
            "movement_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["movement_type"].choices = self.CHOICES
        # Said in the bookkeeper's terms rather than "not a valid choice": the
        # receipt or issue they tried to type has a proper home elsewhere.
        self.fields["movement_type"].error_messages["invalid_choice"] = (
            "This screen records count adjustments only. Goods from a supplier are "
            "received on their purchase order (Receive Goods), and goods for a "
            "customer go out through a sales delivery or the till.")
        self.fields["unit_cost"].required = False
        self.fields["unit_cost"].help_text = (
            "Cost of one unit coming in. Stock going out is always taken off at "
            "the shelf's average cost, so this is ignored for a decrease.")
        self.company = company
        if not company:
            return
        from apps.organizations.models import Warehouse
        self.fields["product"].queryset = Product.objects.filter(
            company=company, is_active=True).select_related("unit_of_measure")
        self.fields["warehouse"].queryset = Warehouse.objects.filter(
            company=company, is_active=True)

    company = None

    def clean_quantity(self):
        quantity = self.cleaned_data.get("quantity")
        if quantity is not None and quantity <= 0:
            raise forms.ValidationError("A movement has to move something — use a positive quantity.")
        return quantity

    def clean_movement_date(self):
        """An adjustment records a count already made: not tomorrow's, and not
        one inside a month that has been closed."""
        from .models import stock_date_refusal
        day = self.cleaned_data.get("movement_date")
        if day and self.company is not None:
            refusal = stock_date_refusal(self.company, day, "An adjustment")
            if refusal:
                raise forms.ValidationError(refusal)
        return day

    def clean_unit_cost(self):
        cost = self.cleaned_data.get("unit_cost")
        if cost is not None and cost < 0:
            raise forms.ValidationError(
                "A unit cost cannot be negative. Enter what one unit cost, or leave "
                "it empty for a count surplus to take the shelf's average.")
        return cost if cost is not None else Decimal("0")

    def clean(self):
        from apps.inventory.numbers import MONEY, QUANTITY, fits
        from .models import StockLevel

        cleaned = super().clean()
        product = cleaned.get("product")
        warehouse = cleaned.get("warehouse")
        kind = cleaned.get("movement_type")
        quantity = cleaned.get("quantity")
        cost = cleaned.get("unit_cost") or Decimal("0")
        if not (product and warehouse and kind and quantity):
            return cleaned
        if not product.track_inventory:
            self.add_error(
                "product",
                f"{product.name} is not stock-tracked, so it has no shelf to adjust. "
                "Turn on stock tracking on the product first.")
            return cleaned
        level = StockLevel.objects.filter(product=product, warehouse=warehouse).first()
        on_hand = level.quantity if level else Decimal("0")
        average = level.avg_cost if level else Decimal("0")
        if kind in self.OUTGOING:
            # The shelf prices it; what was typed plays no part.
            cleaned["unit_cost"] = Decimal("0")
            if quantity > on_hand and not product.allow_negative_stock:
                self.add_error(
                    "quantity",
                    f"Only {on_hand.normalize():f} of {product.name} is on hand in "
                    f"{warehouse.name}, so {quantity.normalize():f} cannot go out.")
            value = quantity * average
        else:
            if kind == "ADJUSTMENT_PLUS" and cost <= 0 and average <= 0:
                self.add_error(
                    "unit_cost",
                    f"{product.name} has never been costed in {warehouse.name}, so there is "
                    "no shelf average to value the surplus at. Enter a unit cost greater than zero.")
            if not fits(on_hand + quantity, QUANTITY):
                self.add_error(
                    "quantity",
                    f"{quantity.normalize():f} more would put more on the shelf than the "
                    "stock record can hold. Check the quantity.")
            value = quantity * (cost if cost > 0 else average)
        if not fits(value, MONEY):
            self.add_error(
                None,
                f"{quantity.normalize():f} × {(cost or average).normalize():f} is worth more "
                "than the ledger can record in one line. Check the quantity and the unit cost.")
        return cleaned


class UnitOfMeasureForm(PlainNumbersMixin, forms.ModelForm):
    """One counting unit. The conversion factor is what lets a warehouse count
    in bags while purchasing pays per kilogram."""

    class Meta:
        model = UnitOfMeasure
        fields = ["name", "abbreviation", "kind", "base_factor", "is_base"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "e.g. 50kg Bag"}),
            "abbreviation": forms.TextInput(attrs={"placeholder": "e.g. BAG"}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if company is not None and self.instance._state.adding:
            # The model's own rules are company-scoped, so the instance needs
            # its company before validation runs, not after.
            self.instance.company = company
        base = ""
        if company:
            found = UnitOfMeasure.objects.filter(
                company=company, is_base=True, is_active=True).first()
            base = found.abbreviation if found else ""
        self.fields["base_factor"].help_text = (
            f"How much of the base unit of this kind one {self.instance.name or 'unit'} is worth "
            f"(1 for the base unit itself)."
            + (f" Base unit on file: {base}." if base else "")
        )

    def clean_abbreviation(self):
        abbrev = (self.cleaned_data.get("abbreviation") or "").strip().upper()
        if not abbrev:
            raise forms.ValidationError("A unit needs an abbreviation to print on documents.")
        clash = UnitOfMeasure.objects.filter(
            company=self.instance.company, abbreviation__iexact=abbrev)
        if not self.instance._state.adding:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(
                f"{abbrev} is already a unit in this company ({clash.first().name}).")
        return abbrev

    def clean_kind(self):
        kind = self.cleaned_data.get("kind")
        valid = dict(UnitOfMeasure.KINDS)
        if kind not in valid:
            raise forms.ValidationError(f"Unknown unit kind '{kind}'.")
        return kind

    def clean(self):
        """A unit in use keeps its factor and its kind.

        Products, recipe lines, order lines and stock movements all hold plain
        numbers of a unit. Re-defining what the unit is worth (or what it
        measures) does not touch those numbers, so it would silently rescale
        them: a recipe calling for 2 kg would start drawing 2 tonnes. Once the
        unit is used, a different size is a new unit.
        """
        from .models import unit_in_use
        cleaned = super().clean()
        unit = self.instance
        if unit._state.adding or not unit.pk:
            return cleaned
        saved = UnitOfMeasure.objects.filter(pk=unit.pk).first()
        if saved is None:
            return cleaned
        factor = cleaned.get("base_factor")
        kind = cleaned.get("kind")
        refactored = factor is not None and factor != saved.base_factor
        rekinded = kind is not None and kind != saved.kind
        rebased = "is_base" in cleaned and bool(cleaned.get("is_base")) != saved.is_base
        if not (refactored or rekinded or rebased):
            return cleaned
        if unit_in_use(saved):
            what = ("what it is worth" if refactored else
                    "what it measures" if rekinded else "whether it is the base unit")
            self.add_error(
                None,
                f"{saved.abbreviation} is already used by products, recipes, orders or "
                f"stock movements, so {what} cannot change: every quantity on record in "
                f"{saved.abbreviation} would silently be rescaled. Add a new unit with the "
                "factor you need instead.")
        return cleaned


# ───────────────────────────── product categories ────────────────────────────

class CategoryPickerWidget(forms.Select):
    """Draws a category field as the cascading three-level picker.

    The include (``_includes/category_picker.html``) renders one ``<select>``
    carrying the field's name and every category by its full path, so the form
    works with scripts off; with scripts on it adds the linked Level 1 / 2 / 3
    selects and keeps that one named select set to the deepest level chosen.
    The form still posts a single category id under the field's own name.
    """

    def __init__(self, company=None, empty_label="— No category", attrs=None):
        super().__init__(attrs)
        self.company = company
        self.empty_label = empty_label

    def render(self, name, value, attrs=None, renderer=None):
        from django.template.loader import render_to_string
        from django.utils.safestring import mark_safe
        attrs = self.build_attrs(self.attrs, attrs)
        return mark_safe(render_to_string("_includes/category_picker.html", {
            "name": name,
            "selected": "" if value in (None, "") else str(value),
            "field_id": attrs.get("id") or f"id_{name}",
            "company": self.company,
            "empty_label": self.empty_label,
            "show_label": False,
        }))


class ProductCategoryForm(forms.ModelForm):
    """Create or edit a category. The parent list offers only valid parents:
    this company's active categories at level 1 or 2, never the category
    itself or anything beneath it, and none that would push its own
    sub-categories past level 3. The model's clean() checks the same again."""

    class Meta:
        model = ProductCategory
        fields = ["name", "parent", "code", "sort_order", "description"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if company is not None and self.instance._state.adding:
            self.instance.company = company
        parent = self.fields["parent"]
        parent.empty_label = "— None (top level)"
        parent.required = False
        parent.label = "Parent category"
        parent.help_text = (f"Leave blank for a level-1 category. Categories go "
                            f"{ProductCategory.MAX_DEPTH} levels deep at most.")
        if company is None:
            parent.queryset = ProductCategory.objects.none()
            return
        editing = not self.instance._state.adding
        height = self.instance.subtree_height() if editing else 1
        excluded = set(self.instance.descendant_ids()) if editing else set()
        valid, labels = [], {}
        for node in ProductCategory.flat_tree(company):
            cat = node["category"]
            if cat.pk in excluded or node["level"] + height > ProductCategory.MAX_DEPTH:
                continue
            valid.append(cat.pk)
            labels[cat.pk] = ("\u2003" * (node["level"] - 1)) + cat.name
        # A parent the category already has stays selectable, even if retired,
        # so an unrelated edit is not refused for a choice nobody made today.
        if editing and self.instance.parent_id and self.instance.parent_id not in valid:
            valid.append(self.instance.parent_id)
        parent.queryset = ProductCategory.objects.filter(pk__in=valid)
        order = {pk: i for i, pk in enumerate(valid)}
        parent.label_from_instance = lambda c: labels.get(c.pk, c.path_label)
        # Keep the tree order rather than the queryset's alphabetical one.
        parent.choices = [("", parent.empty_label)] + [
            (pk, labels.get(pk) or str(ProductCategory.objects.get(pk=pk)))
            for pk in sorted(valid, key=lambda pk: order[pk])]
        self.fields["sort_order"].required = False

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("A category needs a name.")
        if ">" in name:
            raise forms.ValidationError("A name cannot contain '>': it separates the levels.")
        return name

    def clean_sort_order(self):
        value = self.cleaned_data.get("sort_order")
        return 0 if value in (None, "") else value
