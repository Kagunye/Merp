from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory

from apps.core import guards
from .models import BillOfMaterials, BOMLine, WorkOrder


class BOMForm(guards.GuardedFormMixin, forms.ModelForm):
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
        return guards.positive(self.cleaned_data.get("production_quantity"), "Batch size")


class BOMLineForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = BOMLine
        fields = ["component", "quantity", "unit_of_measure", "scrap_percent"]
        widgets = {
            "component": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "quantity": forms.NumberInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "step": "0.0001"}),
            "unit_of_measure": forms.Select(attrs={"class": "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"}),
            "scrap_percent": forms.NumberInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "step": "0.01"}),
        }

    def clean_quantity(self):
        return guards.positive(self.cleaned_data.get("quantity"), "Component quantity")

    def clean_scrap_percent(self):
        return guards.percent(self.cleaned_data.get("scrap_percent"), "Scrap")


class _BOMLines(BaseInlineFormSet):
    """A recipe needs at least one component, and can't contain what it makes."""
    made = None   # set by the view to the product being made

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        live = [f for f in self.forms if f.cleaned_data and not f.cleaned_data.get("DELETE")]
        if not live:
            raise forms.ValidationError("Add at least one component: a product can't be made from nothing.")
        for f in live:
            if self.made is not None and f.cleaned_data.get("component") == self.made:
                raise forms.ValidationError(f"{self.made.name} can't be a component of itself.")


BOMLineFormSet = inlineformset_factory(
    BillOfMaterials, BOMLine,
    form=BOMLineForm,
    formset=_BOMLines,
    extra=3,
    can_delete=True,
)


class WorkOrderForm(guards.GuardedFormMixin, forms.ModelForm):
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

    def clean_planned_quantity(self):
        return guards.positive(self.cleaned_data.get("planned_quantity"), "Quantity to make")

    def clean_planned_start(self):
        return guards.plausible_date(self.cleaned_data.get("planned_start"), "Planned start")

    def clean(self):
        data = super().clean()
        guards.order(self, "planned_start", "planned_end", "Planned end can't be before the planned start.")
        return data


class CompleteWorkOrderForm(guards.GuardedFormMixin, forms.Form):
    actual_quantity = forms.DecimalField(
        max_digits=12, decimal_places=4,
        widget=forms.NumberInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "step": "0.0001"}),
    )
    actual_end = forms.DateField(
        widget=forms.DateInput(attrs={"class": "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "type": "date"}),
    )

    def clean_actual_quantity(self):
        return guards.positive(self.cleaned_data.get("actual_quantity"), "Quantity produced")

    def clean_actual_end(self):
        return guards.not_in_future(self.cleaned_data.get("actual_end"), "Completion date")
