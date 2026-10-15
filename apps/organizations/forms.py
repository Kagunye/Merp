from django import forms
from .models import Branch, Company, Department, Warehouse


class CompanyForm(forms.ModelForm):
    class Meta:
        model = Company
        fields = ["name", "legal_name", "registration_number", "tax_id", "vat_number",
                  "currency", "timezone", "fiscal_year_start_month", "logo", "industry",
                  "is_default", "phone", "email", "website", "address_line1", "address_line2",
                  "city", "state_province", "postal_code", "country"]


class CompanyBrandingForm(forms.ModelForm):
    """Letterhead + theme settings — logo, colours, header/footer, bank block."""
    class Meta:
        model = Company
        fields = [
            "logo", "tagline", "primary_color", "accent_color",
            "document_header", "document_footer", "bank_details",
        ]
        widgets = {
            "primary_color": forms.TextInput(attrs={"type": "color", "class": "h-10 w-20 p-0 border rounded"}),
            "accent_color": forms.TextInput(attrs={"type": "color", "class": "h-10 w-20 p-0 border rounded"}),
            "tagline": forms.TextInput(attrs={"placeholder": "e.g. Trusted since 2010"}),
            "document_header": forms.Textarea(attrs={"rows": 3, "placeholder": "Optional extra text above the letterhead"}),
            "document_footer": forms.Textarea(attrs={"rows": 3, "placeholder": "Payment terms, thank-you note, legal footnotes"}),
            "bank_details": forms.Textarea(attrs={"rows": 4, "placeholder": "Bank: KCB\nAccount: 1234567890\nBranch: Westlands\nSWIFT: KCBLKENX"}),
        }


class BranchForm(forms.ModelForm):
    class Meta:
        model = Branch
        fields = ["company", "name", "code", "is_headquarters", "manager_name",
                  "phone", "email", "address_line1", "city", "country"]


class DepartmentForm(forms.ModelForm):
    class Meta:
        model = Department
        fields = ["company", "branch", "name", "code", "parent"]


class WarehouseForm(forms.ModelForm):
    class Meta:
        model = Warehouse
        fields = ["company", "branch", "name", "code", "is_default",
                  "address_line1", "city", "country"]
