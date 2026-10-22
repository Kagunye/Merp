from django import forms
from django.forms import inlineformset_factory

from apps.core import guards
from apps.core.scoping import narrow

from .models import Bill, PurchaseOrder, PurchaseOrderLine, PurchaseRequisition


class PurchaseRequisitionForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = PurchaseRequisition
        fields = ["reference", "department", "required_date", "notes"]
        widgets = {
            "required_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        narrow(self, company, department="company")

    def clean_required_date(self):
        return guards.plausible_date(self.cleaned_data.get("required_date"), "Required date")

    def clean(self):
        data = super().clean()
        guards.unique(self, PurchaseRequisition, self.company, "reference", "Requisition number")
        return data


class PurchaseOrderForm(guards.GuardedFormMixin, forms.ModelForm):
    """Header only: the totals come from the lines."""

    class Meta:
        model = PurchaseOrder
        fields = ["reference", "supplier", "order_date", "expected_delivery_date", "warehouse", "notes"]
        widgets = {
            "order_date": forms.DateInput(attrs={"type": "date"}),
            "expected_delivery_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        narrow(self, company, supplier="company", warehouse="company")
        self.fields["supplier"].queryset = self.fields["supplier"].queryset.filter(is_active=True)
        self.fields["warehouse"].queryset = self.fields["warehouse"].queryset.filter(is_active=True)
        self.fields["warehouse"].required = True
        self.fields["warehouse"].help_text = "Where the goods will be received."

    def clean_order_date(self):
        return guards.plausible_date(self.cleaned_data.get("order_date"), "Order date")

    def clean(self):
        data = super().clean()
        guards.order(self, "order_date", "expected_delivery_date",
                     "Expected delivery can't be before the order date.")
        guards.unique(self, PurchaseOrder, self.company, "reference", "PO number")
        return data


class PurchaseOrderLineForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = PurchaseOrderLine
        fields = ["product", "description", "quantity", "unit_cost", "tax_rate"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        narrow(self, company, product="company", tax_rate="company")
        self.fields["product"].queryset = self.fields["product"].queryset.filter(is_active=True)
        self.fields["tax_rate"].queryset = self.fields["tax_rate"].queryset.filter(is_active=True)
        self.fields["tax_rate"].empty_label = "— No tax"

    def clean_quantity(self):
        return guards.positive(self.cleaned_data.get("quantity"), "Quantity")

    def clean_unit_cost(self):
        return guards.not_negative(self.cleaned_data.get("unit_cost"), "Unit cost")


PurchaseOrderLineFormSet = inlineformset_factory(
    PurchaseOrder, PurchaseOrderLine, form=PurchaseOrderLineForm,
    extra=3, min_num=1, validate_min=True, can_delete=True,
)


class BillForm(guards.GuardedFormMixin, forms.ModelForm):
    """Supplier bill header. Amounts are entered by the accountant and checked."""

    class Meta:
        model = Bill
        fields = ["reference", "supplier_reference", "supplier", "purchase_order", "bill_date", "due_date",
                  "expense_account", "subtotal", "tax_amount", "total_amount", "notes"]
        widgets = {
            "bill_date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        narrow(self, company, purchase_order="company")
        if "purchase_order" in self.fields:
            self.fields["purchase_order"].queryset = self.fields["purchase_order"].queryset.exclude(
                status="CANCELLED")

    def clean_bill_date(self):
        return guards.plausible_date(self.cleaned_data.get("bill_date"), "Bill date")

    def clean_subtotal(self):
        return guards.not_negative(self.cleaned_data.get("subtotal"), "Subtotal")

    def clean_tax_amount(self):
        return guards.not_negative(self.cleaned_data.get("tax_amount"), "VAT")

    def clean_total_amount(self):
        return guards.positive(self.cleaned_data.get("total_amount"), "Total")

    def clean(self):
        data = super().clean()
        guards.order(self, "bill_date", "due_date", "Due date can't be before the bill date.")
        guards.unique(self, Bill, self.company, "reference", "Bill number")
        supplier, sref = data.get("supplier"), (data.get("supplier_reference") or "").strip()
        if supplier and sref and self.company is not None:
            dup = Bill.objects.filter(company=self.company, supplier=supplier, supplier_reference__iexact=sref) \
                .exclude(status="CANCELLED")
            if not self.instance._state.adding:
                dup = dup.exclude(pk=self.instance.pk)
            first = dup.first()
            if first:
                self.add_error("supplier_reference",
                               f"{supplier.name}'s invoice {sref} is already entered as bill {first.reference}. "
                               f"Entering it again would pay it twice.")
        return data
