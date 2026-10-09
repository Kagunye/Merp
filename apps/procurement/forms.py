from django import forms
from .models import Bill, PurchaseOrder, PurchaseRequisition


class PurchaseRequisitionForm(forms.ModelForm):
    class Meta:
        model = PurchaseRequisition
        fields = ["reference", "department", "required_date", "notes"]
        widgets = {
            "required_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
