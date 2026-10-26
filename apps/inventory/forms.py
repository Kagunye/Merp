"""Inventory forms."""
from decimal import Decimal

from django import forms
from apps.core.forms import date_input
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
        self.fields["unit_of_measure"].queryset = UnitOfMeasure.objects.filter(company=company, is_active=True)
        self.fields["tax_rate"].queryset = TaxRate.objects.filter(company=company, is_active=True)
        self.fields["tax_rate"].empty_label = "— Use default tax"
        accounts = Account.objects.filter(company=company, is_active=True)
        for fname, acct_type in (
            ("income_account", "REVENUE"),
            ("cogs_account", "COST_OF_GOODS"),
            ("inventory_account", "ASSET"),
        ):
            field = self.fields[fname]
            field.queryset = accounts.filter(account_type=acct_type)
            field.empty_label = "— System default"


class StockMovementForm(forms.ModelForm):
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
        if not company:
            return
        from apps.organizations.models import Warehouse
        self.fields["product"].queryset = Product.objects.filter(
            company=company, is_active=True).select_related("unit_of_measure")
        self.fields["warehouse"].queryset = Warehouse.objects.filter(
            company=company, is_active=True)

    def clean_quantity(self):
        quantity = self.cleaned_data.get("quantity")
        if quantity is not None and quantity <= 0:
            raise forms.ValidationError("A movement has to move something — use a positive quantity.")
        return quantity

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


class UnitOfMeasureForm(forms.ModelForm):
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
