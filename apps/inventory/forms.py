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
        from .models import ProductCategory, UnitOfMeasure
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
    class Meta:
        model = StockMovement
        fields = ["product", "warehouse", "movement_type", "quantity", "unit_cost", "movement_date", "notes", "reference"]
        widgets = {
            "movement_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
