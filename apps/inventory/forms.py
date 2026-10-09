"""Inventory forms."""
from django import forms
from .models import Product, StockMovement, ProductCategory, UnitOfMeasure


class ProductForm(forms.ModelForm):
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


class StockMovementForm(forms.ModelForm):
    class Meta:
        model = StockMovement
        fields = ["product", "warehouse", "movement_type", "quantity", "unit_cost", "movement_date", "notes", "reference"]
        widgets = {
            "movement_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
