from decimal import Decimal

from django import forms
from apps.core.forms import date_input, is_new, lock_currency
from .models import Bill, PurchaseOrder, PurchaseRequisition


class PurchaseRequisitionForm(forms.ModelForm):
    class Meta:
        model = PurchaseRequisition
        fields = ["reference", "department", "required_date", "notes"]
        widgets = {
            "required_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }


class BillForm(forms.ModelForm):
    class Meta:
        model = Bill
        fields = [
            "reference", "supplier", "purchase_order", "supplier_reference",
            "bill_date", "due_date", "currency", "subtotal", "tax_amount",
            "total_amount", "notes",
        ]
        widgets = {
            "bill_date": date_input(),
            "due_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if not company:
            return
        from apps.crm.models import Supplier
        self.fields["supplier"].queryset = Supplier.objects.filter(company=company, is_active=True)
        self.fields["purchase_order"].queryset = PurchaseOrder.objects.filter(company=company)
        self.fields["purchase_order"].empty_label = "— Not linked to an order"
        lock_currency(self, company)
        if is_new(self):
            import datetime
            self.fields["bill_date"].initial = datetime.date.today()
            self.fields["due_date"].initial = datetime.date.today() + datetime.timedelta(days=30)

    def clean(self):
        cleaned = super().clean()
        subtotal = cleaned.get("subtotal") or Decimal("0")
        tax = cleaned.get("tax_amount") or Decimal("0")
        total = cleaned.get("total_amount")
        if total is None:
            return cleaned
        if subtotal < 0 or tax < 0 or total < 0:
            raise forms.ValidationError("Bill amounts cannot be negative.")
        if abs((subtotal + tax) - total) > Decimal("0.01"):
            self.add_error(
                "total_amount",
                f"Total must equal subtotal + tax ({subtotal + tax}). "
                "The payable posted to the ledger must match the bill.",
            )
        return cleaned
