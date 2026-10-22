from django import forms

from apps.core import guards
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


class FixedAssetForm(guards.GuardedFormMixin, forms.ModelForm):
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
            "purchase_date": forms.DateInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "type": "date"}),
            "purchase_cost": forms.NumberInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "step": "0.01"}),
            "residual_value": forms.NumberInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "step": "0.01"}),
            "useful_life_years": forms.NumberInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "depreciation_method": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "status": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "supplier": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "warranty_expiry": forms.DateInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "type": "date"}),
            "notes": forms.Textarea(attrs={"class": "form-textarea w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "rows": 3}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company

    def clean_purchase_cost(self):
        return guards.positive(self.cleaned_data.get("purchase_cost"), "Purchase cost")

    def clean_residual_value(self):
        return guards.not_negative(self.cleaned_data.get("residual_value"), "Residual value")

    def clean_useful_life_years(self):
        life = self.cleaned_data.get("useful_life_years")
        if life is not None and not (1 <= life <= 100):
            raise forms.ValidationError("Useful life must be between 1 and 100 years.")
        return life

    def clean_purchase_date(self):
        return guards.not_in_future(guards.plausible_date(self.cleaned_data.get("purchase_date"), "Purchase date",
                                                          years_back=50), "Purchase date")

    def clean(self):
        data = super().clean()
        cost, residual = data.get("purchase_cost"), data.get("residual_value")
        if cost and residual is not None and residual >= cost:
            self.add_error("residual_value", "Residual value must be less than the purchase cost.")
        guards.unique(self, FixedAsset, self.company, "code", "Asset tag")
        return data


class DepreciationRunForm(guards.GuardedFormMixin, forms.Form):
    period_date = forms.DateField(
        widget=forms.DateInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "type": "date"}),
        help_text="Run depreciation for this month (use the first day of the month)",
    )


class AssetDisposalForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = AssetDisposal
        fields = ["disposal_date", "disposal_type", "proceeds", "notes"]
        widgets = {
            "disposal_date": forms.DateInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "type": "date"}),
            "disposal_type": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "proceeds": forms.NumberInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "step": "0.01"}),
            "notes": forms.Textarea(attrs={"class": "form-textarea w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "rows": 3}),
        }

    def clean_proceeds(self):
        return guards.not_negative(self.cleaned_data.get("proceeds"), "Proceeds")

    def clean_disposal_date(self):
        return guards.not_in_future(guards.plausible_date(self.cleaned_data.get("disposal_date"), "Disposal date"),
                                    "Disposal date")
