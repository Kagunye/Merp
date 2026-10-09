from django import forms
from django.forms import inlineformset_factory
from .models import BillOfMaterials, BOMLine, WorkOrder


class BOMForm(forms.ModelForm):
    class Meta:
        model = BillOfMaterials
        fields = ["name", "product", "version", "status", "production_quantity", "notes"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "product": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "version": forms.TextInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "status": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "production_quantity": forms.NumberInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "step": "0.0001"}),
            "notes": forms.Textarea(attrs={"class": "form-textarea w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "rows": 3}),
        }


class BOMLineForm(forms.ModelForm):
    class Meta:
        model = BOMLine
        fields = ["component", "quantity", "unit_of_measure", "scrap_percent"]
        widgets = {
            "component": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "quantity": forms.NumberInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "step": "0.0001"}),
            "unit_of_measure": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "scrap_percent": forms.NumberInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "step": "0.01"}),
        }


BOMLineFormSet = inlineformset_factory(
    BillOfMaterials, BOMLine,
    form=BOMLineForm,
    extra=3,
    can_delete=True,
)


class WorkOrderForm(forms.ModelForm):
    class Meta:
        model = WorkOrder
        fields = ["reference", "bom", "planned_quantity", "planned_start", "planned_end", "warehouse", "notes"]
        widgets = {
            "reference": forms.TextInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "bom": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "planned_quantity": forms.NumberInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "step": "0.0001"}),
            "planned_start": forms.DateInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "type": "date"}),
            "planned_end": forms.DateInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "type": "date"}),
            "warehouse": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "notes": forms.Textarea(attrs={"class": "form-textarea w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "rows": 3}),
        }


class CompleteWorkOrderForm(forms.Form):
    actual_quantity = forms.DecimalField(
        max_digits=12, decimal_places=4,
        widget=forms.NumberInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "step": "0.0001"}),
    )
    actual_end = forms.DateField(
        widget=forms.DateInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "type": "date"}),
    )
