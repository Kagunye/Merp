from django import forms
from .models import Branch, Company, Department, Warehouse


# Codes the ledger can book in. This is the book's own currency: every foreign
# document is translated into it at a filed rate before it reaches the journal.
CURRENCY_CHOICES = [
    ("KES", "KES — Kenyan shilling"),
    ("UGX", "UGX — Ugandan shilling"),
    ("TZS", "TZS — Tanzanian shilling"),
    ("RWF", "RWF — Rwandan franc"),
    ("ETB", "ETB — Ethiopian birr"),
    ("USD", "USD — US dollar"),
    ("EUR", "EUR — euro"),
    ("GBP", "GBP — pound sterling"),
]


class CompanyForm(forms.ModelForm):
    # A ChoiceField, not a CharField with a Select widget: only the field owns
    # the option list, so a legacy code can be appended and still validates.
    currency = forms.ChoiceField(choices=CURRENCY_CHOICES, label="Currency")

    class Meta:
        model = Company
        fields = ["name", "legal_name", "registration_number", "tax_id", "vat_number",
                  "currency", "timezone", "fiscal_year_start_month", "logo", "industry",
                  "is_default", "phone", "email", "website", "address_line1", "address_line2",
                  "city", "state_province", "postal_code", "country"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        code = self.instance.currency or "KES"
        if code not in dict(CURRENCY_CHOICES):
            self.fields["currency"].choices = CURRENCY_CHOICES + [(code, code)]
        self.fields["currency"].help_text = (
            "Base currency for the whole ledger. Foreign invoices, bills and "
            "payments are converted into it at the rate filed for their date."
        )
        if self.instance.pk and self.instance.journal_entries.exists():
            # Every posted figure is in this currency; changing it would relabel
            # the whole ledger (KES 1,000 owed would read as USD 1,000).
            self.fields["currency"].disabled = True
            self.fields["currency"].help_text = (
                "Fixed: the ledger already holds entries in this currency.")


class BranchForm(forms.ModelForm):
    class Meta:
        model = Branch
        fields = ["company", "name", "code", "is_headquarters", "manager_name",
                  "phone", "email", "address_line1", "city", "country"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company:
            # A branch can only ever hang off the company you are working in.
            self.fields["company"].queryset = Company.objects.filter(pk=company.pk)
            self.fields["company"].initial = company.pk


class DepartmentForm(forms.ModelForm):
    class Meta:
        model = Department
        fields = ["company", "branch", "name", "code", "parent"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company:
            self.fields["company"].queryset = Company.objects.filter(pk=company.pk)
            self.fields["company"].initial = company.pk
            self.fields["branch"].queryset = Branch.objects.filter(
                company=company, is_active=True).order_by("name")
            self.fields["parent"].queryset = Department.objects.filter(
                company=company, is_active=True).order_by("name")
            if self.instance.pk:
                self.fields["parent"].queryset = self.fields["parent"].queryset.exclude(
                    pk=self.instance.pk)


class WarehouseForm(forms.ModelForm):
    class Meta:
        model = Warehouse
        fields = ["company", "branch", "name", "code", "is_default",
                  "address_line1", "city", "country"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company:
            self.fields["company"].queryset = Company.objects.filter(pk=company.pk)
            self.fields["company"].initial = company.pk
            self.fields["branch"].queryset = Branch.objects.filter(
                company=company, is_active=True).order_by("name")
