"""Inventory forms."""
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


ADJUSTMENT_TYPES = ("ADJUSTMENT_PLUS", "ADJUSTMENT_MINUS", "OPENING")


class StockMovementForm(forms.ModelForm):
    class Meta:
        model = StockMovement
        fields = ["product", "warehouse", "movement_type", "quantity", "unit_cost", "movement_date", "notes", "reference"]
        widgets = {
            "movement_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not company:
            return
        from apps.organizations.models import Warehouse
        self.fields["product"].queryset = Product.objects.filter(
            company=company, is_active=True).select_related("unit_of_measure")
        self.fields["warehouse"].queryset = Warehouse.objects.filter(
            company=company, is_active=True)
        # Receipts, deliveries, transfers, returns and production each have
        # their own screen, which books both sides. Typed here they would move
        # stock with half an entry, or none.
        self.fields["movement_type"].choices = [
            c for c in self.fields["movement_type"].choices if c[0] in ADJUSTMENT_TYPES]
        self.fields["unit_cost"].required = False
        self.fields["unit_cost"].help_text = (
            "For an increase or opening stock. A decrease always leaves at the "
            "shelf's average cost.")

    def clean_unit_cost(self):
        cost = self.cleaned_data.get("unit_cost")
        if cost is not None and (not cost.is_finite() or cost < 0):
            raise forms.ValidationError("Unit cost must be zero or more.")
        return cost or 0

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("movement_type") == "ADJUSTMENT_MINUS":
            # Stock leaves the book at what the book holds it at; a typed
            # figure would make the ledger and the shelf disagree.
            cleaned["unit_cost"] = 0
            self.instance.unit_cost = 0
        return cleaned

    def clean_quantity(self):
        quantity = self.cleaned_data.get("quantity")
        if quantity is not None and quantity <= 0:
            raise forms.ValidationError("A movement has to move something — use a positive quantity.")
        return quantity


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
