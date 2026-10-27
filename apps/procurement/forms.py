from decimal import Decimal

from django import forms
from apps.core.forms import date_input, is_new, pick_currency
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
        pick_currency(self, company)
        if not is_new(self):
            # The order link decides which received goods this bill pays for;
            # moving it afterwards would bill another order's goods twice.
            self.fields["purchase_order"].disabled = True
            self.fields["purchase_order"].help_text = "Fixed once the bill is entered."
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
        po = cleaned.get("purchase_order")
        if po and is_new(self):
            self._check_order_billable(po)
        return cleaned

    def _check_order_billable(self, po):
        """A bill linked to an order pays for its received, unbilled goods.

        Without this, a bill typed in by hand and "Create bill" on the order
        both charge for the same delivery.
        """
        if po.status not in ("SENT", "PARTIALLY_RECEIVED", "FULLY_RECEIVED"):
            self.add_error("purchase_order", f"{po.reference} is {po.get_status_display().lower()}, "
                                             "so it can't take another bill.")
            return
        if any(line.to_bill > 0 for line in po.lines.all()):
            return
        others = po.bills.exclude(status="CANCELLED").filter(original_bill__isnull=True)
        if others.exists():
            refs = ", ".join(b.reference for b in others[:3])
            self.add_error("purchase_order", f"Everything received on {po.reference} is already billed "
                                             f"({refs}). Cancel that bill first, or leave this one unlinked "
                                             "if it is for something else.")
        else:
            self.add_error("purchase_order", f"Nothing has been received on {po.reference} yet. Record the "
                                             "goods receipt first: a bill pays for what arrived.")
