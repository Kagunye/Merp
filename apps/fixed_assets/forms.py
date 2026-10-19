from django import forms
from apps.core.forms import date_input
from .models import AssetCategory, FixedAsset, AssetDisposal


class AssetCategoryForm(forms.ModelForm):
    class Meta:
        model = AssetCategory
        fields = [
            "name", "default_useful_life", "default_depreciation_method",
            "gl_asset_account", "gl_depreciation_account", "gl_accumulated_account",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "default_useful_life": forms.NumberInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "default_depreciation_method": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "gl_asset_account": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "gl_depreciation_account": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "gl_accumulated_account": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
        }


class FixedAssetForm(forms.ModelForm):
    class Meta:
        model = FixedAsset
        fields = [
            "code", "name", "category", "serial_number", "location",
            "purchase_date", "purchase_cost", "residual_value",
            "useful_life_years", "depreciation_method", "status",
            "supplier", "warranty_expiry", "notes",
        ]
        widgets = {
            "code": forms.TextInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "name": forms.TextInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "category": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "serial_number": forms.TextInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "location": forms.TextInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "purchase_date": date_input({"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "purchase_cost": forms.NumberInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "step": "0.01"}),
            "residual_value": forms.NumberInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "step": "0.01"}),
            "useful_life_years": forms.NumberInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "depreciation_method": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "status": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "supplier": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "warranty_expiry": date_input({"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "notes": forms.Textarea(attrs={"class": "form-textarea w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "rows": 3}),
        }


class DepreciationRunForm(forms.Form):
    period_date = forms.DateField(
        widget=date_input({"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
        help_text="Run depreciation for this month (use the first day of the month)",
    )


class AssetDisposalForm(forms.ModelForm):
    class Meta:
        model = AssetDisposal
        fields = ["disposal_date", "disposal_type", "proceeds", "notes"]
        widgets = {
            "disposal_date": date_input({"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "disposal_type": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "proceeds": forms.NumberInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "step": "0.01"}),
            "notes": forms.Textarea(attrs={"class": "form-textarea w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "rows": 3}),
        }
