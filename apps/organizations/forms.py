from django import forms
from .models import Branch, Company, Department, Warehouse
from .validators import validate_timezone


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

    def __init__(self, *args, platform_admin=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not platform_admin:
            # "Default company" is one flag across the whole platform: ticking
            # it on one tenant clears it on every other tenant (Company.save).
            # A company admin has no business changing another company's row,
            # so only a platform admin is offered it.
            self.fields.pop("is_default", None)
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

    def clean_fiscal_year_start_month(self):
        # The first fiscal year and its twelve periods are built from this
        # month when the company is created; month 0 or 13 is no month at all
        # and would crash that set-up instead of being refused here.
        month = self.cleaned_data.get("fiscal_year_start_month")
        if month is not None and not 1 <= month <= 12:
            raise forms.ValidationError(
                "Enter the month the fiscal year starts as a number from 1 "
                "(January) to 12 (December).")
        return month

    def clean_timezone(self):
        return validate_timezone((self.cleaned_data.get("timezone") or "").strip())


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
        self.fields["is_headquarters"].help_text = (
            "A company has one head office: ticking this moves the title from "
            "the current headquarters to this branch.")

    def clean_is_headquarters(self):
        """The head office can be moved, not abolished.

        Ticking the box on another branch moves the title there. Unticking it
        on the branch that holds it would leave the company with no
        headquarters at all, and the screens and documents that print the
        head-office address would have none to print.
        """
        value = self.cleaned_data.get("is_headquarters")
        if value or self.instance._state.adding or not self.instance.pk:
            return value
        was_hq = Branch.objects.filter(pk=self.instance.pk, is_headquarters=True).exists()
        if was_hq:
            raise forms.ValidationError(
                "This branch is the company's headquarters, and a company needs one. "
                "To move the head office, open the branch that should take over and "
                "tick Headquarters there.")
        return value

    def clean_code(self):
        """Branch codes are how people tell branches apart on reports and
        documents, so two branches of one company may not share a code. A
        blank code is allowed (and may repeat): it is simply not set."""
        code = (self.cleaned_data.get("code") or "").strip()
        company = self.cleaned_data.get("company") or getattr(self.instance, "company", None)
        if code and company is not None:
            twin = Branch.objects.filter(company=company, code__iexact=code).exclude(
                pk=self.instance.pk).first()
            if twin:
                raise forms.ValidationError(
                    f"Branch '{twin.name}' already uses the code {twin.code}. "
                    "Give this branch a different code.")
        return code


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

    def clean_parent(self):
        """Refuse a parent that would make the tree loop back on itself.

        Picking yourself is excluded from the list, but picking one of your
        own sub-departments is not: Sales under Retail under Sales has no top,
        and anything that walks the tree (org charts, roll-ups) never ends.
        """
        parent = self.cleaned_data.get("parent")
        if parent is None or not self.instance.pk:
            return parent
        seen = set()
        node = parent
        while node is not None and node.pk not in seen:
            if node.pk == self.instance.pk:
                raise forms.ValidationError(
                    f"{parent.name} sits under {self.instance.name}, so it can't also be "
                    "its parent. Pick a department higher up the tree, or none.")
            seen.add(node.pk)
            node = node.parent
        return parent


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
        if "is_default" in self.fields:
            self.fields["is_default"].help_text = (
                "A company has one default warehouse: ticking this moves the role "
                "from the current default to this one.")
