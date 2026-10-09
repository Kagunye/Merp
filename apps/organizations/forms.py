from django import forms
from .models import Branch, Company, Department, Warehouse


class CompanyForm(forms.ModelForm):
    class Meta:
        model = Company
        fields = ["name", "legal_name", "registration_number", "tax_id", "vat_number",
                  "currency", "timezone", "fiscal_year_start_month", "logo", "industry",
                  "is_default", "phone", "email", "website", "address_line1", "address_line2",
                  "city", "state_province", "postal_code", "country"]


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
