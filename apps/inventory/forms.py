"""Inventory forms."""
from django import forms

from apps.core import guards
from .models import Product, StockMovement, ProductCategory, UnitOfMeasure


class ProductForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            "sku", "name", "product_type", "category", "unit_of_measure",
            "cost_price", "selling_price", "reorder_level", "reorder_quantity",
            "track_inventory", "allow_negative_stock", "valuation_method",
            "description",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company

    def clean_cost_price(self):
        return guards.not_negative(self.cleaned_data.get("cost_price"), "Cost price")

    def clean_selling_price(self):
        return guards.not_negative(self.cleaned_data.get("selling_price"), "Selling price")

    def clean_reorder_level(self):
        return guards.not_negative(self.cleaned_data.get("reorder_level"), "Reorder level")

    def clean_reorder_quantity(self):
        return guards.not_negative(self.cleaned_data.get("reorder_quantity"), "Reorder quantity")

    def clean(self):
        data = super().clean()
        guards.unique(self, Product, self.company, "sku", "SKU")
        return data


class StockMovementForm(guards.GuardedFormMixin, forms.ModelForm):
    """Manual stock adjustments. Receipts, sales, transfers and production
    have their own screens, so they aren't offered here."""
    ADJUSTMENT_TYPES = ("ADJUSTMENT_PLUS", "ADJUSTMENT_MINUS", "OPENING")

    class Meta:
        model = StockMovement
        fields = ["product", "warehouse", "movement_type", "quantity", "unit_cost", "movement_date", "notes", "reference"]
        widgets = {
            "movement_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["movement_type"].choices = [
            c for c in self.fields["movement_type"].choices if c[0] in self.ADJUSTMENT_TYPES]

    def clean_quantity(self):
        return guards.positive(self.cleaned_data.get("quantity"),
                               "Quantity (choose increase or decrease for the direction)")

    def clean_unit_cost(self):
        return guards.not_negative(self.cleaned_data.get("unit_cost"), "Unit cost")

    def clean_movement_date(self):
        return guards.not_in_future(guards.plausible_date(self.cleaned_data.get("movement_date"), "Date"), "Date")
