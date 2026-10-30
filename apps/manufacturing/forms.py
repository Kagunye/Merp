from decimal import Decimal

from django import forms
from apps.core.forms import date_input
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

    def clean_production_quantity(self):
        # A work order's needs are the recipe scaled by planned ÷ this figure.
        # Nothing or less than nothing per run turns that into a division by
        # zero, or a recipe that hands materials back for every unit made.
        quantity = self.cleaned_data.get("production_quantity")
        if quantity is not None and quantity <= 0:
            raise forms.ValidationError(
                "Enter how many one run of this recipe makes — a number greater than zero.")
        return quantity


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

    def clean(self):
        cleaned = super().clean()
        component = cleaned.get("component")
        quantity = cleaned.get("quantity")
        unit = cleaned.get("unit_of_measure")
        if quantity is not None and quantity <= 0:
            self.add_error("quantity", "A BOM line has to consume something.")
        if component and not unit:
            # Write the recipe in the unit the store already keeps the item in.
            cleaned["unit_of_measure"] = unit = component.unit_of_measure
        if component and unit and component.unit_of_measure_id != unit.pk:
            if component.unit_of_measure.kind != unit.kind:
                self.add_error(
                    "unit_of_measure",
                    f"{component.name} is counted in {component.unit_of_measure} "
                    f"({component.unit_of_measure.kind_label}), not {unit} ({unit.kind_label}). "
                    "Pick a unit of the same kind, or the stock issue cannot be converted.",
                )
        return cleaned


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
            "planned_start": date_input({"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "planned_end": date_input({"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "warehouse": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "notes": forms.Textarea(attrs={"class": "form-textarea w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "rows": 3}),
        }

    def clean_planned_quantity(self):
        # A plan of nothing draws no materials, and a plan below nothing would
        # scale the recipe negative; either way the order could only put stock
        # on the shelf that nothing went into.
        quantity = self.cleaned_data.get("planned_quantity")
        if quantity is not None and quantity <= 0:
            raise forms.ValidationError(
                "Enter how many to make — a number greater than zero.")
        return quantity


class CompleteWorkOrderForm(forms.Form):
    # The finished goods carry the materials' cost divided by how many came
    # out, so a run that made nothing has no unit cost to put on the shelf.
    actual_quantity = forms.DecimalField(
        max_digits=12, decimal_places=4, min_value=Decimal("0.0001"),
        error_messages={"min_value": "Enter how many were made — more than zero. "
                                     "A run that made nothing should be cancelled instead."},
        widget=forms.NumberInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "step": "0.0001"}),
    )
    actual_end = forms.DateField(
        widget=date_input({"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
    )
