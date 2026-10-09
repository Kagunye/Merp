"""CRM forms."""
from django import forms
from .models import Customer, Supplier, Lead


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = [
            "name", "customer_type", "tax_id", "phone", "email", "website",
            "credit_limit", "payment_terms_days", "currency",
            "address_line1", "address_line2", "city", "country",
            "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
            "address_line1": forms.TextInput(attrs={"placeholder": "Street address"}),
            "city": forms.TextInput(attrs={"placeholder": "City"}),
            "country": forms.TextInput(attrs={"placeholder": "Kenya"}),
        }


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = [
            "name", "tax_id", "phone", "email", "website",
            "payment_terms_days", "currency",
            "address_line1", "address_line2", "city", "country",
            "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
        }


class LeadForm(forms.ModelForm):
    class Meta:
        model = Lead
        fields = [
            "name", "company_name", "phone", "email",
            "status", "source", "estimated_value", "expected_close_date",
            "assigned_to", "notes",
        ]
        widgets = {
            "expected_close_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
